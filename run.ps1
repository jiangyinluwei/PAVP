# PAVP: Launch Streamlit UI, then auto-close terminal
# Proxy runs in background, UI is just a control panel

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  PAVP - Plan-Act-Verify-Plan" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# -- Check Python --
try {
    $null = python --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw }
} catch {
    Write-Host "[✗] Python 未安装或不在 PATH 中！" -ForegroundColor Red
    Write-Host "    请安装 Python 3.10+ (https://python.org) 并确保 'python' 命令可用。" -ForegroundColor Yellow
    Write-Host "    安装完成后重新运行本脚本。" -ForegroundColor Yellow
    pause
    exit 1
}
Write-Host "[✓] Python $(python --version 2>&1)" -ForegroundColor Green

# -- Auto-create settings.json --
$settingsPath = Join-Path $HOME ".pavp\settings.json"
if (-not (Test-Path $settingsPath)) {
    Write-Host "[*] 正在生成 settings.json 模板 ..." -ForegroundColor Yellow
    $template = @'
{
  "litellm_master_key": "sk-pavp-local",
  "proxy_port": 4001,
  "plan_api": "",
  "plan_base_url": "",
  "plan_model": "",
  "act_api": "",
  "act_base_url": "",
  "act_model": "",
  "cc_bin": "claude",
  "act_max_budget": 3.0,
  "act_max_turns": 40,
  "act_timeout": 600,
  "loop_mode": "auto",
  "auto_start": true,
  "auto_start_ui": false
}
'@
    $settingsDir = Join-Path $HOME ".pavp"
    if (-not (Test-Path $settingsDir)) { New-Item -ItemType Directory -Path $settingsDir -Force | Out-Null }
    $template | Out-File -FilePath $settingsPath -Encoding utf8
    Write-Host "[✓] 模板已生成: $settingsPath" -ForegroundColor Green
    Write-Host "    ⚠ 请编辑该文件填入 plan_* 和 act_* 密钥，否则后续工作流会失败。" -ForegroundColor Yellow
} else {
    Write-Host "[✓] 设置文件已存在: $settingsPath" -ForegroundColor Green
}

# -- Install deps if missing --
Write-Host "[*] 检查 Python 依赖 ..." -ForegroundColor Yellow
python -c "import httpx, pydantic, streamlit" 2>$null
if ($LASTEXITCODE -ne 0) {
    # -- Check pip --
    try {
        $null = pip --version 2>&1
        if ($LASTEXITCODE -ne 0) { throw }
    } catch {
        Write-Host "[✗] pip 未找到！请确保 Python 安装时已包含 pip。" -ForegroundColor Red
        Write-Host "    尝试: python -m ensurepip --upgrade" -ForegroundColor Yellow
        pause
        exit 1
    }

    Write-Host "[*] 正在安装依赖 (pip install -r requirements.txt) ..." -ForegroundColor Yellow
    pip install -r requirements.txt 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[✗] 依赖安装失败！请手动执行: pip install -r requirements.txt" -ForegroundColor Red
        pause
        exit 1
    }
    Write-Host "[✓] 依赖安装完成" -ForegroundColor Green
} else {
    Write-Host "[✓] 依赖已就绪" - ForegroundColor Green
}

# -- Launch Streamlit UI in background, then close terminal --
Write-Host "[*] 正在启动 PAVP UI ..." -ForegroundColor Yellow
Write-Host "    浏览器将自动打开 http://localhost:8501" -ForegroundColor DarkGray
Write-Host "    终端将自动关闭。" -ForegroundColor DarkGray

Start-Process -FilePath "python" `
    -ArgumentList "-m streamlit run pavp/ui.py" `
    -WorkingDirectory $root `
    -WindowStyle Hidden

Start-Sleep -Seconds 3