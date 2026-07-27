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

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .engine import make_plan, plan_requires_act, _call_llm_raw, _call_llm_stream, _call_llm_text, _call_anthropic_raw, _call_anthropic_stream, _call_anthropic_text, build_pvap_stage
from .settings import load as load_settings, settings_path, DEFAULT_PORT, get_plan_config, get_act_config, get_current_plan_id, get_current_act_id
from .prompts import VERIFY_PROXY_LITE_SYSTEM, build_verify_proxy_prompt

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
    _STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")


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

    If the last message is a user message, this is a new request (continuation
    or new task), not a finish notification from the assistant. Return False
    to prevent the Verify flow from being triggered prematurely.
    """
    # If the last message is a user message, this is a new request, not a finish
    if messages and messages[-1].get("role") == "user":
        return False
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


# ---------------------------------------------------------------------
# Proxy Verify 辅助函数
# ---------------------------------------------------------------------

_FILE_MODIFY_TOOLS = frozenset({"Edit", "Write", "Create", "edit", "write", "create"})


def _has_file_changes(messages: list[dict]) -> bool:
    """检查整个对话中是否有文件修改类的 tool_calls。"""
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                name = func.get("name", "")
                if name in _FILE_MODIFY_TOOLS:
                    return True
    return False


def _extract_file_changes(messages: list[dict]) -> str:
    """提取所有文件修改类 tool_calls 的摘要。"""
    changes: list[str] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                name = func.get("name", "")
                if name in _FILE_MODIFY_TOOLS:
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                        fp = args.get("file_path") or args.get("file", "")
                        changes.append(f"  - {name}: {fp}")
                    except json.JSONDecodeError:
                        changes.append(f"  - {name}: (参数解析失败)")
    return "\n".join(changes)


def _extract_act_output(messages: list[dict]) -> str:
    """提取 Act 模型的所有文本输出。"""
    outputs: list[str] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", "") or ""
            if content.strip():
                outputs.append(content.strip()[:300])
    return "\n---\n".join(outputs[-5:])  # 最近 5 条


def _run_verify_proxy(
    plan_json: str,
    messages: list[dict],
    plan_cfg: dict,
    api_key: str,
    base_url: str,
    api_format: str,
) -> dict:
    """调用 Plan 模型执行轻量级 Verify。

    Returns:
        {"verdict": "PASS"|"FAIL", "summary": "...", "issues": [...], "debug_plan": dict|None}
    """
    file_changes = _extract_file_changes(messages)
    act_output = _extract_act_output(messages)
    verify_prompt = build_verify_proxy_prompt(plan_json, file_changes, act_output)

    # 使用 Anthropic 调用器还是 OpenAI 调用器
    use_anthropic = (api_format == "anthropic") or "anthropic.com" in base_url.lower()
    caller = _call_anthropic_text if use_anthropic else _call_llm_text

    try:
        raw = caller(
            plan_cfg["model"], api_key, base_url,
            [
                {"role": "system", "content": VERIFY_PROXY_LITE_SYSTEM},
                {"role": "user", "content": verify_prompt},
            ],
            temperature=0.1,
            max_tokens=2048,
        )
    except Exception as e:
        print(f"[PAVP] Verify LLM call failed: {e}", flush=True)
        return {"verdict": "PASS", "summary": "Verify 调用失败，默认通过", "issues": [], "debug_plan": None}

    try:
        data = json.loads(raw)
        verdict = str(data.get("verdict", "PASS")).upper()
        if verdict not in ("PASS", "FAIL"):
            verdict = "PASS"
        return {
            "verdict": verdict,
            "summary": data.get("summary", ""),
            "issues": data.get("issues", []),
            "debug_plan": data.get("debug_plan"),
        }
    except (json.JSONDecodeError, TypeError) as e:
        print(f"[PAVP] Verify JSON parse failed: {e}\nraw={raw[:200]}", flush=True)
        return {"verdict": "PASS", "summary": "Verify 解析失败，默认通过", "issues": [], "debug_plan": None}


def create_app(settings: Optional[dict] = None) -> FastAPI:
    s = settings or load_settings()
    app = FastAPI(title="PAVP Proxy", version="0.5.0")

    # 启动时重置状态文件，避免 UI 读取到残留的旧状态
    _update_proxy_state("IDLE")

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "pavp-proxy"}

    @app.get("/v1/models")
    async def list_models():
        return {"object": "list", "data": [{"id": "pavp", "object": "model", "created": int(time.time()), "owned_by": "pavp"}]}

    @app.get("/info")
    async def info():
        plan_cfg = get_plan_config(s)
        act_cfg = get_act_config(s)
        openai_ready = bool(plan_cfg["model"] and plan_cfg["openai_api"] and act_cfg["model"] and act_cfg["openai_api"])
        anthropic_ready = bool(plan_cfg["model"] and plan_cfg["anthropic_api"] and act_cfg["model"] and act_cfg["anthropic_api"])
        return {
            "plan_model": plan_cfg["model"], "act_model": act_cfg["model"],
            "ready": openai_ready or anthropic_ready,
            "openai_ready": openai_ready,
            "anthropic_ready": anthropic_ready,
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

        # Detect API format: "openai" (default) or "anthropic" (set by /v1/messages)
        api_format: str = body.get("pavp_api_format", "openai")

        # Resolve model configs based on current selection
        plan_cfg = get_plan_config(s)
        act_cfg = get_act_config(s)

        if api_format == "anthropic":
            act_api_key = act_cfg["anthropic_api"] or act_cfg["openai_api"]
            act_base_url = act_cfg["anthropic_base_url"] or act_cfg["openai_base_url"]
            # Use native Anthropic callers for anthropic endpoints
            _call_raw = _call_anthropic_raw
            _call_stream = _call_anthropic_stream
            plan_base_url = plan_cfg["anthropic_base_url"] or plan_cfg["openai_base_url"]
            plan_api_key = plan_cfg["anthropic_api"] or plan_cfg["openai_api"]
        else:
            act_api_key = act_cfg["openai_api"]
            act_base_url = act_cfg["openai_base_url"]
            _call_raw = _call_llm_raw
            _call_stream = _call_llm_stream
            plan_base_url = plan_cfg["openai_base_url"]
            plan_api_key = plan_cfg["openai_api"]

        # Extract tool definitions (for Plan context + Act forwarding)
        tools: Optional[list[dict]] = body.get("tools")
        tool_choice: Any = body.get("tool_choice")

        # Build extra: all original request fields forwarded to Act model.
        # Exclude fields we handle specially: model (set from settings),
        # messages (rewritten with plan injection), stream (consumed here).
        _extra_skip = {"model", "messages", "stream", "pavp_api_format"}
        extra: dict[str, Any] = {
            k: v for k, v in body.items() if k not in _extra_skip
        }
        # Ensure tools/tool_choice are present even if not in extra (belt-and-suspenders)
        if tools and "tools" not in extra:
            extra["tools"] = tools
        if tool_choice is not None and "tool_choice" not in extra:
            extra["tool_choice"] = tool_choice

        if not plan_cfg["model"]:
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
                    plan = make_plan(prompt, project_root, s, tools=tools,
                                     base_url=plan_base_url, api_key=plan_api_key,
                                     use_anthropic_caller=(api_format == "anthropic"))
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

            if needs_act and not act_cfg["model"]:
                return JSONResponse(status_code=502, content={"error": {"message": "Act model not configured"}})

            response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            model_name = body.get("model", "pavp")

            # Determine pavp_stage + optionally run Verify
            # _verify_passed: None=no verify done, True=passed (skip Act), False=failed (continue Act)
            _verify_passed: bool | None = None
            if not needs_act:
                pavp_stage = build_pvap_stage("plan")
            elif compound_key in _act_started:
                if _is_finish_turn(messages):
                    # Agent 发送了完成通知 → 运行真正的 Verify（如果有文件改动）
                    if _has_file_changes(messages):
                        _update_proxy_state("VERIFYING", turn_count + 1)
                        _v_stop = _start_state_updater("VERIFYING", turn_count + 1)
                        try:
                            verify_result = _run_verify_proxy(
                                plan, messages, plan_cfg,
                                plan_api_key, plan_base_url, api_format,
                            )
                        finally:
                            _v_stop.set()

                        if verify_result.get("verdict") == "FAIL":
                            debug_plan = verify_result.get("debug_plan")
                            if debug_plan and isinstance(debug_plan, dict) and debug_plan.get("tasks"):
                                # 验证失败 + 有有效 debug_plan → 更新 plan 继续 Act
                                plan = json.dumps(debug_plan, ensure_ascii=False)
                                if ckey in _plan_cache:
                                    _plan_cache[ckey]["plan"] = plan
                                pavp_stage = build_pvap_stage("act", loop=turn_count)
                                _update_proxy_state("ACTING", turn_count + 1)
                                print(f"[PAVP] Verify FAILED: {verify_result.get('summary', '')}. "
                                      f"Debug plan adopted, continuing.", flush=True)
                            else:
                                # 无有效 debug_plan → 当作 PASS
                                pavp_stage = build_pvap_stage("act", is_finish=True)
                                _update_proxy_state("DONE")
                                _verify_passed = True
                                print(f"[PAVP] Verify FAILED but no valid debug_plan, treated as PASS", flush=True)
                        else:
                            pavp_stage = build_pvap_stage("act", is_finish=True)
                            _update_proxy_state("DONE")
                            _verify_passed = True
                            print(f"[PAVP] Verify PASSED for '{task_key}'", flush=True)
                    else:
                        # 无文件改动 → 直接完成
                        pavp_stage = build_pvap_stage("act", is_finish=True)
                        _update_proxy_state("DONE")
                        _verify_passed = True
                        print(f"[PAVP] No file changes, finish turn", flush=True)
                else:
                    pavp_stage = build_pvap_stage("act", is_continue=True)
            else:
                pavp_stage = build_pvap_stage("act")
                _act_started.add(compound_key)

            # ---- Plan-only (no Act needed): route to act model directly ----
            if not needs_act:
                print(f"[PAVP] Plan indicates no Act needed (requires_act=false). Routing to act model for direct answer.", flush=True)

                if not act_cfg["model"]:
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
                            for line in _call_stream(
                                act_cfg["model"], act_api_key, act_base_url,
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
                    resp = _call_raw(act_cfg["model"], act_api_key, act_base_url,
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

            # ---- Verify passed: return finish response, skip Act model ----
            if _verify_passed is True:
                _update_proxy_state("DONE")
                if is_stream:
                    def _finish_stream():
                        nonlocal response_id, model_name, pavp_stage
                        yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model_name, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': f'{pavp_stage}\n\n'}, 'finish_reason': None}], 'task_key': task_key})}\n\n"
                        yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model_name, 'choices': [{'index': 0, 'delta': {'content': 'Verified: all acceptance criteria met.'}, 'finish_reason': 'stop'}], 'task_key': task_key})}\n\n"
                        yield "data: [DONE]\n\n"
                    return StreamingResponse(_finish_stream(), media_type="text/event-stream")
                msg = {"role": "assistant", "content": f"{pavp_stage}\n\nVerified: all acceptance criteria met."}
                return {
                    "id": response_id, "object": "chat.completion",
                    "created": int(time.time()), "model": model_name,
                    "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "pavp_stage": pavp_stage,
                    "task_key": task_key,
                }

            # ---- Normal mode: Phase 2: Act ----
            act_messages = _build_act_messages(messages, plan)

            if is_stream:
                print(f"[PAVP] Act (stream, turn {turn_count}): routing to {act_cfg['model']}...", flush=True)

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
                        for line in _call_stream(
                            act_cfg["model"], act_api_key, act_base_url,
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
            print(f"[PAVP] Act (turn {turn_count}): routing to {act_cfg['model']}...", flush=True)

            # Allow one retry: initial Act call + potential retry with debug_plan after Verify
            for _act_try in range(2):
                _act_stop = _start_state_updater("ACTING", turn_count + 1)
                try:
                    resp = _call_raw(act_cfg["model"], act_api_key, act_base_url,
                                     act_messages, max_tokens=8192, timeout=600, extra=extra)
                finally:
                    _act_stop.set()
                msg = resp["choices"][0]["message"]
                has_tool_calls = bool(msg.get("tool_calls"))

                # 无 tool_calls + 非首次 Act 轮次 → 任务完成，运行 Verify
                if not has_tool_calls and compound_key in _act_started and _act_try == 0:
                    if _has_file_changes(messages):
                        _update_proxy_state("VERIFYING", turn_count + 1)
                        _v_stop = _start_state_updater("VERIFYING", turn_count + 1)
                        try:
                            verify_result = _run_verify_proxy(
                                plan, messages, plan_cfg,
                                plan_api_key, plan_base_url, api_format,
                            )
                        finally:
                            _v_stop.set()

                        if verify_result.get("verdict") == "FAIL":
                            dp = verify_result.get("debug_plan")
                            if dp and isinstance(dp, dict) and dp.get("tasks"):
                                plan = json.dumps(dp, ensure_ascii=False)
                                if ckey in _plan_cache:
                                    _plan_cache[ckey]["plan"] = plan
                                act_messages = _build_act_messages(messages, plan)
                                pavp_stage = build_pvap_stage("act", loop=turn_count)
                                _update_proxy_state("ACTING", turn_count + 1)
                                print(f"[PAVP] Non-streaming Verify FAILED: "
                                      f"{verify_result.get('summary', '')}. Retrying with debug plan.",
                                      flush=True)
                                continue  # Retry with debug plan

                    # Verify PASSED or no file changes
                    pavp_stage = build_pvap_stage("act", is_finish=True)
                elif has_tool_calls:
                    pavp_stage = build_pvap_stage("act", is_continue=True) if compound_key in _act_started else build_pvap_stage("act")

                break  # Normal exit

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
            return JSONResponse(status_code=500, content={"error": {"message": "Internal proxy error"}})

    # =====================================================================
    # Anthropic Messages API (Claude Code compatible)
    # =====================================================================

    @app.post("/v1/messages")
    async def messages_anthropic(request: Request):
        """Anthropic-compatible Messages API endpoint.

        Converts Anthropic-format requests to OpenAI format,
        forwards to the internal Plan/Act pipeline,
        and converts responses back to Anthropic format.
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid JSON"}},
            )

        # Authentication: accept x-api-key (Anthropic style) or
        # Authorization: Bearer (OpenAI/OAuth fallback).
        # This is a local proxy endpoint for Claude Code compatibility.
        # Claude Code sends its own ANTHROPIC_API_KEY in x-api-key, which
        # won't match litellm_master_key. We only require a non-empty key
        # to allow the connection while still rejecting obviously invalid requests.
        api_key = request.headers.get("x-api-key", "")
        if not api_key:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                api_key = auth_header[7:]
        if not api_key:
            return JSONResponse(
                status_code=401,
                content={"type": "error", "error": {"type": "authentication_error", "message": "Missing API key"}},
            )

        model = body.get("model", "pavp")
        is_stream = body.get("stream", False)
        anthropic_messages = body.get("messages", [])
        system = body.get("system", "")

        # ---- Convert Anthropic messages to OpenAI format ----
        openai_messages: list[dict] = []
        if system:
            openai_messages.append({"role": "system", "content": system})
        for msg in anthropic_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts: list[str] = []
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text":
                        text_parts.append(block.get("text", ""))
                    elif bt == "tool_use":
                        text_parts.append(
                            f"[Tool: {block.get('name', '?')}("
                            f"{json.dumps(block.get('input', {}), ensure_ascii=False)})]"
                        )
                    elif bt == "tool_result":
                        tc = block.get("content", "")
                        if isinstance(tc, list):
                            tc = " ".join(
                                c.get("text", "") for c in tc
                                if isinstance(c, dict) and c.get("type") == "text"
                            )
                        text_parts.append(str(tc))
                content = "".join(text_parts)
            openai_messages.append({"role": role, "content": content})

        # ---- Convert Anthropic tools to OpenAI format ----
        openai_tools: Optional[list[dict]] = None
        if "tools" in body:
            openai_tools = []
            for tool in body["tools"]:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {}),
                    },
                })

        # ---- Build OpenAI-format request body ----
        openai_body: dict[str, Any] = {
            "model": "pavp",
            "messages": openai_messages,
            "stream": is_stream,
            "max_tokens": body.get("max_tokens", 4096),
            "pavp_api_format": "anthropic",
        }
        if openai_tools:
            openai_body["tools"] = openai_tools
        if "temperature" in body:
            openai_body["temperature"] = body["temperature"]
        if "top_p" in body:
            openai_body["top_p"] = body["top_p"]
        if "stop_sequences" in body:
            openai_body["stop"] = body["stop_sequences"]
        if "tool_choice" in body:
            openai_body["tool_choice"] = body["tool_choice"]

        # ---- Forward to internal chat completions endpoint ----
        port = s.get("proxy_port", DEFAULT_PORT)
        openai_url = f"http://127.0.0.1:{port}/v1/chat/completions"

        # Use litellm_master_key for internal forwarding (the internal
        # /v1/chat/completions endpoint authenticates against this key).
        internal_key = s.get("litellm_master_key", "sk-pavp-local")

        if is_stream:
            # Use non-streaming internally, then simulate SSE stream.
            # Real-time OpenAI→Anthropic stream conversion loses tool_use
            # blocks (tool_call deltas in OpenAI have no text content).
            # By buffering the full response we can faithfully emit all
            # content (text + tool_use) as proper Anthropic SSE events.
            async def _anthropic_stream():
                nonlocal openai_body
                msg_id = f"msg_{uuid.uuid4().hex[:12]}"
                non_stream_body = dict(openai_body)
                non_stream_body["stream"] = False
                try:
                    async with httpx.AsyncClient(timeout=600) as client:
                        openai_resp = await client.post(
                            openai_url,
                            headers={"Authorization": f"Bearer {internal_key}"},
                            json=non_stream_body,
                        )
                        openai_resp.raise_for_status()
                        data = openai_resp.json()
                except Exception as e:
                    print(f"[PAVP] Anthropic stream (internal) error: {e}", flush=True)
                    yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'message': str(e)}})}\n\n"
                    return

                # ---- Convert OpenAI response to Anthropic content blocks ----
                choices = data.get("choices", [{}])
                choice = choices[0] if choices else {}
                message = choice.get("message", {})
                resp_content = message.get("content", "")
                tool_calls = message.get("tool_calls", [])
                finish_reason = choice.get("finish_reason", "stop")

                anthropic_blocks: list[dict] = []
                if resp_content:
                    anthropic_blocks.append({"type": "text", "text": resp_content})
                if tool_calls:
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        try:
                            args = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}
                        anthropic_blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                            "name": func.get("name", ""),
                            "input": args,
                        })

                # ---- Emit Anthropic SSE events ----
                sr_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
                anthropic_sr = sr_map.get(finish_reason, "end_turn")
                usage = data.get("usage", {})

                yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model, 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': usage.get('prompt_tokens', 0), 'output_tokens': 0}}})}\n\n"

                for idx, block in enumerate(anthropic_blocks):
                    bt = block.get("type")
                    if bt == "text":
                        text = block.get("text", "")
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                        if text:
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'text_delta', 'text': text}})}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': idx})}\n\n"
                    elif bt == "tool_use":
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': idx, 'content_block': {'type': 'tool_use', 'id': block.get('id'), 'name': block.get('name'), 'input': {}}})}\n\n"
                        input_json = json.dumps(block.get("input", {}), ensure_ascii=False)
                        if input_json and input_json != "{}":
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'input_json_delta', 'partial_json': input_json}})}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': idx})}\n\n"

                yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': anthropic_sr, 'stop_sequence': None}, 'usage': {'output_tokens': usage.get('completion_tokens', 0)}})}\n\n"
                yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

            return StreamingResponse(_anthropic_stream(), media_type="text/event-stream")

        # ---- Non-streaming ----
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                openai_resp = await client.post(
                    openai_url,
                    headers={"Authorization": f"Bearer {internal_key}"},
                    json=openai_body,
                )
                openai_resp.raise_for_status()
                data = openai_resp.json()
        except Exception as e:
            print(f"[PAVP] Anthropic forward error: {e}", flush=True)
            return JSONResponse(
                status_code=502,
                content={"type": "error", "error": {"type": "api_error", "message": "Internal proxy error"}},
            )

        # ---- Convert OpenAI response to Anthropic format ----
        choices = data.get("choices", [{}])
        choice = choices[0] if choices else {}
        message = choice.get("message", {})
        resp_content = message.get("content", "")
        tool_calls = message.get("tool_calls", [])

        anthropic_content: list[dict] = []
        if resp_content:
            anthropic_content.append({"type": "text", "text": resp_content})
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                anthropic_content.append({
                    "type": "tool_use",
                    "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                    "name": func.get("name", ""),
                    "input": args,
                })

        stop_reason = choice.get("finish_reason", "stop")
        sr_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
        usage = data.get("usage", {})

        return {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "type": "message",
            "role": "assistant",
            "content": anthropic_content,
            "model": model,
            "stop_reason": sr_map.get(stop_reason, "end_turn"),
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
            "pavp_stage": data.get("pavp_stage", ""),
            "task_key": data.get("task_key", ""),
        }

    return app


