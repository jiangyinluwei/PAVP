"""PAVP 编排器 - 状态机驱动 Plan->Act->Verify->(AwaitUser)->... 循环"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx

from .act_executor import run_act
from . import settings as pavp_settings
from .engine import _call_llm_text
from .models import (
    ActResult,
    Plan,
    SessionState,
    TaskItem,
    Verdict,
    VerifyResult,
)
from .prompts import (
    PLAN_SYSTEM,
    VERIFY_SYSTEM,
    build_plan_user_prompt,
    build_verify_user_prompt,
)

class LLMError(RuntimeError):
    """LLM 调用失败"""


_ANSWER_SYSTEM = """You are a helpful assistant. The user has asked a question or requested analysis/explanation.
Output a JSON object with a single field "answer" containing your response."""


# 事件回调类型: (event_name, payload_dict) -> None
EventHandler = Callable[[str, dict], None]

# 用户决策回调: 收到 VerifyResult(DO_NOT_SHIP)，返回 "continue" 或 "ignore"
DecideHandler = Callable[[VerifyResult, SessionState], str]

# 状态文件：供 Streamlit UI 实时读取当前 PAVP 框架状态
_STATE_FILE = Path.home() / ".pavp" / "current_state.json"


def _write_state_file(state: SessionState) -> None:
    """将当前编排器状态写入文件，供 UI 消费。"""
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "fsm_state": state.fsm_state,
        "iteration": state.iteration,
        "last_api_call": now,
        "updated_at": now,
    }
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class Orchestrator:
    """PAVP 工作流编排器。

    用法:
        orch = Orchestrator(project_root="/path/to/repo")
        state = orch.run("需求描述", on_event=print_event)
    """

    def __init__(self, project_root: str, workdir: Optional[str] = None):
        self.project_root = project_root
        self.workdir = workdir or project_root

    # -----------------------------------------------------------------
    # 主入口
    # -----------------------------------------------------------------
    def run(
        self,
        requirement: str,
        *,
        max_iterations: int = 3,
        loop_mode: Optional[str] = None,
        on_event: Optional[EventHandler] = None,
        decide: Optional[DecideHandler] = None,
    ) -> SessionState:
        """运行完整 PAVP 循环。

        Args:
            requirement: 原始需求
            max_iterations: Act-Verify 最大循环次数
            loop_mode: Loop 循环模式 "auto"(自动) / "manual"(手动)。
                       None 时从 settings.json 读取，默认 "auto"。
            on_event: 阶段事件回调（用于 UI/日志）
            decide: AWAITING_USER 时的决策回调，返回 "continue" / "ignore"。
                    不提供则默认 continue（自动继续）。
        """
        state = SessionState(
            session_id=uuid.uuid4().hex[:12],
            original_requirement=requirement,
            max_iterations=max_iterations,
        )
        # 读取 loop_mode（手动/自动）
        if loop_mode is None:
            try:
                loop_mode = pavp_settings.load().get("loop_mode", "auto")
            except Exception:
                loop_mode = "auto"

        # ---- Phase 1: Plan ----
        state.fsm_state = "PLANNING"
        self._emit(on_event, "plan_start", {"requirement": requirement}, state=state)
        try:
            plan = self._run_plan(state)
        except LLMError as e:
            state.fsm_state = "FAILED"
            self._emit(on_event, "failed", {"reason": str(e)}, state=state)
            return state

        state.current_plan = plan
        state.plan_history.append(plan)

        if plan.summary.startswith("NEEDS_CLARIFICATION"):
            state.fsm_state = "FAILED"
            self._emit(
                on_event,
                "clarification_needed",
                {"plan": plan.model_dump(mode="json")},
                state=state,
            )
            return state

        self._emit(
            on_event,
            "plan_done",
            {"plan": plan.model_dump(mode="json"), "task_count": len(plan.tasks)},
            state=state,
        )

        # ---- Plan-only (requires_act=false): generate answer, skip Act/Verify ----
        if not plan.requires_act:
            try:
                answer = self._run_answer(state)
            except LLMError:
                answer = plan.summary  # fallback to plan summary
            state.fsm_state = "DONE"
            self._emit(on_event, "done", {
                "reason": "plan_only",
                "plan_id": plan.plan_id,
                "answer": answer,
            }, state=state)
            return state

        # ---- Act -> Verify 循环 ----
        while True:
            state.iteration += 1
            state.fsm_state = "ACTING"
            self._emit(
                on_event,
                "act_start",
                {"iteration": state.iteration, "plan_id": state.current_plan.plan_id},
                state=state,
            )
            try:
                act = run_act(state.current_plan, self.project_root, self.workdir)
            except Exception as e:
                state.fsm_state = "FAILED"
                self._emit(on_event, "failed", {"reason": f"Act 异常: {e}"}, state=state)
                return state

            state.act_history.append(act)
            self._emit(
                on_event,
                "act_done",
                {
                    "iteration": state.iteration,
                    "success": act.success,
                    "files": act.files_changed,
                    "cost_usd": act.cost_usd,
                    "error": act.error,
                },
                state=state,
            )
            if not act.success:
                state.fsm_state = "FAILED"
                self._emit(on_event, "failed", {"reason": act.error}, state=state)
                return state

            # ---- Verify ----
            state.fsm_state = "VERIFYING"
            self._emit(on_event, "verify_start", {"iteration": state.iteration}, state=state)
            try:
                verify = self._run_verify(state)
            except LLMError as e:
                state.fsm_state = "FAILED"
                self._emit(on_event, "failed", {"reason": str(e)}, state=state)
                return state

            state.verify_history.append(verify)
            self._emit(
                on_event,
                "verify_done",
                {
                    "iteration": state.iteration,
                    "verdict": verify.verdict.value,
                    "issue_count": len(verify.issues),
                    "has_debug_plan": verify.debug_plan is not None,
                    "has_new_plan": verify.new_plan is not None,
                    "summary": verify.summary,
                },
                state=state,
            )

            # ---- 裁决分支 ----
            if verify.verdict == Verdict.PASS:
                state.fsm_state = "DONE"
                self._emit(on_event, "done", {"reason": "pass", "iterations": state.iteration}, state=state)
                return state

            if verify.verdict == Verdict.SHIP_WITH_FIXES:
                state.fsm_state = "DONE"
                self._emit(
                    on_event,
                    "done",
                    {"reason": "ship_with_fixes", "iterations": state.iteration},
                    state=state,
                )
                return state

            # DO_NOT_SHIP 或 INCOMPLETE -> 需要继续循环
            if state.iteration >= state.max_iterations:
                state.fsm_state = "FAILED"
                self._emit(
                    on_event,
                    "failed",
                    {"reason": f"达到最大迭代次数 {state.max_iterations}"},
                    state=state,
                )
                return state

            # 确定续接计划
            if verify.verdict == Verdict.DO_NOT_SHIP:
                next_plan = verify.debug_plan
                plan_event = "debug_plan_adopted"
                plan_label = "debug_plan"
            elif verify.verdict == Verdict.INCOMPLETE:
                next_plan = verify.new_plan
                plan_event = "new_plan_adopted"
                plan_label = "new_plan"
            else:
                state.fsm_state = "FAILED"
                self._emit(
                    on_event, "failed", {"reason": f"未知裁决: {verify.verdict}"}, state=state,
                )
                return state

            if next_plan is None:
                state.fsm_state = "FAILED"
                self._emit(
                    on_event,
                    "failed",
                    {"reason": f"{verify.verdict.value} 但 Verify 未输出 {plan_label}"},
                    state=state,
                )
                return state

            # 自动模式：直接继续；手动模式：等待用户决策
            if loop_mode == "auto":
                decision = "continue"
            else:
                state.fsm_state = "AWAITING_USER"
                self._emit(
                    on_event,
                    "awaiting_user",
                    {"iteration": state.iteration, "verify": verify.model_dump(mode="json")},
                    state=state,
                )
                if decide is None:
                    decision = "continue"
                else:
                    decision = decide(verify, state)

            if decision != "continue":
                state.fsm_state = "DONE"
                self._emit(
                    on_event,
                    "done",
                    {"reason": "user_ignore", "iterations": state.iteration},
                    state=state,
                )
                return state

            # 采用续接计划，继续循环
            next_plan.plan_id = uuid.uuid4().hex[:8]
            if verify.verdict == Verdict.DO_NOT_SHIP:
                next_plan.is_debug_plan = True
            state.current_plan = next_plan
            state.plan_history.append(next_plan)
            self._emit(
                on_event,
                plan_event,
                {"plan_id": next_plan.plan_id, "task_count": len(next_plan.tasks)},
                state=state,
            )

    # -----------------------------------------------------------------
    # 阶段实现
    # -----------------------------------------------------------------
    def _run_plan(self, state: SessionState) -> Plan:
        s = pavp_settings.load()
        _write_state_file(state)  # 刷新时间戳，防止 UI 超时显示 Standby
        try:
            raw = _call_llm_text(
                s["plan_model"], s["plan_api"], s["plan_base_url"],
                [
                    {"role": "system", "content": PLAN_SYSTEM},
                    {"role": "user", "content": build_plan_user_prompt(state.original_requirement, self.project_root)},
                ],
                temperature=0.2,
                max_tokens=4096,
            )
        except httpx.HTTPError as e:
            raise LLMError(str(e))
        data = _parse_json(raw)
        return Plan(
            plan_id=uuid.uuid4().hex[:8],
            is_debug_plan=False,
            summary=data.get("summary", ""),
            reasoning=data.get("reasoning", ""),
            tech_stack=data.get("tech_stack", []),
            requires_act=data.get("requires_act", True),
            root_cause=None,
            tasks=[TaskItem(**t) for t in data.get("tasks", [])],
        )

    def _run_answer(self, state: SessionState) -> str:
        """Generate a natural answer for Q&A requests (requires_act=false).

        Calls the plan model with a Q&A prompt to produce a direct answer
        instead of a structured plan.
        """
        s = pavp_settings.load()
        _write_state_file(state)  # 刷新时间戳，防止 UI 超时显示 Standby
        try:
            raw = _call_llm_text(
                s["plan_model"], s["plan_api"], s["plan_base_url"],
                [
                    {"role": "system", "content": _ANSWER_SYSTEM},
                    {"role": "user", "content": state.original_requirement},
                ],
                temperature=0.2,
                max_tokens=4096,
            )
        except httpx.HTTPError as e:
            raise LLMError(str(e))
        data = _parse_json(raw)
        return data.get("answer", "")

    def _run_verify(self, state: SessionState) -> VerifyResult:
        s = pavp_settings.load()
        _write_state_file(state)  # 刷新时间戳，防止 UI 超时显示 Standby
        try:
            raw = _call_llm_text(
                s["plan_model"], s["plan_api"], s["plan_base_url"],
                [
                    {"role": "system", "content": VERIFY_SYSTEM},
                    {"role": "user", "content": build_verify_user_prompt(
                        state.original_requirement,
                        state.current_plan,
                        state.act_history[-1],
                    )},
                ],
                temperature=0.1,
                max_tokens=8192,
            )
        except httpx.HTTPError as e:
            raise LLMError(str(e))
        data = _parse_json(raw)
        # 规范化 verdict
        v = str(data.get("verdict", "")).upper().replace("_", "-").replace(" ", "-")
        if v not in ("PASS", "SHIP-WITH-FIXES", "DO-NOT-SHIP", "INCOMPLETE"):
            v = "DO-NOT-SHIP"
        # debug_plan 解析
        debug_plan = None
        dp_data = data.get("debug_plan")
        if dp_data:
            debug_plan = Plan(
                plan_id=uuid.uuid4().hex[:8],
                is_debug_plan=True,
                summary=dp_data.get("summary", ""),
                reasoning=dp_data.get("reasoning", ""),
                tech_stack=dp_data.get("tech_stack", []),
                root_cause=dp_data.get("root_cause"),
                tasks=[TaskItem(**t) for t in dp_data.get("tasks", [])],
            )
        # new_plan 解析
        new_plan = None
        np_data = data.get("new_plan")
        if np_data:
            new_plan = Plan(
                plan_id=uuid.uuid4().hex[:8],
                is_debug_plan=False,
                summary=np_data.get("summary", ""),
                reasoning=np_data.get("reasoning", ""),
                tech_stack=np_data.get("tech_stack", []),
                root_cause=np_data.get("root_cause"),
                tasks=[TaskItem(**t) for t in np_data.get("tasks", [])],
            )
        return VerifyResult(
            verdict=Verdict(v),
            summary=data.get("summary", ""),
            issues=[
                _safe_issue(i) for i in data.get("issues", [])
            ],
            debug_plan=debug_plan,
            new_plan=new_plan,
        )

    # -----------------------------------------------------------------
    @staticmethod
    def _emit(handler: Optional[EventHandler], name: str, payload: dict, state: Optional[SessionState] = None) -> None:
        if handler:
            try:
                handler(name, payload)
            except Exception:
                pass  # 事件回调失败不影响主流程
        # 每次事件发射时同步状态文件（供 UI 实时读取）
        if state is not None:
            _write_state_file(state)


# ---------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------
def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    # 部分模型会在 JSON 外包裹 ```json ... ```，剥掉
    if raw.startswith("```"):
        raw = raw.strip("`")
        # 去掉语言标识 json
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"模型输出非合法 JSON: {e}\n原始输出前500字: {raw[:500]}")


def _safe_issue(d: dict) -> "object":
    from .models import VerifyIssue

    return VerifyIssue(
        severity=d.get("severity", "major"),
        file=d.get("file", "(unknown)"),
        line=d.get("line"),
        criterion=d.get("criterion", ""),
        failure_scenario=d.get("failure_scenario", ""),
        suggested_fix=d.get("suggested_fix", ""),
    )
