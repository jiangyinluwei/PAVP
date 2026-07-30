# AGENT.md - PAVP Agent Guide

> 本文件是 AI 编码代理在本项目工作的**主入口与操作手册**。代理启动时自动加载，无需手动调用。

---

## 1. 项目概述

**PAVP = Plan-Act-Verify-Plan**，一个本地 AI 编码工作流工具。通过 "Plan -> Act -> Verify -> Loop" 状态机驱动代码生成与质量保证，支持自动迭代修复。

核心思路：用高能力推理模型做 Plan/Verify，用编码模型做 Act，用 Claude Code headless 执行实际文件修改，形成闭环自纠系统。

### 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 语言 | Python 3.12+ | 统一使用 `from __future__ import annotations` |
| 数据模型 | Pydantic v2 | 全阶段共享数据结构 |
| HTTP 客户端 | httpx >= 0.27 | 同步 LLM 调用 + Proxy 健康检查 |
| 代理服务器 | FastAPI + Uvicorn | PAVP Proxy（Plan-Act 路由，`/v1/chat/completions`） |
| Act 执行器 | Claude Code (headless) | `claude --bare -p` 子进程调用 + 工具白名单 |
| UI | Streamlit >= 1.40 | 配置面板 + 代理启动器 + 日志查看 |
| 持久化 | SQLite | 会话状态存储（`~/.pavp/sessions.db`），支持中断恢复 |
| 配置 | JSON | `~/.pavp/settings.json` |
| 打包 | PyInstaller | 构建 `dist/pavp.exe` 独立可执行文件 |

### 文件结构

```
PAVP/
├── pavp/
│   ├── __init__.py          # 包初始化, __version__ = "0.1.0"
│   ├── settings.py          # ~/.pavp/settings.json 读写
│   ├── models.py            # Pydantic 模型 (Plan/VerifyResult/ActResult/SessionState 等)
│   ├── prompts.py           # Plan/Act/Verify 阶段提示词模板
│   ├── engine.py            # 核心引擎：LLM 调用、工具感知规划、Plan-Act 路由
│   ├── orchestrator.py      # 状态机编排器 (Plan->Act->Verify->Loop)
│   ├── act_executor.py      # Claude Code headless 子进程执行 + 回滚
│   ├── proxy_server.py      # FastAPI 代理服务器 (Plan-Act 路由, /v1/chat/completions)
│   ├── storage.py           # SQLite 会话持久化 (sessions.db)
│   ├── ui.py                # Streamlit UI 配置面板 + 代理启动器
│   └── auto_start.py        # Windows 自启动 (VBS 包装 + 注册表 Run 键)
├── pavp_skill/              # 项目技能文件夹（见下方路由表）
├── requirements.txt         # Python 依赖
├── run.ps1                  # Streamlit UI 启动脚本
└── .gitignore
```

---

## 2. 编码规范

### Python 风格
- 每个文件顶部使用 `from __future__ import annotations`
- 类型注解：用 `str | None` 而非 `Optional[str]`，用 `list[str]` 而非 `List[str]`
- Pydantic v2：`model_dump(mode="json")` / `model_validate()`
- 字符串：统一使用双引号 `"`
- 注释：始终用英文
- Docstring：模块级 `"""..."""`，函数级简短描述

### 模块职责
- 每个模块有明确的单一职责
- `prompts.py` 只管理 LLM 提示词模板，不混入业务逻辑
- `models.py` 只定义数据结构，不含业务方法
- `settings.py` 只处理配置读写，不含业务逻辑

### 错误处理
- 自定义异常类继承 `RuntimeError`（如 `SettingsError`、`LLMError`、`ActError`）
- 外部调用（HTTP/子进程）必须有 try-except 并附带有意义错误信息
- 事件回调失败不阻塞主流程（`orchestrator._emit` 使用 try-except pass）

### 安全约束（Act 阶段）
- 允许工具白名单：Read, Edit, Write, Grep, Glob, Restricted Bash (git diff/status, dotnet build/test, pytest)
- 禁止工具黑名单：rm, git push, git commit, git reset --hard, WebFetch, WebSearch
- 预算控制：max_budget_usd / max_turns / timeout 三重限制