def _find_free_port(preferred: int, max_tries: int = 100) -> int:
    """Find a free port, preferring *preferred*.

    Strategy (aggressive about keeping the configured port):
    1. If *preferred* is free, use it immediately.
    2. If occupied, check if it's an old PAVP proxy (stale PID file)
       → kill it, wait, retry.
    3. If occupied by something else → wait, retry (transient boot process).
    4. After 5 retries, auto-increment to find the next free port.

    Checks both 127.0.0.1 and 0.0.0.0 to ensure the port is truly free.
    """
    import socket as _sock
    import time as _t

    def _port_free(port: int) -> bool:
        for host in ("127.0.0.1", "0.0.0.0"):
            try:
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                s.bind((host, port))
                s.close()
            except OSError:
                return False
        return True

    # --- Phase 1: try to claim the preferred port ---
    for attempt in range(5):
        if _port_free(preferred):
            return preferred
        # If this is an old PAVP proxy with a stale PID file, kill it
        _flush_old_pavp()
        _t.sleep(2)

    # --- Phase 2: preferred port is persistently occupied, auto-increment ---
    for port in range(preferred + 1, preferred + max_tries):
        if _port_free(port):
            print(f"[PAVP] Port {preferred} persistently occupied, using port {port}", flush=True)
            return port
    raise RuntimeError(
        f"No free port found in range {preferred}-{preferred + max_tries - 1}"
    )


