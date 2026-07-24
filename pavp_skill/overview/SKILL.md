---
name: "overview"
description: "PAVP project overview and context guide. MUST be invoked before any project-related task to provide architecture overview, file structure, tech stack, coding conventions, and workflow agreements."
---

# PAVP Project Overview

## Project Description

**PAVP = Plan-Act-Verify-Plan**, a local AI coding workflow tool. It drives code generation and quality assurance through a state machine of "Plan -> Act -> Verify -> Loop", supporting automatic iterative fixes.

Core idea: use high-power reasoning models for Plan/Verify, use coding models for Act, and use Claude Code headless to execute actual file modifications, forming a closed-loop self-correction system.

---

## Tech Stack

| Layer | Technology | Description |
|---|---|---|
| Language | Python 3.12+ | `from __future__ import annotations` used consistently |
| Data Models | Pydantic v2 | Shared data structures across all phases |
| HTTP Client | httpx >= 0.27 | Synchronous LLM API calls + Proxy health checks |
| Proxy Server | FastAPI + Uvicorn | PAVP Proxy (Plan-Act routing, `/v1/chat/completions`) |
| Act Executor | Claude Code (headless) | `claude --bare -p` subprocess call with tool whitelist |
| UI | Streamlit >= 1.40 | Config panel + proxy launcher + log viewer |
| Persistence | SQLite | Session state storage (`~/.pavp/sessions.db`), supports interrupt recovery |
| Config | JSON | `~/.pavp/settings.json` |
| Packaging | PyInstaller | Build `dist/pavp.exe` standalone executable |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    User Entry                            │
│  ┌──────────┐   ┌────────────┐   ┌──────────────────┐  │
│  │ CLI      │   │ Streamlit  │   │ Proxy Server     │  │
│  │ cli.py   │   │ ui.py      │   │ proxy_server.py  │  │
│  └────┬─────┘   └─────┬──────┘   └────────┬─────────┘  │
│       │               │                   │             │
│       ▼               ▼                   ▼             │
│  ┌────────────────────────────────────────────────┐     │
│  │           Orchestrator (State Machine)          │     │
│  │           orchestrator.py                       │     │
│  │  PLANNING → ACTING → VERIFYING → DONE/FAILED   │     │
│  │       ↑                    │                    │     │
│  │       └─── AWAITING_USER ──┘ (manual mode)      │     │
│  └───┬──────────┬──────────────┬───────────────────┘    │
│      ▼          ▼              ▼                        │
│  ┌────────┐ ┌────────┐   ┌──────────┐                   │
│  │ Plan   │ │ Act    │   │ Verify   │                   │
│  │ LLM    │ │ CC Proc│   │ LLM      │                   │
│  │prompts │ │act_exec│   │prompts   │                   │
│  └───┬────┘ └───┬────┘   └────┬─────┘                   │
│      │          │             │                          │
│      ▼          ▼             ▼                          │
│  ┌──────────────────────────────────────┐               │
│  │  engine.py (_call_llm_text) → LLM   │               │
│  │  settings.py → ~/.pavp/settings.json │               │
│  │  storage.py  → ~/.pavp/sessions.db   │               │
│  └──────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────┘
```

### Operation Mode

1. **Proxy Mode (Proxy Server)**: `proxy_server.py` acts as a FastAPI proxy, intercepting Agent LLM requests for Plan-Act routing (without Verify loop)

---

## File Structure

```
PAVP/
├── pavp/
│   ├── __init__.py          # Package init, __version__ = "0.1.0"
│   ├── settings.py          # ~/.pavp/settings.json read/write
│   ├── models.py            # Pydantic models (Plan/VerifyResult/ActResult/SessionState etc.)
│   ├── prompts.py           # Plan/Act/Verify phase prompt templates
│   ├── engine.py            # Core engine: LLM calls (Plan, raw completion), tool-aware planning, Plan-Act routing
│   ├── orchestrator.py      # State machine orchestrator (Plan->Act->Verify->Loop)
│   ├── act_executor.py      # Claude Code headless subprocess execution + rollback
│   ├── proxy_server.py      # FastAPI proxy server (Plan-Act routing, /v1/chat/completions)
│   ├── storage.py           # SQLite session persistence (sessions.db)
│   ├── ui.py                # Streamlit UI config panel + proxy launcher
│   └── auto_start.py        # Windows auto-start via registry Run key
├── pavp_skill/              # Project skill folder
│   ├── find-skills/SKILL.md
│   ├── writing-plans/SKILL.md
│   ├── overview/SKILL.md
│   └── streamlit-ui/SKILL.md
├── .trae/
│   ├── rules/               # Project rules directory
│   └── skills/              # IDE-level skills (orchestrator)
├── requirements.txt         # Python dependencies
├── run.ps1                  # Streamlit UI launch script
└── .gitignore
```

---

## Core Data Models (models.py)

| Model | Description |
|---|---|
| `Verdict` (Enum) | Four verdicts: PASS / SHIP_WITH_FIXES / DO_NOT_SHIP / INCOMPLETE |
| `TaskItem` | Single task item in a Plan (id, title, file_paths, acceptance_criteria, depends_on, status) |
| `Plan` | Plan structure (plan_id, is_debug_plan, summary, requires_act, root_cause, tasks) |
| `VerifyIssue` | Issue found by Verify (severity, file, line, criterion, failure_scenario, suggested_fix) |
| `VerifyResult` | Verify output (verdict, summary, issues, debug_plan, new_plan) |
| `ActResult` | Act execution result (session_id, diff, files_changed, cc_output, cost_usd, success) |
| `SessionState` | Workflow persisted state (session_id, original_requirement, plan_history, act_history, verify_history, fsm_state, iteration) |

---

## State Machine (FSM)

```
IDLE -> PLANNING -> ACTING -> VERIFYING -> DONE
                              |
                              +-- PASS / SHIP_WITH_FIXES -> DONE
                              +-- DO_NOT_SHIP -> (debug_plan) -> ACTING (loop)
                              +-- INCOMPLETE -> (new_plan) -> ACTING (loop)
                              +-- AWAITING_USER (manual mode) -> continue/ignore
                              |
                              +-- max_iterations reached -> FAILED
