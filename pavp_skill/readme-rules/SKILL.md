---
name: "readme-rules"
description: "README.md update rules and conventions. Activated when updating README.md — covers bilingual structure, content placement, concise description style, and formatting consistency."
---

# README.md 更新准则

## 核心原则

### 1. 双语结构

README.md 使用**中文在前、英文在后**的结构。新增内容必须同时提供中英文版本，英文在前一个中文段落/列表之后，用 `---` 分隔。

**正确示例：**

```markdown
### 3. 使用方式

1. **运行启动脚本**：双击或运行 `run.ps1`，自动检查 Python 环境...

---

### 3. How to Use

1. **Run launcher**: Run `run.ps1` — it auto-checks the Python environment...
```

### 2. 融入现有章节

新内容**不应作为独立章节追加在文件末尾**，而应融入"项目介绍"中对应的子章节：

| 内容类型 | 目标章节 |
|---|---|
| `run.ps1` 启动脚本、环境检查 | 放入 `### 3. 使用方式` / `### 3. How to Use` |
| `task_key`、核心机制、架构细节 | 放入 `### 1. 项目逻辑` / `### 1. Architecture` |
| 环境依赖、平台兼容性 | 放入 `### 2. 环境要求` / `### 2. Environment` |
| 进程管理 | 放入 `### 4. 关闭进程` / `### 4. Stopping the Process` |

### 3. 简洁描述

描述应精简，用**一两句话**说明核心要点即可，不需要展开成完整文档（如流程图、表格、详细 API 说明等）。

**正确示例（简洁）：**
> 每个新任务会生成唯一 `task_key`（如 `TK-a1b2c3d4`），Plan 模型输出到 JSON 中，用于跨轮次锚定上下文、缓存隔离和状态追踪。

**错误示例（冗长）：**
> 每个新任务会生成唯一 task_key，格式为 TK-xxxxxxxx，由 Plan 模型在生成计划 JSON 时自动创建……

### 4. 保持格式一致

- 标题层级：`###` 子章节 → `####` 次级 → `-` 列表
- 代码/配置引用用 `` ` `` 或 ``` ``` ``` 代码块
- 列表风格：`-` 或 `1.`，与上下文一致
- 语言：中文部分保持中文标点，英文部分保持英文标点
- 无需修改 `## Quick Start`、`## Project Structure` 等已有章节的结构

### 5. 文件位置

- 不要修改 `Project Structure` 章节的文件列表，它只反映项目目录结构

---

## README.md Update Rules

### 1. Bilingual Structure

README.md uses a **Chinese first, English second** structure. All new content must provide both Chinese and English versions, separated by `---`.

### 2. Integrate into Existing Sections

**Do NOT append new content as standalone sections at the end of the file.** Instead, integrate content into the corresponding subsection of "项目介绍" / "Project Introduction":

| Content Type | Target Section |
|---|---|
| `run.ps1` launcher, environment checks | `### 3. 使用方式` / `### 3. How to Use` |
| `task_key`, core mechanisms, architecture details | `### 1. 项目逻辑` / `### 1. Architecture` |
| Environment dependencies, platform compatibility | `### 2. 环境要求` / `### 2. Environment` |
| Process management | `### 4. 关闭进程` / `### 4. Stopping the Process` |

### 3. Concise Descriptions

Keep descriptions to **one or two sentences** covering the core point. Avoid expanding into full documentation (flowcharts, tables, detailed API docs, etc.).

### 4. Maintain Formatting Consistency

- Heading hierarchy: `###` subsection → `####` sub-section → `-` lists
- Code/config references use `` ` `` or ``` ``` ``` code blocks
- List style: `-` or `1.`, consistent with context
- Language: Chinese sections use Chinese punctuation, English sections use English punctuation
- Do not modify the structure of `## Quick Start` or `## Project Structure`

### 5. File Structure

- Do not modify the file list in `Project Structure` — it only reflects the project directory layout