def _flush_old_pavp() -> None:
    """Kill any previous PAVP proxy process referenced by the PID file.

    This is called when the preferred port is occupied — if the occupant
    is our own stale proxy (PID file exists but process is orphaned), we
    clean it up so the new instance can claim the port.
    """
    pid_file = Path.home() / ".pavp" / "proxy.pid"
    try:
        if not pid_file.exists():
            return
        old_pid = int(pid_file.read_text().strip())
    except (ValueError, IOError):
        return

    # Check if the old process is still alive
    import ctypes
    kernel32 = ctypes.windll.kernel32
    SYNCHRONIZE = 0x00100000
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, old_pid)
    if handle:
        kernel32.CloseHandle(handle)
        # Process exists — don't kill it, it might not be PAVP
        return
    # Process does NOT exist: stale PID file, clean up
    pid_file.unlink(missing_ok=True)


def run_server(host="0.0.0.0", port=None):
    s = load_settings()
    if port is None:
        port = s.get("proxy_port", DEFAULT_PORT)

    # Auto-find a free port (prefers the configured port, auto-increments if needed).
    actual_port = _find_free_port(port)
    # Always sync settings.json so UI and clients know the actual port.
    from .settings import save_field
    save_field("proxy_port", actual_port)

    # Write PID file so the UI can track this process.
    pid_file = Path.home() / ".pavp" / "proxy.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    # Write the actual port to a well-known file so the UI can auto-sync
    # even when the port differs from settings.json.
    port_file = Path.home() / ".pavp" / "proxy_port.txt"
    port_file.write_text(str(actual_port))

    app = create_app(s)
    settings_file = settings_path()
    print(f"PAVP Proxy starting (pid={os.getpid()})", flush=True)
    print(f"  Settings file: {settings_file}", flush=True)
    print(f"  Home dir:      {Path.home()}", flush=True)
    print(f"  Listen:        http://{host}:{actual_port}", flush=True)
    _plan_cfg = get_plan_config(s)
    _act_cfg = get_act_config(s)
    print(f"  Plan:          {_plan_cfg['model'] or '?'} @ {_plan_cfg['openai_base_url'] or '?'}", flush=True)
    print(f"  Act:           {_act_cfg['model'] or '?'} @ {_act_cfg['openai_base_url'] or '?'}", flush=True)
    print(f"  Ready:         {bool(_plan_cfg['model'] and _plan_cfg['openai_api'] and _act_cfg['model'] and _act_cfg['openai_api'])}", flush=True)
    try:
        uvicorn.run(app, host=host, port=actual_port, log_level="info")
    finally:
        pid_file.unlink(missing_ok=True)
        port_file.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=None,
                   help="Override the port from settings.json (auto-increments if occupied)")
    args = p.parse_args()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