```

---

## Configuration (settings.py)

Config file location: `~/.pavp/settings.json`

Key fields:
- `litellm_master_key`: Orchestrator auth key for calling proxy (default `sk-pavp-local`)
- `proxy_port`: PAVP proxy listen port (default 4001)
- `plan_api` / `plan_base_url` / `plan_model`: Plan/Verify model config (e.g. `deepseek/deepseek-reasoner`)
- `act_api` / `act_base_url` / `act_model`: Act model config (e.g. `openai/qwen2.5-coder-32b-instruct`)
- `cc_bin`: Claude Code executable path (default `claude`)
- `act_max_budget` / `act_max_turns` / `act_timeout`: Act execution limits
- `loop_mode`: `auto` (automatic loop) / `manual` (manual confirmation)
- `auto_start`: Enable Windows auto-start (default `True`)

Model identifier format: `provider/model` (e.g. `openai/gpt-4o-mini`, `deepseek/deepseek-reasoner`).

---

## Coding Conventions

### Python Style
- `from __future__ import annotations` at the top of every file
- Type annotations: use `str | None` instead of `Optional[str]`, `list[str]` instead of `List[str]`
- Pydantic v2: `model_dump(mode="json")` / `model_validate()`
- Strings: consistently use double quotes `"`
- Comments: always in English
- Docstrings: module-level `"""..."""`, function-level brief description

### File Responsibilities
- Each module has a clear single responsibility
- `prompts.py` manages all LLM prompt templates, no business logic mixed in
- `models.py` defines only data structures, no business methods
- `settings.py` handles only config read/write, no business logic

### Error Handling
- Custom exception classes inherit from `RuntimeError` (e.g., `SettingsError`, `LLMError`, `ActError`)
- External calls (HTTP/subprocess) must have try-except with meaningful error messages
- Event callback failures do not block the main flow (`orchestrator._emit` uses try-except pass)

### Security Constraints (Act Phase)
- Allowed tools whitelist: Read, Edit, Write, Grep, Glob, Restricted Bash (git diff/status, dotnet build/test, pytest)
- Disallowed tools blacklist: rm, git push, git commit, git reset --hard, WebFetch, WebSearch
- Budget control: triple restriction of max_budget_usd / max_turns / timeout

---

## Launch Methods

| Method | Command | Description |
|---|---|---|
| UI Launch | `.\run.ps1` | Start Streamlit config panel, proxy runs in background |

---

## Development Conventions

1. **Read before modifying**: Understand existing code before making changes, don't guess
2. **Keep it simple**: Only make necessary changes, avoid over-engineering
3. **Pydantic first**: Use Pydantic models for data structures, not raw dicts
4. **Centralized prompts**: All LLM prompts defined in `prompts.py`
5. **Clear state machine**: FSM state transitions managed centrally in `orchestrator.py`
6. **Auto-persistence**: Auto-save to SQLite after each phase completes
7. **Event-driven**: UI/logging receives phase events through `on_event` callback

---

## How to Use This Skill

Activate this skill to get project context before executing any PAVP-related task. Steps:

1. **Understand requirements**: Identify which modules are involved (UI/engine/orchestrator/proxy/storage etc.)
2. **Locate files**: Find relevant files according to the file structure table
3. **Follow conventions**: Write code according to coding conventions and development guidelines
4. **Stay consistent**: Keep consistent with existing code style (type annotations, comments, error handling)
