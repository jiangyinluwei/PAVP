"""PAVP Engine - Plan -> Act -> Verify

Plan: plan model generates plan (internal, not returned to agent)
Act: act model receives full context (agent messages + plan), output flows to agent
     Act output may include tool_calls - agent executes, then sends next request
Verify: (future) triggered by conversation state
"""
from __future__ import annotations

import json
import time as _time_module
from typing import Any, Optional

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
