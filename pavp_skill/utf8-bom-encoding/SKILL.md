---
name: "utf8-bom-encoding"
description: "UTF-8 BOM encoding rule for all file save operations. Invoke when writing, saving, or creating any text file in the project. Ensures all output files are UTF-8 BOM for Chinese Windows compatibility."
---

# UTF-8 BOM 编码规则

## 核心规则

**任何保存文档行为，都必须保证保存结果为 UTF-8 BOM。**

这意味着项目中的所有文本文件（`.json`、`.ps1`、`.py`、`.log` 等），写入时必须使用 `encoding="utf-8-sig"`，读取时必须使用 `encoding="utf-8-sig"`。

## 原因

1. **PowerShell 5.x 兼容性**：中文 Windows 上 PowerShell 5.x 默认以 GB2312 读取无 BOM 文件，导致中文乱码和语法解析错误
2. **跨工具兼容性**：Windows 记事本、VS Code 等工具默认输出 UTF-8 BOM，项目文件可能被这些工具编辑后自带 BOM
3. **统一编码行为**：消除"有的文件有 BOM、有的没有"的不一致状态，避免 `json.load()` 等解析器因 BOM 报错

## 读写规范

### 写入文件

```python
# 正确：使用 utf-8-sig 输出 BOM
path.write_text(content, encoding="utf-8-sig")

# 错误：不加 BOM
path.write_text(content, encoding="utf-8")
```

### 读取文件

```python
# 正确：使用 utf-8-sig 自动去除 BOM（兼容有无 BOM 两种情况）
data = path.read_text(encoding="utf-8-sig")

# 错误：用 utf-8 读取有 BOM 的文件，BOM 字节会混入内容
data = path.read_text(encoding="utf-8")
```

### JSON 文件特殊处理

```python
# 读取 JSON（utf-8-sig 自动去除 BOM，json.loads 正常解析）
data = json.loads(path.read_text(encoding="utf-8-sig"))

# 写入 JSON（utf-8-sig 添加 BOM）
path.write_text(
    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8-sig",
)
```

## 适用文件类型

| 文件类型 | 是否需要 BOM | 理由 |
|---------|-------------|------|
| `.json` (settings.json, state.json 等) | 是 | 可能被 Windows 工具编辑后自带 BOM，统一使用 utf-8-sig 读写 |
| `.ps1` (run.ps1 等) | 是 | PowerShell 5.x 中文 Windows 必须 BOM，详见 `ps1-encoding` skill |
| `.log` | 是 | 统一编码，避免读取时 BOM 判断问题 |
| `.py` | 是 | 统一编码 |
| PID/port 等纯数字文件 | 否 | 仅含 ASCII 数字，无 BOM 不影响解析 |

## 相关 Skill

- `ps1-encoding`：PowerShell .ps1 文件专门的 BOM 处理规则（更详细的 PowerShell 场景说明）

## 反例：BOM 导致的问题

```python
# 如果文件以 UTF-8 BOM 保存，但用 utf-8 读取：
data = path.read_text(encoding="utf-8")
# data 开头会有 \ufeff 字符
json.loads(data)
# → JSONDecodeError: Unexpected UTF-8 BOM (decode using utf-8-sig)
```

## 检查 BOM

```bash
# 检查文件前 3 字节是否为 EF BB BF (UTF-8 BOM)
python -c "d=open('file.json','rb').read(3);print(d==b'\xef\xbb\xbf')"
```
