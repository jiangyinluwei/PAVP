"""PAVP Engine - Plan -> Act -> Verify

Plan: plan model generates plan (internal, not returned to agent)
Act: act model receives full context (agent messages + plan), output flows to agent
     Act output may include tool_calls - agent executes, then sends next request
Verify: (future) triggered by conversation state
"""
from __future__ import annotations

import json
import time as _time_module
import uuid
from typing import Any, Generator, Optional

import httpx

from .settings import load as load_settings

PLAN_SYSTEM = """You are a planner. Analyze the requirement, evaluate available tools, and output a structured plan as JSON.

Rules:
1. Only output the plan. Do NOT write code.
2. Output valid JSON following this schema:
   {
     "task_key": "TK-a1b2c3d4",
     "summary": "one-line plan summary",
     "reasoning": "Thinking process: requirement analysis, tech selection, trade-offs, risks",
     "tech_stack": ["Python 3.10+", "FastAPI", "SQLAlchemy 2.0"],
     "requires_act": true,
     "tasks": [
       {
         "id": "T1",
         "title": "task title",
         "file_paths": ["src/foo.py"],
         "entry_points": "Entry point: foo() method in FooBar class (lines 45-78)",
         "tech_stack": ["tech stack for this task"],
         "implementation_logic": "Implementation logic: use Strategy pattern...",
         "acceptance_criteria": ["verifiable criterion"],
         "depends_on": []
       }
     ]
   }
3. Set "requires_act": false when the user only wants analysis, explanation, review, planning, or Q&A — anything that does NOT require modifying files.
   Set "requires_act": true only when the user explicitly asks to write, modify, create, fix, or implement code.
4. TOOL-AWARE PLANNING: When agent-calling tools (Task, subagent, delegate, etc.) are available:
   - Break complex tasks into independent subtasks that can run in parallel via sub-agents
   - Mark each task with a "suggested_tool" field naming the best tool for that task (optional)
   - For tasks suited for sub-agents, describe what the sub-agent should do in the title
   - Tasks that depend on each other should be sequenced; independent tasks can be dispatched concurrently
5. If unclear: {"summary": "NEEDS_CLARIFICATION: <what is missing>", "requires_act": false, "tasks": []}
6. [REASONING]: Include a "reasoning" field in the output JSON detailing your thought process:
   - Requirement analysis and understanding
   - Technology selection rationale (why choose a particular framework/library/tool)
   - Trade-off analysis (pros and cons of different approaches)
   - Potential risks and mitigation strategies
7. [DETAIL LEVEL]: The plan must be detailed enough, with each task specifying:
   - "entry_points" field: the code modification entry point (file, class, function, line range)
   - "tech_stack" field: the tech stack involved (framework, library, tool, version)
   - "implementation_logic" field: the implementation logic (algorithm, data structure, design pattern, core flow)
   - Top-level "tech_stack" field summarizes the overall tech stack
8. [TASK KEY]: Every new task MUST include a unique "task_key" field (e.g., "TK-a1b2c3d4").
   The task_key is a short identifier that anchors the plan across multiple execution turns.
   It must be unique for each new task. Use random 8-character hex suffix after "TK-".
   The task_key field goes at the top level of the JSON output.
   When the Act model receives this plan, it will use the task_key to maintain context continuity.
"""

def build_pvap_stage(
    stage: str,
    *,
    loop: int = 0,
    is_continue: bool = False,
    is_finish: bool = False,
) -> str:
    """Build the PAVP flow stage field for external agent communication.

    Returns a string like "pvap Act..." or "pvap Loop 1..." that can be
    prepended to system prompts to inform the agent of the current PAVP phase.

    Args:
        stage: Current PAVP phase - "plan", "act", "verify"
        loop: Loop iteration count (0 = first run, no loop field displayed)
        is_continue: Whether this is a continuation of previous context
        is_finish: Whether the workflow is complete
    """
    if is_finish:
        return "pvap Finish"
    if is_continue:
        return "pvap Continue..."
    if stage == "plan":
        return "pvap Plan..."
    if stage == "verify":
        return "pvap Verify..."
    if stage == "act":
        if loop > 0:
            return f"pvap Loop {loop}..."
        return "pvap Start..."
    return ""


def _call_llm_with_retry(api_call, max_retries: int = 5, base_delay: float = 2.0):
    """Wrap an LLM API call with exponential-backoff retry for transient errors.

    Retries on:
      - httpx.ConnectError / httpx.TimeoutException (network not ready)
      - httpx.HTTPStatusError with 5xx status codes
    Gives up immediately on 4xx errors (bad request / auth).
    """
    for attempt in range(max_retries):
        try:
            return api_call()
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise  # 4xx: don't retry
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            _time_module.sleep(delay)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            _time_module.sleep(delay)


