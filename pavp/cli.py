"""PAVP CLI 入口 - 命令行运行 Plan-Act-Verify-Plan 工作流

用法:
  python -m pavp --init                       # 生成 ~/.pavp/settings.json 模板
  python -m pavp --check                      # 环境自检
  python -m pavp "需求描述" -p D:/repo         # 运行工作流
  python -m pavp --sessions                   # 列出历史会话
  python -m pavp --resume <session-id>         # 恢复会话（查看摘要/继续）
  python -m pavp --delete <session-id>         # 删除会话
  python -m pavp --help
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import httpx

from . import settings as pavp_settings
from . import storage as pavp_storage
from .orchestrator import Orchestrator


# ---------------------------------------------------------------------
# 事件打印
# ---------------------------------------------------------------------
def print_event(name: str, payload: dict) -> None:
    if name == "plan_start":
        print(f"\n[Phase 1/3 · Plan] 分析需求: {payload['requirement']}")
    elif name == "plan_done":
        print(f"[Plan ✓] {payload['task_count']} 个任务")
        print_plan(payload["plan"])
    elif name == "clarification_needed":
        print(f"[Plan] 需求需澄清: {payload['plan'].get('summary')}")
    elif name == "act_start":
        print(f"\n[Phase 2/3 · Act] 迭代 {payload['iteration']} - CC 执行编码...")
    elif name == "act_done":
        cost = payload.get("cost_usd", 0)
        print(
            f"[Act {'✓' if payload['success'] else '✗'}] "
            f"{len(payload['files'])} 文件改动, ${cost:.4f}"
        )
        if payload.get("error"):
            print(f"  错误: {payload['error']}")
    elif name == "verify_start":
        print(f"\n[Phase 3/3 · Verify] 迭代 {payload['iteration']} - 红队审计...")
    elif name == "verify_done":
        v = payload["verdict"]
        mark = {"PASS": "✓", "SHIP-WITH-FIXES": "⚠", "DO-NOT-SHIP": "✗", "INCOMPLETE": "◐"}.get(v, "?")
        print(f"[Verify {mark}] {v} ({payload['issue_count']} issues)")
        print(f"  {payload['summary']}")
    elif name == "awaiting_user":
        print("\n[等待] Verify 失败，等待用户决策...")
    elif name == "debug_plan_adopted":
        print(f"[DebugPlan] 已采纳 {payload['task_count']} 个修正任务")
    elif name == "new_plan_adopted":
        print(f"[NewPlan] 已采纳 {payload['task_count']} 个续接任务")
    elif name == "done":
        print(f"\n{'='*60}")
        print(f"工作流结束: {payload['reason']} (共 {payload.get('iterations',0)} 次迭代)")
        if "answer" in payload:
            print(f"\n{payload['answer']}")
        print("=" * 60)
    elif name == "failed":
        print(f"\n[失败] {payload['reason']}", file=sys.stderr)


def print_plan(plan: dict) -> None:
    print(f"  摘要: {plan.get('summary')}")
    for t in plan.get("tasks", []):
        deps = f" <- {t['depends_on']}" if t.get("depends_on") else ""
        print(f"  [{t['id']}] {t['title']}{deps}")
        for c in t.get("acceptance_criteria", []):
            print(f"        验收: {c}")


# ---------------------------------------------------------------------
# 环境自检
# ---------------------------------------------------------------------
def check_env() -> int:
    print("PAVP 环境自检")
    print("=" * 60)
    ok = True

    # 1. Python
    print(f"[1] Python {sys.version.split()[0]}")

    # 2. 依赖
    missing = []
    for mod in ("httpx", "pydantic", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        ok = False
        print(f"[2] ✗ 缺少依赖: {missing}  (pip install -r requirements.txt)")
    else:
        print("[2] ✓ Python 依赖齐全")

    # 3. 设置文件
    sp = pavp_settings.settings_path()
    print(f"[3] 设置文件: {sp}")
    s = None
    try:
        s = pavp_settings.load()
        print("[3] ✓ 设置文件存在且为合法 JSON")
    except pavp_settings.SettingsError as e:
        ok = False
        print(f"[3] ✗ {e}")
        print("       运行 python -m pavp --init 生成模板")

    # 4. 模型配置填写情况
    if s is not None:
        plan_fields = {"plan_api": "Plan API 密钥", "plan_base_url": "Plan 代理地址",
                       "plan_model": "Plan 模型标识"}
        act_fields = {"act_api": "Act API 密钥", "act_base_url": "Act 代理地址",
                      "act_model": "Act 模型标识"}
        plan_filled = [plan_fields[k] for k, v in s.items()
                       if k in plan_fields and v]
        plan_empty = [plan_fields[k] for k, v in s.items()
                      if k in plan_fields and not v]
        act_filled = [act_fields[k] for k, v in s.items()
                      if k in act_fields and v]
        act_empty = [act_fields[k] for k, v in s.items()
                     if k in act_fields and not v]
        print(f"[4] Plan (已填): {plan_filled or '(无)'}")
        if plan_empty:
            print(f"    Plan (未填): {plan_empty}")
        print(f"    Act  (已填): {act_filled or '(无)'}")
        if act_empty:
            print(f"    Act  (未填): {act_empty}")

    # 5. 代理可达
    if s is not None:
        url = pavp_settings.proxy_url(s)
        key = pavp_settings.proxy_key(s)
        print(f"[5] PAVP 代理: {url}")
        try:
            r = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "pavp",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
                timeout=15,
            )
            if r.status_code == 200:
                print("[5] ✓ 代理可达，模型响应正常")
            else:
                ok = False
                print(f"[5] ✗ 代理返回 {r.status_code}: {r.text[:200]}")
        except httpx.ConnectError:
            ok = False
            print("[5] ✗ 无法连接代理。请先启动代理: python -m pavp.proxy_server")
        except Exception as e:
            ok = False
            print(f"[5] ✗ {e}")

    # 6. Claude Code 可执行
    cc_bin = (s or {}).get("cc_bin", "claude")
    found = shutil.which(cc_bin) or (
        Path(cc_bin).is_file() if Path(cc_bin).is_absolute() else None
    )
    if found:
        print(f"[6] ✓ Claude Code: {cc_bin}")
    else:
        ok = False
        print(
            f"[6] ✗ 未找到 '{cc_bin}'。安装: npm i -g @anthropic-ai/claude-code "
            "或在 settings.json 改 cc_bin"
        )

    print("=" * 60)
    print("自检通过" if ok else "自检未通过，请按提示修复")
    return 0 if ok else 1


# ---------------------------------------------------------------------
# 会话管理命令
# ---------------------------------------------------------------------
def list_sessions_cmd() -> int:
    sessions = pavp_storage.list_all()
    if not sessions:
        print("无历史会话")
        return 0
    print(f"{'会话ID':<14} {'状态':<6} {'时间':<22} 需求")
    print("-" * 80)
    for s in sessions:
        print(
            f"{s['session_id']:<14} "
            f"{s['fsm_state']:<6} "
            f"{s['updated_at'][:19]:<22} "
            f"{s['requirement'][:50]}"
        )
    return 0


def delete_session_cmd(sid: str) -> int:
    state = pavp_storage.load(sid)
    if not state:
        print(f"会话 {sid} 不存在")
        return 1
    pavp_storage.delete(sid)
    print(f"已删除会话 {sid}: {state.original_requirement[:60]}")
    return 0


def resume_session_cmd(sid: str) -> int:
    state = pavp_storage.load(sid)
    if not state:
        print(f"会话 {sid} 不存在")
        return 1
    project_root = pavp_storage.load_project_root(sid) or os.getcwd()

    print(f"会话: {sid}")
    print(f"状态: {state.fsm_state}")
    print(f"需求: {state.original_requirement}")
    print(f"迭代: {state.iteration}/{state.max_iterations}")

    # Plan 历史
    for i, plan in enumerate(state.plan_history):
        tag = "DebugPlan" if plan.is_debug_plan else "Plan"
        print(f"\n--- {tag} #{i+1} [{plan.plan_id}] ---")
        print(f"摘要: {plan.summary}")
        if plan.root_cause:
            print(f"根因: {plan.root_cause}")
        for t in plan.tasks:
            print(f"  [{t.id}] {t.title} ({t.status})")

    # Act 历史
    for i, act in enumerate(state.act_history):
        print(f"\n--- Act #{i+1} ---")
        print(f"成功: {act.success} | 文件: {act.files_changed} | 花费: ${act.cost_usd:.4f}")
        if act.error:
            print(f"错误: {act.error}")

    # Verify 历史
    for i, v in enumerate(state.verify_history):
        print(f"\n--- Verify #{i+1} ---")
        print(f"裁决: {v.verdict.value}")
        print(f"摘要: {v.summary}")
        for issue in v.issues:
            print(f"  [{issue.severity}] {issue.file}:{issue.line} — {issue.failure_scenario}")

    print(f"\n项目根目录: {project_root}")

    # 若处于 AWAITING_USER 状态，提示可继续
    if state.fsm_state == "AWAITING_USER" and state.verify_history:
        last_v = state.verify_history[-1]
        plan = last_v.debug_plan or last_v.new_plan
        plan_label = "DebugPlan" if last_v.debug_plan else "NewPlan"
        if plan:
            print(f"\n⚠ 该会话处于 AWAITING_USER 状态，{plan_label} 可用")
            print(f"{plan_label}: {plan.summary}")
            print(f"可通过 Streamlit UI 继续: streamlit run pavp/ui.py")

    return 0


# ---------------------------------------------------------------------
# 主
# ---------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pavp",
        description="Plan-Act-Verify-Plan 本地工作流",
    )
    p.add_argument("requirement", nargs="?", help="需求描述")
    p.add_argument("--project-root", "-p", default=os.getcwd(),
                   help="目标项目根目录 (CC 可访问范围)")
    p.add_argument("--workdir", "-w", default=None,
                   help="CC 工作目录 (默认 = project-root)")
    p.add_argument("--max-iterations", "-n", type=int, default=3,
                   help="Act-Verify 最大循环次数 (默认 3)")
    p.add_argument("--loop-mode", choices=["auto", "manual"], default=None,
                   help="Loop 循环模式: auto(自动,默认) / manual(手动). 不指定则读取 settings.json")
    p.add_argument("--check", action="store_true", help="环境自检后退出")
    p.add_argument("--init", action="store_true",
                   help="生成 ~/.pavp/settings.json 模板后退出")
    p.add_argument("--sessions", action="store_true", help="列出历史会话")
    p.add_argument("--resume", metavar="SESSION_ID", help="恢复并查看指定会话")
    p.add_argument("--delete", metavar="SESSION_ID", help="删除指定会话")
    args = p.parse_args(argv)

    if args.init:
        try:
            path = pavp_settings.init_template()
            print(f"已生成模板: {path}")
            print("请编辑该文件填入 plan_* 和 act_* 中的密钥与模型标识。")
            return 0
        except pavp_settings.SettingsError as e:
            print(e, file=sys.stderr)
            return 1

    if args.check:
        return check_env()

    if args.sessions:
        return list_sessions_cmd()

    if args.delete:
        return delete_session_cmd(args.delete)

    if args.resume:
        return resume_session_cmd(args.resume)

    if not args.requirement:
        p.error("需要提供需求描述，或使用 --check / --init / --sessions / --resume")

    project_root = str(Path(args.project_root).resolve())
    workdir = str(Path(args.workdir or args.project_root).resolve())

    print(f"PAVP 工作流启动")
    print(f"  项目: {project_root}")
    print(f"  最大迭代: {args.max_iterations}")

    orch = Orchestrator(project_root=project_root, workdir=workdir)
    state = orch.run(
        args.requirement,
        max_iterations=args.max_iterations,
        loop_mode=args.loop_mode,
        on_event=print_event,
    )

    return 0 if state.fsm_state == "DONE" else 1


if __name__ == "__main__":
    sys.exit(main())
