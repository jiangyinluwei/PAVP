# PAVP: Auto-build if needed, then launch the bundled executable
# If the exe is not yet built, run build.ps1 first.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$exePath = Join-Path $root (Join-Path "dist" (Join-Path "pavp" "pavp.exe"))

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  PAVP - Plan-Act-Verify-Plan" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# -- Check if the built exe exists --
if (-not (Test-Path $exePath)) {
    Write-Host "[*] Executable not found, building..." -ForegroundColor Yellow

    # -- Check Python (needed for building) --
    try {
        $null = python --version 2>&1
        if ($LASTEXITCODE -ne 0) { throw }
    } catch {
        Write-Host "[✗] Build requires Python, but Python is not installed or not in PATH!" -ForegroundColor Red
        Write-Host "    Please install Python 3.10+ (https://python.org) and ensure the 'python' command is available." -ForegroundColor Yellow
        Write-Host "    Re-run this script after installation." -ForegroundColor Yellow
        pause
        exit 1
    }
    Write-Host "[✓] Python $(python --version 2>&1)" -ForegroundColor Green

    # -- Run build script --
    $buildScript = Join-Path $root "build.ps1"
    & $buildScript
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[✗] Build failed. Check the logs above." -ForegroundColor Red
        pause
        exit 1
    }
    Write-Host "[✓] Build completed" -ForegroundColor Green
} else {
    Write-Host "[✓] Executable ready: $exePath" -ForegroundColor Green
}

# -- Launch the bundled executable --
Write-Host "[*] Starting PAVP UI..." -ForegroundColor Yellow
Write-Host "    Browser will auto-open http://localhost:8501" -ForegroundColor DarkGray
Write-Host "    Terminal will auto-close." -ForegroundColor DarkGray

Start-Process -FilePath $exePath `
    -WorkingDirectory $root `
    -WindowStyle Hidden

Start-Sleep -Seconds 3