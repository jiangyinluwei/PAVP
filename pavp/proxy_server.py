"""PAVP Proxy Server - Plan-act routing proxy

Each request from agent:
1. Plan (internal, cached per conversation): plan model generates plan on first request,
   reused on subsequent tool-result requests.
2. Act (transparent): plan injected into act model's system prompt,
   act model output (content + tool_calls) flows back to agent unchanged.
   Agent executes tools, sends next request -> repeat.

The proxy never executes tools. All tool execution is by the agent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

if sys.platform == "win32" and sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .engine import make_plan, plan_requires_act, _call_llm_raw, _call_llm_stream, _call_llm_text, build_pvap_stage
from .settings import load as load_settings, settings_path, DEFAULT_PORT

# In-memory plan cache: ckey -> {"plan": plan_json_str, "task_key": str}
_plan_cache: dict[str, dict] = {}

# Tracks active task_keys with their last-used timestamps for auto-cleanup.
# task_key -> {"last_used": float}
_task_cache: dict[str, dict] = {}

# Stale task timeout in seconds — tasks idle longer than this are evicted.
_TASK_TIMEOUT = 600

# Tracks compound keys for which the Act phase has already been started (first response sent).
# Used to distinguish "pvap Act..." (first turn) from "pvap Continue..." (follow-up turns).
_act_started: set[str] = set()

# 状态文件：供 Streamlit UI 实时读取当前 PAVP 代理状态
_STATE_FILE = Path.home() / ".pavp" / "current_state.json"


def _update_proxy_state(fsm_state: str, iteration: int = 0, *, last_api_call: str | None = None) -> None:
    """将当前代理状态写入文件，供 UI 消费。

    Args:
        fsm_state: 当前状态 (PLANNING, ACTING, IDLE 等)
        iteration: 当前迭代次数
        last_api_call: 最近一次 API 调用的 ISO 时间戳，
                       不传则使用当前时间（即每次状态更新都刷新 API 调用时间）。
    """
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "fsm_state": fsm_state,
        "iteration": iteration,
        "updated_at": now,
        "last_api_call": last_api_call or now,
    }
    # 添加 task_key 信息（总数 + 随机采样），供 UI 展示 &N TK-xxx#N State 格式
    if _task_cache:
        data["task_key_count"] = len(_task_cache)
        data["task_key_sample"] = random.choice(list(_task_cache.keys()))
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _start_state_updater(fsm_state: str, iteration: int, interval: float = 15.0) -> threading.Event:
    """启动后台线程，定时刷新状态文件，防止 UI 因长时间无更新而显示 Standby...

    在阻塞式 LLM 调用（如 make_plan、_call_llm_raw）期间使用。
    返回一个 threading.Event，调用方在操作完成后应调用 .set() 停止后台线程。
    """
    stop_event = threading.Event()

    def _update_loop():
        while not stop_event.is_set():
            _update_proxy_state(fsm_state, iteration)
            stop_event.wait(timeout=interval)

    t = threading.Thread(target=_update_loop, daemon=True)
    t.start()
    return stop_event


# Markers indicating a message refers to earlier conversation context (anaphora).
# Used to tell follow-ups (reuse the current task's cached plan) from new
# independent requests (make a fresh plan). False positives are safe: no-act
# plans are never cached, so a stale plan can never suppress a needed Act.
_REF_MARKERS_ZH = [
    "上面", "上文", "上述", "之前的", "前面的", "前文", "刚才", "刚刚",
    "继续", "接着", "上面提到", "上次的", "刚才的", "前一轮", "上一轮",
    "这个文件", "这个函数", "这个代码", "这个问题", "这个类", "这个方法",
    "这个项目", "这段代码", "那段代码", "该文件", "该函数", "该代码",
    "该类", "该方法", "该项目", "修复它", "改它", "修改它", "修改一下",
    "补充一下", "完善一下", "优化一下", "改一下", "修复一下",
]
_REF_MARKERS_EN = [
    "above", "the above", "previous", "previously", "earlier", "as mentioned",
    "as discussed", "just now", "continue", "fix it", "modify it", "update it",
    "change it", "the code", "the file", "the function", "the class",
    "the method", "that code", "this code", "that file", "this file",
    "last response", "your answer", "your code",
]


def _references_prior(text: str) -> bool:
    """Heuristic: does this message refer to earlier conversation context?

    True  => follow-up: reuse the current task's cached plan.
    False => new independent request: make a fresh plan.
    """
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _REF_MARKERS_EN) or any(m in text for m in _REF_MARKERS_ZH)


def _user_messages(messages: list[dict]) -> list[str]:
    """Plain-text user messages in chronological order."""
    out: list[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        c = msg.get("content", "")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list) and c:
            out.append(str(c[-1].get("text", c[-1]) if isinstance(c[-1], dict) else c[-1]))
    return out


def _current_anchor(messages: list[dict]) -> str:
    """Return the originating message of the current task.

    The latest user message that does NOT reference prior context. Follow-ups
    (which DO reference context) share this anchor's cache key and reuse its
    plan. Falls back to the first user message if every message references
    context (or there are none).
    """
    user_texts = _user_messages(messages)
    for text in reversed(user_texts):
        if not _references_prior(text):
            return text
    return user_texts[0] if user_texts else ""


def _cache_key(messages: list[dict]) -> str:
    """Cache key from the current task's anchor message.

    The anchor is the latest non-referencing user message, so all follow-ups
    in the same task share one key (reusing the cached plan), while a new
    independent request becomes a new anchor and gets a fresh plan.
    """
    anchor = _current_anchor(messages)
    return hashlib.sha256(anchor.encode("utf-8", errors="replace")).hexdigest()


def _extract_task_key(plan_json: str) -> str:
    """Extract task_key from Plan model's JSON output.

    Returns the task_key if present, otherwise generates a fallback key.
    The Plan model is instructed to include a unique task_key for each new task.
    """
    try:
        data = json.loads(plan_json)
        tk = data.get("task_key", "")
        if tk and isinstance(tk, str) and tk.startswith("TK-"):
            return tk
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: generate a unique key
    return f"TK-{uuid.uuid4().hex[:8]}"


def _cleanup_stale_tasks() -> None:
    """Evict task_keys and their cached plans that have been idle for > _TASK_TIMEOUT.

    Called on each request to prevent memory leak from abandoned tasks.
    """
    now = time.time()
    stale_keys = [
        tk for tk, info in _task_cache.items()
        if now - info["last_used"] > _TASK_TIMEOUT
    ]
    if not stale_keys:
        return
    for tk in stale_keys:
        del _task_cache[tk]
        # Remove all _plan_cache entries with this task_key
        stale_ckeys = [ck for ck, v in _plan_cache.items()
                       if isinstance(v, dict) and v.get("task_key") == tk]
        for ck in stale_ckeys:
            compound = f"{tk}:{ck}"
            _act_started.discard(compound)
            del _plan_cache[ck]
    print(f"[PAVP] Cleaned {len(stale_keys)} stale task(s), removed {len(stale_ckeys)} plan(s)", flush=True)


def _extract_project_root(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str): parts.append(c)
        elif isinstance(c, list): parts.extend(str(x) for x in c)
    all_text = " ".join(parts)
    for pat in [r"Working directory:\s*([^\n]+)", r"Project root:\s*([^\n]+)",
                r"workspace:\s*([^\n]+)", r"current directory:\s*([^\n]+)"]:
        m = re.search(pat, all_text, re.IGNORECASE)
        if m:
            p = Path(m.group(1).strip())
            if p.exists(): return str(p)
    for word in all_text.split():
        p = Path(word.strip("\"'`"))
        if p.is_absolute() and p.exists() and p.is_dir():
            if (p / ".git").exists() or (p / "src").exists():
                return str(p)
    return os.getcwd()


def _extract_latest_user_msg(messages: list[dict]) -> str:
    """Extract the latest (most recent) user message - the current turn's intent.

    Used as the planning input on a cache miss so the plan reflects the current
    request rather than a stale earlier one.
    """
    user_texts = _user_messages(messages)
    return user_texts[-1] if user_texts else ""


def _extract_prompt(messages: list[dict]) -> str:
    """Extract the current requirement from the latest user message."""
    return _extract_latest_user_msg(messages)


def _count_turns(messages: list[dict]) -> int:
    """Count conversation turns (assistant messages)."""
    return sum(1 for m in messages if m.get("role") == "assistant")


def _is_finish_turn(messages: list[dict]) -> bool:
    """Check if the last assistant message has no tool_calls → workflow is complete.

    When the proxy's previous response had no tool_calls, the act model has
    finished executing the plan, and the current request is a follow-up turn
    that should be tagged as "pvap Finish".
    """
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            return not bool(msg.get("tool_calls"))
    return False


def _build_act_messages(messages: list[dict], plan: str) -> list[dict]:
    """Build messages for act model with plan injected into the system prompt.

    Instead of appending a new user message (which creates confusing
    consecutive-user patterns in multi-turn), prepend the plan to the
    first system message or insert it as a new system message.
    """
    act_messages = list(messages)  # shallow copy
    plan_prefix = (
        f"[PAVP Plan]\n{plan}\n\n"
        "Follow the plan above when responding. "
        "When executing the plan, use available agent-delegation tools "
        "(e.g. Task, subagent) to dispatch independent subtasks in parallel "
        "for efficiency. Each subtask assigned to a sub-agent should be "
        "self-contained with clear inputs and expected outputs."
    )

    # Find first system message and inject plan into it
    for i, m in enumerate(act_messages):
        if m.get("role") == "system":
            act_messages[i] = {**m, "content": f"{plan_prefix}\n\n{m.get('content', '')}"}
            return act_messages

    # No system message → insert one at the beginning
    act_messages.insert(0, {"role": "system", "content": plan_prefix})
    return act_messages


def create_app(settings: Optional[dict] = None) -> FastAPI:
    s = settings or load_settings()
    app = FastAPI(title="PAVP Proxy", version="0.5.0")

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "pavp-proxy"}

    @app.get("/v1/models")
    async def list_models():
        return {"object": "list", "data": [{"id": "pavp", "object": "model", "created": int(time.time()), "owned_by": "pavp"}]}

    @app.get("/info")
    async def info():
        return {
            "plan_model": s.get("plan_model", ""), "act_model": s.get("act_model", ""),
            "ready": bool(s.get("plan_model") and s.get("plan_api") and s.get("act_model") and s.get("act_api")),
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON"}})

        if body.get("model") != "pavp":
            return JSONResponse(
                status_code=400,
                content={"error": {"message": f"Invalid model: '{body.get('model', '')}'. This proxy only accepts model 'pavp'."}}
            )

        expected_key = s.get("litellm_master_key", "sk-pavp-local")
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != expected_key:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid or missing API key. Use 'Authorization: Bearer <litellm_master_key>'."}}
            )

        messages: list[dict] = body.get("messages", [])
        prompt = _extract_prompt(messages)
        project_root = _extract_project_root(messages)
        is_stream = body.get("stream", False)
        ckey = _cache_key(messages)
        turn_count = _count_turns(messages)

        # Extract tool definitions (for Plan context + Act forwarding)
        tools: Optional[list[dict]] = body.get("tools")
        tool_choice: Any = body.get("tool_choice")

        # Build extra: all original request fields forwarded to Act model.
        # Exclude fields we handle specially: model (set from settings),
        # messages (rewritten with plan injection), stream (consumed here).
        _extra_skip = {"model", "messages", "stream"}
        extra: dict[str, Any] = {
            k: v for k, v in body.items() if k not in _extra_skip
        }
        # Ensure tools/tool_choice are present even if not in extra (belt-and-suspenders)
        if tools and "tools" not in extra:
            extra["tools"] = tools
        if tool_choice is not None and "tool_choice" not in extra:
            extra["tool_choice"] = tool_choice

        if not s.get("plan_model"):
            return JSONResponse(status_code=502, content={"error": {"message": "Plan model not configured"}})

        try:
            # Phase 1: Plan. The cache key is the current task's anchor (the
            # latest user message that does NOT reference prior context), so
            # follow-ups reuse the anchor's plan while a new independent
            # request becomes a new anchor -> fresh plan. Only Act plans are
            # cached, so a no-act greeting/Q&A can never poison later turns.
            #
            # The Plan model generates a unique task_key for each new task.
            # task_key is used to isolate caches per task and to anchor
            # context across multiple turns of the same task.
            if ckey in _plan_cache:
                cached = _plan_cache[ckey]
                plan = cached["plan"]
                task_key = cached["task_key"]
                needs_act = plan_requires_act(plan)
                # Update last-used timestamp for this task
                if task_key in _task_cache:
                    _task_cache[task_key]["last_used"] = time.time()
                print(f"[PAVP] Plan (cached, task_key={task_key}, turn {turn_count}): {plan[:100]}...", flush=True)
                _update_proxy_state("ACTING", turn_count + 1)
            else:
                print(f"[PAVP] Planning (turn {turn_count}): {prompt[:60]}...", flush=True)
                _update_proxy_state("PLANNING", turn_count + 1)
                _plan_stop = _start_state_updater("PLANNING", turn_count + 1)
                try:
                    plan = make_plan(prompt, project_root, s, tools=tools)
                finally:
                    _plan_stop.set()
                needs_act = plan_requires_act(plan)
                if needs_act:
                    # Extract task_key from Plan model's JSON output
                    task_key = _extract_task_key(plan)
                    # Ensure uniqueness: if the key somehow collides, generate a fallback
                    if task_key in _task_cache:
                        task_key = f"TK-{uuid.uuid4().hex[:8]}"
                    _task_cache[task_key] = {"last_used": time.time()}
                    _plan_cache[ckey] = {"plan": plan, "task_key": task_key}
                else:
                    # Plan-only (no Act): no task_key needed
                    task_key = ""
                print(f"[PAVP] Plan done: task_key={task_key}, plan={plan[:120]}", flush=True)
                _update_proxy_state("ACTING", turn_count + 1)

            # Clean up stale tasks periodically
            _cleanup_stale_tasks()

            # Compound key for act_started tracking: task_key + ckey
            compound_key = f"{task_key}:{ckey}" if task_key else ckey

            if needs_act and not s.get("act_model"):
                return JSONResponse(status_code=502, content={"error": {"message": "Act model not configured"}})

            response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            model_name = body.get("model", "pavp")

            # Determine pavp_stage for the API response to the external agent
            # Use _act_started set to distinguish first Act (pvap Act...)
            # from follow-up turns (pvap Continue...).
            # If the last assistant message had no tool_calls, the workflow is complete.
            if not needs_act:
                pavp_stage = build_pvap_stage("plan")
            elif compound_key in _act_started:
                if _is_finish_turn(messages):
                    pavp_stage = build_pvap_stage("act", is_finish=True)
                else:
                    pavp_stage = build_pvap_stage("act", is_continue=True)
            else:
                pavp_stage = build_pvap_stage("act")
                _act_started.add(compound_key)

            # ---- Plan-only (no Act needed): route to act model directly ----
            if not needs_act:
                print(f"[PAVP] Plan indicates no Act needed (requires_act=false). Routing to act model for direct answer.", flush=True)

                if not s.get("act_model"):
                    return JSONResponse(status_code=502, content={"error": {"message": "Act model not configured"}})

                # Forward to act model with original messages (no plan injection).
                # The act model naturally answers the Q&A question.
                if is_stream:
                    def qa_generate():
                        nonlocal response_id, model_name, pavp_stage
                        last_state_update = time.time()
                        # Inject pavp_stage as first delta so agent sees it in context
                        initial = {
                            "id": response_id, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": model_name,
                            "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"{pavp_stage}\n\n"}, "finish_reason": None}],
                            "task_key": task_key,
                        }
                        yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
                        role_sent = True
                        try:
                            for line in _call_llm_stream(
                                s["act_model"], s["act_api"], s["act_base_url"],
                                messages, max_tokens=8192, timeout=600, extra=extra,
                            ):
                                # Periodically refresh state to prevent UI timeout
                                now = time.time()
                                if now - last_state_update > 15:
                                    _update_proxy_state("ACTING", turn_count + 1)
                                    last_state_update = now
                                line_str = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
                                if not line_str.startswith("data:"):
                                    continue
                                data_str = line_str[5:].strip()
                                if data_str == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    return
                                try:
                                    chunk = json.loads(data_str)
                                except json.JSONDecodeError:
                                    continue
                                chunk["id"] = response_id
                                chunk["model"] = model_name
                                chunk["pavp_stage"] = pavp_stage
                                chunk["task_key"] = task_key
                                if not role_sent:
                                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                                    if delta.get("role") is None:
                                        delta["role"] = "assistant"
                                    else:
                                        role_sent = True
                                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                        finally:
                            _update_proxy_state("DONE")

                    return StreamingResponse(qa_generate(), media_type="text/event-stream")

                # Non-streaming
                _qa_stop = _start_state_updater("ACTING", turn_count + 1)
                try:
                    resp = _call_llm_raw(s["act_model"], s["act_api"], s["act_base_url"],
                                         messages, max_tokens=8192, timeout=600, extra=extra)
                finally:
                    _qa_stop.set()
                msg = resp["choices"][0]["message"]
                # Inject pavp_stage into assistant message content so agent sees it
                if msg.get("content"):
                    msg["content"] = f"{pavp_stage}\n\n{msg['content']}"
                else:
                    msg["content"] = pavp_stage
                _update_proxy_state("DONE")
                return {
                    "id": response_id, "object": "chat.completion",
                    "created": int(time.time()), "model": model_name,
                    "choices": [{"index": 0, "message": msg,
                                 "finish_reason": resp["choices"][0].get("finish_reason", "stop")}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "pavp_stage": pavp_stage,
                    "task_key": task_key,
                }

            # ---- Normal mode: Phase 2: Act ----
            act_messages = _build_act_messages(messages, plan)

            if is_stream:
                print(f"[PAVP] Act (stream, turn {turn_count}): routing to {s['act_model']}...", flush=True)

                def generate():
                    nonlocal response_id, model_name, pavp_stage
                    last_state_update = time.time()
                    # Inject pavp_stage as first delta so agent sees it in context
                    initial = {
                        "id": response_id, "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": model_name,
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"{pavp_stage}\n\n"}, "finish_reason": None}],
                        "task_key": task_key,
                    }
                    yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
                    role_sent = True
                    try:
                        for line in _call_llm_stream(
                            s["act_model"], s["act_api"], s["act_base_url"],
                            act_messages, max_tokens=8192, timeout=600, extra=extra,
                        ):
                            # Periodically refresh state to prevent UI timeout
                            now = time.time()
                            if now - last_state_update > 15:
                                _update_proxy_state("ACTING", turn_count + 1)
                                last_state_update = now
                            line_str = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
                            if not line_str.startswith("data:"):
                                continue
                            data_str = line_str[5:].strip()
                            if data_str == "[DONE]":
                                yield "data: [DONE]\n\n"
                                return
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            # Remap to PAVP model identity
                            chunk["id"] = response_id
                            chunk["model"] = model_name
                            chunk["pavp_stage"] = pavp_stage
                            chunk["task_key"] = task_key
                            # Ensure first delta has role: "assistant"
                            if not role_sent:
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
                                if delta.get("role") is None:
                                    delta["role"] = "assistant"
                                else:
                                    role_sent = True
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    finally:
                        _update_proxy_state("DONE")

                return StreamingResponse(generate(), media_type="text/event-stream")

            # Non-streaming path
            print(f"[PAVP] Act (turn {turn_count}): routing to {s['act_model']}...", flush=True)
            _act_stop = _start_state_updater("ACTING", turn_count + 1)
            try:
                resp = _call_llm_raw(s["act_model"], s["act_api"], s["act_base_url"],
                                     act_messages, max_tokens=8192, timeout=600, extra=extra)
            finally:
                _act_stop.set()
            msg = resp["choices"][0]["message"]
            # If the act model responded without tool_calls, the workflow is complete.
            if not msg.get("tool_calls") and compound_key in _act_started:
                pavp_stage = build_pvap_stage("act", is_finish=True)
                print(f"[PAVP] Act finished: no tool_calls, pavp_stage={pavp_stage!r}", flush=True)
            # Inject pavp_stage into assistant message content so agent sees it
            if msg.get("content"):
                msg["content"] = f"{pavp_stage}\n\n{msg['content']}"
            else:
                msg["content"] = pavp_stage
            print(f"[PAVP] Act done: content_len={len(msg.get('content','') or '')}, "
                  f"tool_calls={len(msg.get('tool_calls',[]))}, "
                  f"finish_reason={resp['choices'][0].get('finish_reason','?')}", flush=True)
            _update_proxy_state("DONE")

            return {
                "id": response_id, "object": "chat.completion",
                "created": int(time.time()), "model": model_name,
                "choices": [{"index": 0, "message": msg,
                             "finish_reason": resp["choices"][0].get("finish_reason", "stop")}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "pavp_stage": pavp_stage,
                "task_key": task_key,
            }

        except Exception as e:
            import traceback
            print(f"[PAVP] Error: {e}\n{traceback.format_exc()}", flush=True)
            _update_proxy_state("IDLE")
            return JSONResponse(status_code=500, content={"error": {"message": str(e)}})

    return app


def run_server(host="0.0.0.0", port=None):
    s = load_settings()
    if port is None:
        port = s.get("proxy_port", DEFAULT_PORT)
    app = create_app(s)
    settings_file = settings_path()
    print(f"PAVP Proxy starting (pid={os.getpid()})", flush=True)
    print(f"  Settings file: {settings_file}", flush=True)
    print(f"  Home dir:      {Path.home()}", flush=True)
    print(f"  Listen:        http://{host}:{port}", flush=True)
    print(f"  Plan:          {s.get('plan_model','?')} @ {s.get('plan_base_url','?')}", flush=True)
    print(f"  Act:           {s.get('act_model','?')} @ {s.get('act_base_url','?')}", flush=True)
    print(f"  Ready:         {bool(s.get('plan_model') and s.get('plan_api') and s.get('act_model') and s.get('act_api'))}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=None,
                   help="Proxy port (default: from settings.json)")
    args = p.parse_args()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
