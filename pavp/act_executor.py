"""PAVP Act 执行器 - 调用 Claude Code headless 子进程执行编码

方案 A（推荐）: subprocess 调 `claude -p`，复用 CC 工具链
关键参数已按 2026-07 CC 版本核实（见 docs/PAVP技术设计文档.md 第 7 节）

运行配置（cc_bin / 预算 / 轮数 / 超时）从 ~/.pavp/settings.json 读取
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from .models import ActResult, Plan
from .prompts import ACT_CONSTRAINTS, build_act_user_prompt
from .settings import load as load_settings

# Act 阶段允许的工具（最小权限）
ALLOWED_TOOLS = ",".join([
    "Read",
    "Edit",
    "Write",
    "Grep",
    "Glob",
    "Bash(git diff:*)",
    "Bash(git status:*)",
    "Bash(git diff --stat:*)",
    "Bash(dotnet build)",
    "Bash(dotnet test)",
    "Bash(python -m pytest)",
])

# Act 阶段禁用的危险工具
DISALLOWED_TOOLS = ",".join([
    "Bash(rm *)",
    "Bash(rm -rf*)",
    "Bash(git push*)",
    "Bash(git commit*)",
    "Bash(git reset --hard*)",
    "Bash(git checkout -- *)",
    "WebFetch",
    "WebSearch",
])


class ActError(RuntimeError):
    """Act 执行失败"""


def run_act(plan: Plan, project_root: str, workdir: str) -> ActResult:
    """调用 CC headless 执行 Act 阶段。

    Args:
        plan: 当前要执行的 Plan（可能是 DebugPlan）
        project_root: 项目根目录（CC --add-dir，决定可访问范围）
        workdir: CC 子进程工作目录（通常 = project_root）

    Returns:
        ActResult，含 diff / files_changed / cc_output / cost / session_id
    """
    s = load_settings()
    cc_bin = s["cc_bin"]
    max_budget = float(s["act_max_budget"])
    max_turns = int(s["act_max_turns"])
    act_timeout = int(s["act_timeout"])

    project_root = str(Path(project_root).resolve())
    workdir = str(Path(workdir).resolve())
    user_prompt = build_act_user_prompt(plan)

    # 记录 Act 前的 commit，便于失败回滚
    pre_sha = _git_rev_parse_head(workdir)

    cmd = [
        cc_bin,
        "--bare",
        "-p",
        "--permission-mode", "dontAsk",
        "--allowedTools", ALLOWED_TOOLS,
        "--disallowedTools", DISALLOWED_TOOLS,
        "--max-turns", str(max_turns),
        "--max-budget-usd", str(max_budget),
        "--output-format", "json",
        "--model", "act-model",
        "--add-dir", project_root,
        "--append-system-prompt", ACT_CONSTRAINTS,
        user_prompt,
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=act_timeout,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired as e:
        return ActResult(
            success=False,
            error=f"CC 超时({act_timeout}s)。可在 settings.json 调大 act_timeout。",
            cc_output=e.stdout or "" if isinstance(e.stdout, str) else "",
        )
    except FileNotFoundError as e:
        raise ActError(
            f"未找到 Claude Code 可执行文件 '{cc_bin}'。"
            f"请安装 CC (npm i -g @anthropic-ai/claude-code) 或在 "
            f"settings.json 中改 cc_bin。"
        ) from e

    if proc.returncode != 0:
        return ActResult(
            success=False,
            error=f"CC 退出码 {proc.returncode}: {proc.stderr or proc.stdout}",
            cc_output=proc.stdout,
        )

    # 解析 CC JSON 输出
    cc_resp = _parse_cc_json(proc.stdout)
    session_id = cc_resp.get("session_id", "")
    cc_text = cc_resp.get("result", proc.stdout)
    cost = _safe_float(cc_resp.get("total_cost_usd"))

    # diff 由编排器自己抓（不依赖 CC 输出格式）
    diff, files = _capture_diff(workdir, pre_sha)

    return ActResult(
        session_id=session_id,
        diff=diff,
        files_changed=files,
        cc_output=cc_text,
        cost_usd=cost,
        success=True,
    )


# ---------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------
def _git_rev_parse_head(workdir: str) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workdir,
            capture_output=True, text=True, encoding="utf-8",
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _capture_diff(workdir: str, pre_sha: Optional[str]) -> tuple[str, list[str]]:
    """捕获 Act 后的 git diff（含 untracked 文件）"""
    tracked_cmd = (
        ["git", "diff", pre_sha] if pre_sha else ["git", "diff", "HEAD"]
    )
    tracked = subprocess.run(
        tracked_cmd, cwd=workdir,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=workdir,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.split()

    changed = subprocess.run(
        ["git", "diff", "--name-only", pre_sha or "HEAD"], cwd=workdir,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.split() + untracked

    # 把 untracked 文件内容拼成 diff 样式（便于 Verify 审阅）
    untracked_diff = ""
    for f in untracked:
        p = Path(workdir) / f
        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = "(读取失败)"
            untracked_diff += f"\ndiff --git (new) {f}\n+++ b/{f}\n{content}\n"

    return (tracked + untracked_diff), changed


def _parse_cc_json(stdout: str) -> dict:
    """解析 CC 的 JSON 输出（容错：某些版本会混入日志行）"""
    stdout = stdout.strip()
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        idx = stdout.rfind("\n{")
        if idx >= 0:
            try:
                return json.loads(stdout[idx + 1 :])
            except json.JSONDecodeError:
                pass
        return {"result": stdout, "session_id": "", "total_cost_usd": 0.0}


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