def _call_llm_raw(
    model: str, api_key: str, base_url: str,
    messages: list[dict],
    *, temperature: float = 0.2, max_tokens: int = 4096,
    timeout: float = 300.0,
    extra: Optional[dict[str, Any]] = None,
) -> dict:
    """Call LLM and return full response dict (may include tool_calls).

    extra: additional payload fields (tools, tool_choice, top_p, etc.)
           merged into the request body after model/messages/temperature/max_tokens.
    """
    if _is_anthropic(base_url):
        return _call_anthropic_raw(model, api_key, base_url, messages,
                                   temperature=temperature, max_tokens=max_tokens,
                                   timeout=timeout, extra=extra)

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra:
        payload.update(extra)

    def _do_call():
        r = httpx.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=timeout)
        r.raise_for_status()
        return r

    r = _call_llm_with_retry(_do_call)
    return r.json()


def _call_llm_stream(
    model: str, api_key: str, base_url: str,
    messages: list[dict],
    *, temperature: float = 0.2, max_tokens: int = 4096,
    timeout: float = 600.0,
    extra: Optional[dict[str, Any]] = None,
    max_retries: int = 5,
    base_delay: float = 2.0,
):
    """Call LLM with streaming enabled. Yields raw SSE lines (bytes) as they arrive.

    Retries on connection failure (common during boot before network is up).

    extra: additional payload fields (tools, tool_choice, top_p, etc.)
           merged into the request body.
    """
    if _is_anthropic(base_url):
        yield from _call_anthropic_stream(model, api_key, base_url, messages,
                                          temperature=temperature, max_tokens=max_tokens,
                                          timeout=timeout, extra=extra,
                                          max_retries=max_retries, base_delay=base_delay)
        return

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if extra:
        payload.update(extra)

    last_error = None
    for attempt in range(max_retries):
        try:
            with httpx.stream("POST", url,
                              headers={"Authorization": f"Bearer {api_key}"},
                              json=payload, timeout=timeout) as resp:
                resp.raise_for_status()
                yield from resp.iter_lines()
            return  # success
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            last_error = e
            if attempt < max_retries - 1:
                _time_module.sleep(base_delay * (2 ** attempt))
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise
            last_error = e
            if attempt < max_retries - 1:
                _time_module.sleep(base_delay * (2 ** attempt))
    raise last_error  # type: ignore[misc]


def _call_llm_text(
    model: str, api_key: str, base_url: str,
    messages: list[dict],
    *, temperature: float = 0.2, max_tokens: int = 4096,
    timeout: float = 300.0,
) -> str:
    """Call LLM with json_object format"""
    if _is_anthropic(base_url):
        return _call_anthropic_text(model, api_key, base_url, messages,
                                    temperature=temperature, max_tokens=max_tokens,
                                    timeout=timeout)

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    def _do_call():
        r = httpx.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=timeout)
        r.raise_for_status()
        return r

    r = _call_llm_with_retry(_do_call)
    return r.json()["choices"][0]["message"]["content"]


# =====================================================================
# Anthropic Native API Support
# =====================================================================


def _is_anthropic(base_url: str) -> bool:
    """Detect if the base URL points to an Anthropic API endpoint."""
    return "anthropic.com" in base_url.lower().rstrip("/")


def _anthropic_messages_url(base_url: str) -> str:
    """Build the Anthropic messages endpoint URL from a base URL."""
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        return f"{url}/messages"
    if url.endswith("/messages"):
        return url
    return f"{url}/v1/messages"


def _convert_to_anthropic_messages(
    openai_messages: list[dict],
) -> tuple[Optional[str], list[dict]]:
    """Convert OpenAI-format messages to Anthropic-format messages.

    Anthropic uses a separate ``system`` parameter and does not support
    a ``system`` role inside the messages array.  Tool / function messages
    are dropped since Anthropic handles them differently.

    Returns (system_prompt, anthropic_messages).
    """
    system = None
    messages: list[dict] = []
    for msg in openai_messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            existing = system or ""
            text = content if isinstance(content, str) else str(content)
            system = f"{existing}\n{text}".strip() if existing else text
        elif role in ("user", "assistant"):
            text = content if isinstance(content, str) else str(content)
            messages.append({"role": role, "content": text})
        # tool / function roles are deliberately dropped – Anthropic
        # expects tool_results inside a user message via content blocks.
    return system, messages


