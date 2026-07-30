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
    Write-Host "[*] 未找到可执行文件，正在构建 ..." -ForegroundColor Yellow

    # -- Check Python (needed for building) --
    try {
        $null = python --version 2>&1
        if ($LASTEXITCODE -ne 0) { throw }
    } catch {
        Write-Host "[✗] 构建需要 Python，但 Python 未安装或不在 PATH 中！" -ForegroundColor Red
        Write-Host "    请安装 Python 3.10+ (https://python.org) 并确保 'python' 命令可用。" -ForegroundColor Yellow
        Write-Host "    安装完成后重新运行本脚本。" -ForegroundColor Yellow
        pause
        exit 1
    }
    Write-Host "[✓] Python $(python --version 2>&1)" -ForegroundColor Green

    # -- Run build script --
    $buildScript = Join-Path $root "build.ps1"
    & $buildScript
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[✗] 构建失败，请检查上方日志。" -ForegroundColor Red
        pause
        exit 1
    }
    Write-Host "[✓] 构建完成" -ForegroundColor Green
} else {
    Write-Host "[✓] 可执行文件已就绪: $exePath" -ForegroundColor Green
}

# -- Launch the bundled executable --
Write-Host "[*] 正在启动 PAVP UI ..." -ForegroundColor Yellow
Write-Host "    浏览器将自动打开 http://localhost:8501" -ForegroundColor DarkGray
Write-Host "    终端将自动关闭。" -ForegroundColor DarkGray

Start-Process -FilePath $exePath `
    -WorkingDirectory $root `
    -WindowStyle Hidden

Start-Sleep -Seconds 3