### 开发原则
1. **先读后改**：修改前理解现有代码，不猜测
2. **保持简单**：只做必要变更，避免过度设计
3. **Pydantic 优先**：用 Pydantic 模型而非原始 dict
4. **提示词集中**：所有 LLM 提示词定义在 `prompts.py`
5. **状态机清晰**：FSM 状态转换集中在 `orchestrator.py` 管理
6. **自动持久化**：每阶段完成后自动保存到 SQLite
7. **事件驱动**：UI/日志通过 `on_event` 回调接收阶段事件

---

## 3. 技能路由表

`pavp_skill/` 文件夹下存放各领域专项技能。根据任务需求匹配下表，读取对应 `SKILL.md` 获取详细指引。

| 需求场景 | 触发关键词 | 路由到 skill |
|---------|-----------|-------------|
| 项目架构概览、文件结构、技术栈、编码约定 | 项目介绍、架构、技术栈、文件结构、编码规范 | `pavp_skill/overview/SKILL.md` |
| 新功能实现、重大变更、制定计划 | 计划、方案、实现、新功能、怎么做、task | `pavp_skill/writing-plans/SKILL.md` |
| 寻找/安装外部 skill、扩展能力 | 找skill、有没有skill、如何做X、扩展能力 | `pavp_skill/find-skills/SKILL.md` |
| Streamlit UI 修改、调试、设计 | ui.py、streamlit、界面、UI问题、状态问题 | `pavp_skill/streamlit-ui/SKILL.md` |
| Git 操作（add/commit/push/merge等） | git、commit、push、pull、merge、rebase、分支 | `pavp_skill/git-operations/SKILL.md` |
| 修改 .ps1 文件（run.ps1 等） | ps1、PowerShell、run.ps1、编码、BOM | `pavp_skill/ps1-encoding/SKILL.md` |
| 项目删改、重构、逻辑变更、新增/删除依赖 | 删除、重构、拆分、重命名、新增依赖、修改逻辑 | `pavp_skill/overview-aware-change/SKILL.md` |
| 更新 README.md | readme、README、文档更新、双语 | `pavp_skill/readme-rules/SKILL.md` |
| 文件编码、UTF-8 BOM、写文件/保存 | BOM、utf-8-sig、编码、encoding、写文件、保存文件 | `pavp_skill/utf8-bom-encoding/SKILL.md` |

### 路由规则

1. **精确匹配优先**：用户需求含触发关键词 -> 读取对应 skill
2. **多 skill 匹配**：需求涉及多个领域 -> 按优先级顺序处理：
   - 先读取 `overview` 获取项目上下文
   - 再读取业务相关 skill
3. **无匹配**：需求不匹配任何现有 skill -> 直接使用通用能力执行，无需路由

---

## 4. 新增 Skill 指引

当用户要求新增 skill 时，按以下步骤操作：

1. **确认 skill 名称和用途**：询问用户 skill 名称和功能描述
2. **创建目录**：`pavp_skill/<skill-name>/`
3. **创建 SKILL.md**：`pavp_skill/<skill-name>/SKILL.md`，格式如下：
   - YAML frontmatter：`name`（唯一标识）和 `description`（功能描述+触发条件，<200字符）
   - Markdown 正文：详细说明、使用指南、示例
4. **更新路由表**：将新 skill 加入上方"技能路由表"中

### 文件格式

```markdown
---
name: "<skill-name>"
description: "<功能描述，包含触发条件，不超过200字符>"
---

# <Skill Title>

<详细内容>
```

---

## 5. 常用命令

| 方法 | 命令 | 说明 |
|---|---|---|
| UI 启动 | `.\run.ps1` | 启动 Streamlit 配置面板，代理在后台运行 |
| 自启动（开机） | 注册表 Run 键 -> `wscript.exe` VBS 包装 | 隐藏启动 `python.exe`；代理进程内运行 |