def _convert_tools_to_anthropic(openai_tools: list[dict]) -> list[dict]:
    """Convert OpenAI tool definitions to Anthropic tool format."""
    anthropic_tools = []
    for tool in openai_tools:
        func = tool.get("function", tool)
        name = func.get("name", "")
        desc = func.get("description", "")
        params = func.get("parameters", {})
        anthropic_tools.append({
            "name": name,
            "description": desc,
            "input_schema": params,
        })
    return anthropic_tools


def _anthropic_response_to_openai(
    anthropic_resp: dict, model: str,
) -> dict:
    """Convert an Anthropic API response dict to OpenAI-compatible format."""
    content_blocks = anthropic_resp.get("content", [])
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in content_blocks:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })

    usage = anthropic_resp.get("usage", {})
    return {
        "id": anthropic_resp.get("id", f"msg-{uuid.uuid4().hex[:12]}"),
        "object": "chat.completion",
        "created": int(_time_module.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "".join(text_parts),
            },
            "finish_reason": _anthropic_stop_reason(anthropic_resp.get("stop_reason")),
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


def _anthropic_stop_reason(stop_reason: Optional[str]) -> str:
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }
    return mapping.get(stop_reason or "", "stop")


def _call_anthropic_raw(
    model: str, api_key: str, base_url: str,
    messages: list[dict],
    *, temperature: float = 0.2, max_tokens: int = 4096,
    timeout: float = 300.0,
    extra: Optional[dict[str, Any]] = None,
) -> dict:
    """Call Anthropic API (non-streaming) and return OpenAI-compatible dict."""
    url = _anthropic_messages_url(base_url)
    system, anthropic_messages = _convert_to_anthropic_messages(messages)

    payload: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        payload["system"] = system
    if extra:
        for k in ("metadata", "stop_sequences", "top_p", "top_k"):
            if k in extra:
                payload[k] = extra[k]
        if "tools" in extra:
            payload["tools"] = _convert_tools_to_anthropic(extra["tools"])

    def _do_call():
        r = httpx.post(
            url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        return r

    r = _call_llm_with_retry(_do_call)
    return _anthropic_response_to_openai(r.json(), model)


def _call_anthropic_stream(
    model: str, api_key: str, base_url: str,
    messages: list[dict],
    *, temperature: float = 0.2, max_tokens: int = 4096,
    timeout: float = 600.0,
    extra: Optional[dict[str, Any]] = None,
    max_retries: int = 5,
    base_delay: float = 2.0,
) -> Generator[bytes, None, None]:
    """Call Anthropic API with streaming.

    Yields OpenAI-compatible SSE lines (bytes) so the caller can treat
    the output identically to ``_call_llm_stream``.
    """
    url = _anthropic_messages_url(base_url)
    system, anthropic_messages = _convert_to_anthropic_messages(messages)

    payload: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if system:
        payload["system"] = system
    if extra:
        for k in ("metadata", "stop_sequences", "top_p", "top_k"):
            if k in extra:
                payload[k] = extra[k]
        if "tools" in extra:
            payload["tools"] = _convert_tools_to_anthropic(extra["tools"])

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            with httpx.stream(
                "POST", url,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                yield from _anthropic_stream_to_openai_sse(model, resp.iter_lines())
            return
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            last_error = e
            if attempt < max_retries - 1:
                _time_module.sleep(base_delay * (2 ** attempt))
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise
            last_error = e
            if attempt < max_retries - 1:
                _time_module.sleep(base_delay * (2 ** attempt))
    raise last_error  # type: ignore[misc]


def _call_anthropic_text(
    model: str, api_key: str, base_url: str,
    messages: list[dict],
    *, temperature: float = 0.2, max_tokens: int = 4096,
    timeout: float = 300.0,
) -> str:
    """Call Anthropic API and return the text content.

    Unlike ``_call_llm_text``, Anthropic does not support a native
    ``response_format`` parameter.  The caller should ensure the prompt
    instructs JSON output when needed.
    """
    url = _anthropic_messages_url(base_url)
    system, anthropic_messages = _convert_to_anthropic_messages(messages)

    payload: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        payload["system"] = system

    def _do_call():
        r = httpx.post(
            url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        return r

    r = _call_llm_with_retry(_do_call)
    data = r.json()
    text_parts = [
        block["text"]
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    return "".join(text_parts)


def _anthropic_stream_to_openai_sse(
    model: str,
    anthropic_lines: Generator[bytes, None, None],
) -> Generator[bytes, None, None]:
    """Convert Anthropic SSE event stream to OpenAI-compatible SSE lines.

    Each yielded value is a bytes string like ``b"data: {...}\\n\\n"``.
    """
    response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    role_sent = False
    pending_event: Optional[str] = None

    for raw_line in anthropic_lines:
        line = raw_line if isinstance(raw_line, bytes) else str(raw_line).encode("utf-8")
        text = line.decode("utf-8", errors="replace").strip()

        # Track event type (next line after "event: <type>" is "data: ...")
        if text.startswith("event: "):
            pending_event = text[7:]
            continue

        if not text.startswith("data: "):
            pending_event = None
            continue

        data_str = text[6:]
        try:
            event_data = json.loads(data_str)
        except json.JSONDecodeError:
            pending_event = None
            continue

        event_type = pending_event or event_data.get("type", "")
        pending_event = None

        if event_type == "content_block_delta":
            delta = event_data.get("delta", {})
            if delta.get("type") == "text_delta":
                chunk: dict[str, Any] = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(_time_module.time()),
                    "model": model,
                    "choices": [{
                        "index": event_data.get("index", 0),
                        "delta": {},
                        "finish_reason": None,
                    }],
                }
                if not role_sent:
                    chunk["choices"][0]["delta"]["role"] = "assistant"
                    role_sent = True
                chunk["choices"][0]["delta"]["content"] = delta.get("text", "")
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

        elif event_type == "message_delta":
            msg_delta = event_data.get("delta", {})
            stop_reason = msg_delta.get("stop_reason")
            chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(_time_module.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": _anthropic_stop_reason(stop_reason) if stop_reason else None,
                }],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

        elif event_type == "message_stop":
            yield b"data: [DONE]\n\n"

        # message_start, content_block_start, content_block_stop, ping are ignored


# Keywords that indicate a tool can spawn/delegate to sub-agents
_AGENT_DELEGATION_KEYWORDS = (
    "subagent", "sub-agent", "sub_agent",
    "delegate", "task", "agent", "spawn",
    "parallel", "dispatch",
)


def _is_agent_delegation_tool(tool: dict) -> bool:
    """Check if a tool is an agent-delegation tool (can spawn sub-agents)."""
    func = tool.get("function", tool)
    name = (func.get("name", "") or "").lower()
    desc = (func.get("description", "") or "").lower()
    combined = f"{name} {desc}"
    return any(kw in combined for kw in _AGENT_DELEGATION_KEYWORDS)


def _describe_tools(tools: list[dict]) -> str:
    """Convert tool definitions to a human-readable summary for the Plan prompt.

    Agent-delegation tools are highlighted with usage guidance so the planner
    can break tasks into sub-agent-sized units for parallel execution.
    """
    if not tools:
        return ""
    delegation_tools = []
    other_tools = []
    for t in tools:
        if _is_agent_delegation_tool(t):
            delegation_tools.append(t)
        else:
            other_tools.append(t)

    lines = ["## Available Tools"]

    if delegation_tools:
        lines.append("\n### Agent Delegation Tools (for dispatching subtasks to sub-agents)")
        lines.append("Use these to run independent tasks in parallel for better efficiency.")
        for t in delegation_tools:
            func = t.get("function", t)
            name = func.get("name", "?")
            desc = func.get("description", "")
            # Extract parameters to describe sub-agent types if available
            params = func.get("parameters", {})
            props = params.get("properties", {}) if isinstance(params, dict) else {}
            lines.append(f"  - **{name}**: {desc}")
            if props:
                for pname, pinfo in props.items():
                    if isinstance(pinfo, dict):
                        pdesc = pinfo.get("description", "")
                        if pname.lower() in ("subagent_type", "type", "agent_type"):
                            lines.append(f"    - `{pname}` options: {pdesc}")

    if other_tools:
        lines.append("\n### Other Tools")
        for t in other_tools:
            func = t.get("function", t)
            name = func.get("name", "?")
            desc = func.get("description", "")
            lines.append(f"  - {name}: {desc}")

    return "\n".join(lines)


def make_plan(prompt: str, project_root: str, settings: Optional[dict] = None,
              tools: Optional[list[dict]] = None) -> str:
    """Run only the Plan phase. Returns plan JSON string.

    tools: tool definitions from the agent request, injected as text context
           so the planner knows what capabilities are available.
    """
    s = settings or load_settings()
    tools_context = _describe_tools(tools) if tools else ""
    user_content = f"Project: {project_root}\n\nRequirement:\n{prompt}"
    if tools_context:
        user_content += f"\n\n{tools_context}"
    messages = [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    return _call_llm_text(s["plan_model"], s["plan_api"], s["plan_base_url"], messages,
                          max_tokens=4096)


def plan_requires_act(plan_json: str) -> bool:
    """Parse plan JSON and return whether Act phase is needed.

    Defaults to True if the field is missing (backward compatible).
    """
    try:
        data = json.loads(plan_json)
        return bool(data.get("requires_act", True))
    except (json.JSONDecodeError, TypeError):
        return True  # can't parse → assume act needed
