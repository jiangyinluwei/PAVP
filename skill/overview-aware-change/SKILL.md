---
name: "overview-aware-change"
description: "Project change management with overview awareness. Activated when the project needs modifications (delete, restructure, logic changes). Reads overview skill before planning, and updates it in real-time after changes complete."
---

# Overview-Aware Change Management Skill

## 职责

当项目需要发生删改、逻辑变更时，确保变更与项目整体架构一致，并保持 overview skill 文件始终反映项目的真实状态。

## 行为

1. **变更前：读取 overview** — 在执行任何项目删改或逻辑变更之前，必须先读取 `skill/overview/SKILL.md` 文件，理解当前项目的架构、文件结构、技术栈和编码约定，据此评估变更的影响范围和规划实施方案。

2. **变更后：更新 overview** — 当变更任务完成并且验证通过后，必须实时更新 `skill/overview/SKILL.md` 文件，确保其中记录的：
   - 文件结构（新增/删除/重命名的文件）
   - 技术栈变更（新增/移除的依赖或技术）
   - 架构调整（模块职责变更、新增入口点等）
   - 数据模型变更
   - 配置字段变更
   - 启动方式变更

3. **更新粒度**：仅更新实际发生变化的内容，不触及无关章节。

## 触发条件

- 用户要求删除、重命名、拆分或合并项目文件
- 用户要求重构模块（改变文件职责、提取新模块等）
- 用户要求修改核心逻辑（影响数据流、状态机、API 接口等）
- 用户要求新增/删除依赖
- 用户要求修改项目配置结构
- 其他任何会影响 `skill/overview/SKILL.md` 所描述内容的变更

## 使用示例

### 场景：用户要求拆分一个大文件

```
1. 读取 skill/overview/SKILL.md，了解当前文件结构和模块职责
2. 规划拆分方案（新文件命名、职责边界）
3. 执行拆分
4. 验证结果
5. 更新 skill/overview/SKILL.md 中的文件结构和相关描述
```

### 场景：用户要求添加新模块

```
1. 读取 skill/overview/SKILL.md，确认新模块在架构中的位置
2. 按照项目编码约定创建新模块
3. 验证集成
4. 更新 skill/overview/SKILL.md（文件结构、技术栈、启动方式等）
```

## 与其他 skill 的关系

- 本 skill 依赖 `overview` skill 提供项目上下文
- 本 skill 可与 `writing-plans` skill 配合，在计划阶段引用 overview 进行架构评估
- 本 skill 的触发时机由统筹协调器（orchestrator）判断
