---
name: "ps1-encoding"
description: "PowerShell .ps1 file UTF-8 BOM encoding rule. Invoke when modifying run.ps1, build.ps1, or any .ps1 file in the project. Prevents PowerShell 5.x parse errors caused by missing BOM on Chinese Windows."
---

# PowerShell .ps1 File Encoding Rule

## Problem

PowerShell 5.x on Chinese Windows defaults to reading non-BOM files as **GB2312**. If a `.ps1` file containing Chinese characters is saved as **UTF-8 without BOM**, the Chinese text gets garbled, breaking string literals and causing parse errors that make the script flash-exit.

## Root Cause

- The `SearchReplace` / `Write` tools save files as UTF-8 **without BOM**.
- PowerShell 5.x uses BOM to detect UTF-8 encoding; without BOM it falls back to the system ANSI codepage (GB2312 on Chinese Windows).
- Garbled Chinese characters can introduce stray quotes/brackets, producing parser failures.

## Typical Error Symptoms

When a `.ps1` file with Chinese chars is missing BOM, you'll see errors like these from PowerShell 5.x:

```
数组索引表达式丢失或无效。           (MissingArrayIndexExpression)
Try 语句缺少自己的 Catch 或 Finally 块。(Try without Catch/Finally)
字符串缺少终止符: "。                 (Missing string terminator)
语句块或类型定义中缺少右"}"。         (Missing closing brace)
```

These errors **all point to the same root cause**: garbled Chinese text due to wrong encoding. Do NOT try to fix each error individually — just add BOM and the errors disappear.

## Rule

**Do NOT write Chinese (or any non-ASCII characters) in `run.ps1`.** All user-facing messages in `run.ps1` must be in English only. This applies to `Write-Host` strings, comments, and any other string literals. This eliminates the encoding dependency entirely for the entry-point script.

**When editing any `.ps1` file (especially `run.ps1`, `build.ps1`):**

1. After saving with `SearchReplace` or `Write`, **immediately re-add the UTF-8 BOM** using Python:

```python
data = open('<filename>.ps1', 'rb').read()
# Strip existing BOM if present, then re-add
if data.startswith(b'\xef\xbb\xbf'):
    data = data[3:]
open('<filename>.ps1', 'wb').write(b'\xef\xbb\xbf' + data)
```

Or as a one-liner:

```bash
python -c "p='<filename>.ps1';d=open(p,'rb').read();d=d[3:]if d[:3]==b'\xef\xbb\xbf'else d;open(p,'wb').write(b'\xef\xbb\xbf'+d)"
```

2. **Verify** the BOM exists:

```bash
python -c "print(open('<filename>.ps1','rb').read(3)==b'\xef\xbb\xbf')"
# Expected: True
```

3. **Verify** the script parses without errors:

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File .\<filename>.ps1
```

## Quick Check

First 3 bytes of a correctly encoded `.ps1` file should be `EF BB BF`:

```bash
python -c "data=open('<filename>.ps1','rb').read(4);print(' '.join(f'{b:02X}' for b in data))"
# Expected: EF BB BF 23  (23 = '#' first char of script)
```

## Case Study: `build.ps1` BOM Bug

This bug was encountered on 2026-07-30 when running `.\run.ps1`:

1. `run.ps1` had BOM and ran fine — it printed the banner and invoked `build.ps1`.
2. `build.ps1` was missing BOM (started with `23 20 50 41` = `# PA`).
3. PowerShell 5.x read `build.ps1` as GB2312, garbling all Chinese chars.
4. The garbled text introduced stray quotes/brackets, producing 4 parse errors.
5. Fix: added BOM (`EF BB BF`) to `build.ps1` → parsing succeeded immediately.

**Lesson**: Always check **all** `.ps1` files in the project, not just the one you're editing. If any `.ps1` file with Chinese content is missing BOM, it will fail when invoked.
