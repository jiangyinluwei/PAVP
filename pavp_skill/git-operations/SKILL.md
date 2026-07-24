---
name: "git-operations"
description: "Git operation safety rules for PAVP. Activated when the task involves git commands (add, commit, push, diff, status, etc.) or any VCS-related operations. Restricts task_key usage and enforces single-threaded PAVP agent execution."
---

# Git Operations Safety Rules

## Core Restriction

**涉及 git 操作时，禁止 task_key 逻辑，只能使用单线程 PAVP 代理。**

When git operations are involved, the `task_key` logic is **forbidden**, and only **single-threaded PAVP agents** may be used.

---

## Rationale

Git operations (add, commit, push, diff, merge, rebase, etc.) are **state-sensitive** and **order-dependent**:

1. **State sensitivity**: Git relies on a linear, deterministic commit history. Concurrent task execution with multiple `task_key`s can lead to race conditions, conflicting staging areas, and corrupted commit histories.
2. **Order dependency**: Git operations like `commit`, `push`, and `merge` must happen in a strict sequence. Parallel agent delegation (`Task`/`subagent`) can violate this ordering, producing inconsistent repository states.
3. **Atomicity**: A single logical change (e.g., "add feature X") should map to a single git commit. Multi-threaded execution with `task_key` tracking can fragment changes across multiple unrelated contexts.

---

## Restrictions

### 1. No `task_key` Logic

- **Do NOT** generate or use `task_key` fields in plan JSON output when the task involves git operations.
- **Do NOT** cache plans by `task_key` — bypass `_plan_cache` / `_task_cache` in `proxy_server.py`.
- **Do NOT** track active tasks via `task_key` — the `_task_cache` dict must remain empty for git-related sessions.
- The plan output should omit the `task_key` field entirely.

### 2. Single-Threaded Agent Only

- **Do NOT** use `Task` or `subagent` delegation tools to parallelize work.
- **Do NOT** dispatch independent subtasks in parallel — all execution must be sequential.
- The `[PAVP Plan]` instruction in `_build_act_messages` must be modified to remove the parallel delegation guidance:
  - Instead of: `"use available agent-delegation tools (e.g. Task, subagent) to dispatch independent subtasks in parallel for efficiency"`
  - Use: `"execute all steps sequentially in a single thread — do NOT use Task or subagent delegation"`
- All git operations (add, commit, push, diff, status, merge, rebase, etc.) must run in the same linear execution flow.

### 3. Workflow Constraint

```
IDLE -> PLANNING -> ACTING -> DONE
```

The Verify phase should be minimal or skipped for git operations — the "verify" is the git status/diff output itself. No parallel loops, no concurrent task tracking.

---

## Detection

A task involves git operations if **any** of the following is true:

- The user requirement mentions: "git", "commit", "push", "pull", "merge", "rebase", "branch", "checkout", "stash", "tag", "clone", "remote", "diff", "status", "add", "reset", "log", "blame", "fetch"
- The plan involves modifying `.gitignore`, `.gitattributes`, `.gitmodules`, or any git config files
- The execution plan includes CLI commands like `git status`, `git add`, `git commit`, `git push`, `git diff`, `git log`, etc.
- The project context indicates a version control workflow (e.g., "staging changes", "committing", "pushing to remote")

---

## Code Locations Affected

| Location | Responsibility | Change Required |
|---|---|---|
| `pavp/prompts.py` | Plan prompt template with `task_key` instruction | When git is detected, omit `task_key` from plan JSON schema |
| `pavp/proxy_server.py` lines 274-292 | `_build_act_messages` — parallel delegation instruction | Replace with single-threaded instruction |
| `pavp/proxy_server.py` lines 39-51 | `_plan_cache` / `_task_cache` / `_act_started` | Bypass all caching for git sessions |
| `pavp/proxy_server.py` lines 172-186 | `_extract_task_key` | Do not call for git tasks |
| `pavp/proxy_server.py` lines 189-210 | `_cleanup_stale_tasks` | Not needed for git tasks |
| `pavp/engine.py` lines 25-65 | Plan prompt helper that includes `task_key` requirement | Skip `task_key` when git is detected |

---

## Example

### ✅ Correct (Git Operation)

```python
# Plan JSON — no task_key, no parallel delegation
plan = {
    "summary": "Stage and commit changes to models.py",
    "reasoning": "Sequential execution: git add -> git commit",
    "requires_act": True,
    "tech_stack": ["git"],
    "implementation_logic": "Run git add models.py, then git commit -m 'msg'",
    "tasks": [
        {
            "id": "T1",
            "title": "Stage changes to models.py",
            "file_paths": ["pavp/models.py"],
            "acceptance_criteria": ["git status shows staged changes"],
            "status": "pending"
        }
    ]
}
# Note: no "task_key" field present
```

### ❌ Incorrect (Git Operation)

```python
plan = {
    "task_key": "TK-a1b2c3d4",  # FORBIDDEN: task_key must not exist
    "summary": "...",
    ...
}
```

---

## Enforcement

When this skill is triggered:

1. The agent MUST first verify whether the task involves git operations (see Detection section above).
2. If git is detected, the agent MUST:
   - Skip `task_key` generation in all plan JSON output
   - Bypass all `_plan_cache` / `_task_cache` logic
   - Use only single-threaded execution (no `Task`/`subagent` delegation)
   - Execute all steps sequentially in the main thread
3. The agent MUST NOT modify `proxy_server.py` or `prompts.py` at runtime — the restriction is enforced by the agent's own behavior when following this skill.