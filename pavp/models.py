"""PAVP 数据结构 - 所有阶段共享的 Pydantic 模型"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    """Verify 裁决五档"""
    PASS = "PASS"                        # 通过，任务结束
    SHIP_WITH_FIXES = "SHIP-WITH-FIXES"  # 可发布但有遗留
    DO_NOT_SHIP = "DO-NOT-SHIP"          # 不可发布，需 DebugPlan
    INCOMPLETE = "INCOMPLETE"            # 任务未按预期完成，需新 Plan 续接
    NEEDS_REVIEW = "NEEDS-REVIEW"        # 模棱两可，需人工确认是否 Loop


class TaskItem(BaseModel):
    """Plan 中的单个任务项"""
    id: str
    title: str
    file_paths: list[str] = []
    entry_points: str = ""  # 代码修改入口：文件名、类名、函数名、行号范围
    tech_stack: list[str] = []  # 该任务涉及的技术栈
    implementation_logic: str = ""  # 实现逻辑：算法、数据结构、设计模式、核心流程
    acceptance_criteria: list[str] = []
    depends_on: list[str] = []
    status: str = "pending"  # pending / done / failed / skipped


class Plan(BaseModel):
    """Plan / DebugPlan 统一结构"""
    plan_id: str = ""
    is_debug_plan: bool = False
    summary: str
    reasoning: str = ""  # 思考过程：需求分析、技术选型、方案权衡、潜在风险
    tech_stack: list[str] = []  # 整体技术栈概览
    requires_act: bool = True  # Plan 模型判定是否需要执行 Act 阶段
    root_cause: Optional[str] = None
    tasks: list[TaskItem] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)


class VerifyIssue(BaseModel):
    """Verify 发现的单个问题"""
    severity: str  # blocker / major / minor / nit
    file: str
    line: Optional[str] = None
    criterion: str
    failure_scenario: str
    suggested_fix: str = ""


class VerifyResult(BaseModel):
    """Verify 输出"""
    verdict: Verdict
    summary: str = ""
    issues: list[VerifyIssue] = []
    debug_plan: Optional[Plan] = None
    new_plan: Optional[Plan] = None  # INCOMPLETE 时的续接计划
    needs_loop: bool = False  # 发现漏洞/思考不足/可能导致其他问题时，即使 PASS 也触发 Loop


class ActResult(BaseModel):
    """Act 执行结果"""
    session_id: str = ""
    diff: str = ""
    files_changed: list[str] = []
    cc_output: str = ""
    cost_usd: float = 0.0
    success: bool = True
    error: Optional[str] = None


class SessionState(BaseModel):
    """整个工作流的持久化状态"""
    session_id: str
    original_requirement: str
    current_plan: Optional[Plan] = None
    plan_history: list[Plan] = []
    act_history: list[ActResult] = []
    verify_history: list[VerifyResult] = []
    fsm_state: str = "IDLE"
    iteration: int = 0
    max_iterations: int = 3
    created_at: datetime = Field(default_factory=datetime.utcnow)
