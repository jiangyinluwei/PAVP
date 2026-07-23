# PAVP: Launch Streamlit UI, then auto-close terminal
# Proxy runs in background, UI is just a control panel

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  PAVP - Plan-Act-Verify-Plan" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# -- Auto-create settings.json --
$settingsPath = Join-Path $HOME ".pavp\settings.json"
if (-not (Test-Path $settingsPath)) {
    Write-Host "[*] Creating settings.json ..." -ForegroundColor Yellow
    python -m pavp --init 2>&1 | Out-Null
}

# -- Install deps if missing --
python -c "import fastapi, uvicorn, httpx, streamlit" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[*] Installing dependencies ..." -ForegroundColor Yellow
    pip install fastapi uvicorn httpx streamlit 2>&1 | Out-Null
}

# -- Launch Streamlit UI in background, then close terminal --
Write-Host "[*] Launching PAVP UI ..." -ForegroundColor Yellow
Write-Host "    Browser will open http://localhost:8501" -ForegroundColor DarkGray
Write-Host "    Terminal will close automatically." -ForegroundColor DarkGray

Start-Process -FilePath "python" `
    -ArgumentList "-m streamlit run pavp/ui.py" `
    -WorkingDirectory $root `
    -WindowStyle Hidden

Start-Sleep -Seconds 3
