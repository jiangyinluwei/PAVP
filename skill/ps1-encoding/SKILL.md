---
name: "ps1-encoding"
description: "PowerShell .ps1 file UTF-8 BOM encoding rule. Invoke when modifying run.ps1 or any .ps1 file in the project. Prevents PowerShell 5.x parse errors caused by missing BOM on Chinese Windows."
---

# PowerShell .ps1 File Encoding Rule

## Problem

PowerShell 5.x on Chinese Windows defaults to reading non-BOM files as **GB2312**. If a `.ps1` file containing Chinese characters is saved as **UTF-8 without BOM**, the Chinese text gets garbled, breaking string literals and causing parse errors (e.g. "字符串缺少终止符", "数组索引表达式丢失或无效") that make the script flash-exit.

## Root Cause

- The `SearchReplace` / `Write` tools save files as UTF-8 **without BOM**.
- PowerShell 5.x uses BOM to detect UTF-8 encoding; without BOM it falls back to the system ANSI codepage (GB2312 on Chinese Windows).
- Garbled Chinese characters can introduce stray quotes/brackets, producing parser failures.

## Rule

**When editing any `.ps1` file (especially `run.ps1`):**

1. After saving with `SearchReplace` or `Write`, **immediately re-add the UTF-8 BOM** using Python:

```python
data = open('run.ps1', 'rb').read()
# Strip existing BOM if present, then re-add
if data.startswith(b'\xef\xbb\xbf'):
    data = data[3:]
open('run.ps1', 'wb').write(b'\xef\xbb\xbf' + data)
```

Or as a one-liner:

```bash
python -c "p='run.ps1';d=open(p,'rb').read();d=d[3:]if d[:3]==b'\xef\xbb\xbf'else d;open(p,'wb').write(b'\xef\xbb\xbf'+d)"
```

2. **Verify** the BOM exists:

```bash
python -c "print(open('run.ps1','rb').read(3)==b'\xef\xbb\xbf')"
```

3. **Verify** the script parses without errors:

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

## Quick Check

First 3 bytes of a correctly encoded `.ps1` file should be `EF BB BF`:

```bash
python -c "data=open('run.ps1','rb').read(4);print(' '.join(f'{b:02X}' for b in data))"
# Expected: EF BB BF 23  (23 = '#' first char of script)
```
