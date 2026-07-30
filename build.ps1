# PAVP build script - creates standalone executable with PyInstaller
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$iconPath = Join-Path $root (Join-Path "Assent" "pavp.ico")
$buildDir = Join-Path $root "dist"

# ------------------------------------------------------------
# 1. Check / install PyInstaller
# ------------------------------------------------------------
Write-Host "[*] Checking PyInstaller ..." -ForegroundColor Yellow
try {
    $null = pyinstaller --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw }
    Write-Host "[✓] PyInstaller 已就绪" -ForegroundColor Green
} catch {
    Write-Host "[*] 正在安装 PyInstaller ..." -ForegroundColor Yellow
    pip install pyinstaller 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[✗] PyInstaller 安装失败！请手动执行: pip install pyinstaller" -ForegroundColor Red
        pause
        exit 1
    }
    Write-Host "[✓] PyInstaller 安装完成" -ForegroundColor Green
}

# ------------------------------------------------------------
# 2. Clean old build artifacts
# ------------------------------------------------------------
Write-Host "[*] 清理旧构建 ..." -ForegroundColor Yellow
$oldBuildDir = Join-Path $root "build"
if (Test-Path $oldBuildDir) {
    Remove-Item -Recurse -Force $oldBuildDir
}
if (Test-Path $buildDir) {
    Remove-Item -Recurse -Force $buildDir
}
# Also clean PyInstaller's working directories
$specFile = Join-Path $root "pavp.spec"
if (Test-Path $specFile) { Remove-Item -Force $specFile }
$pycacheDir = Join-Path $root "__pycache__"
if (Test-Path $pycacheDir) { Remove-Item -Recurse -Force $pycacheDir }

# ------------------------------------------------------------
# 3. Build executable (with spinner)
# ------------------------------------------------------------
Write-Host "[*] 正在构建 PAVP 可执行文件 ..." -ForegroundColor Yellow
Write-Host "    图标: $iconPath" -ForegroundColor DarkGray
Write-Host "    输出: $buildDir\pavp\" -ForegroundColor DarkGray

# Run PyInstaller via System.Diagnostics.Process with async output reading.
# The spinner runs in the main thread at a fixed 0.5s interval,
# independent of PyInstaller output speed.
$shared = [hashtable]::Synchronized(@{
    Text = "启动 PyInstaller..."
    Log  = New-Object System.Collections.ArrayList
})

# Build PyInstaller argument string
$pyArgs = @(
    '--onedir',
    '--name', 'pavp',
    '--distpath', "`"$buildDir`"",
    '--workpath', "`"$buildDir\_work`"",
    '--icon', "`"$iconPath`"",
    '--add-data', '"pavp;./pavp"',
    '--hidden-import', 'pavp.ui',
    '--hidden-import', 'pavp.settings',
    '--hidden-import', 'pavp.proxy_server',
    '--hidden-import', 'pavp.auto_start',
    '--hidden-import', 'pavp.engine',
    '--hidden-import', 'pavp.orchestrator',
    '--hidden-import', 'pavp.act_executor',
    '--hidden-import', 'pavp.storage',
    '--hidden-import', 'pavp.prompts',
    '--hidden-import', 'pavp.models',
    '--hidden-import', 'pavp.entry_point',
    '--hidden-import', 'streamlit.web.cli',
    '--collect-all', 'streamlit',
    '--collect-all', 'httpx',
    '--noconfirm',
    'pavp/entry_point.py'
) -join ' '

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo.FileName = "cmd.exe"
$proc.StartInfo.Arguments = "/c pyinstaller $pyArgs 2>&1"
$proc.StartInfo.UseShellExecute = $false
$proc.StartInfo.RedirectStandardOutput = $true
$proc.StartInfo.RedirectStandardError = $true
$proc.StartInfo.CreateNoWindow = $true
$proc.StartInfo.WorkingDirectory = $root

# Register async output events - update shared state
Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -MessageData $shared -Action {
    if ($EventArgs.Data) {
        $line = $EventArgs.Data.TrimEnd()
        $Event.MessageData.Text = $line
        [void]$Event.MessageData.Log.Add($line)
    }
} | Out-Null

Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -MessageData $shared -Action {
    if ($EventArgs.Data) {
        $line = $EventArgs.Data.TrimEnd()
        $Event.MessageData.Text = $line
        [void]$Event.MessageData.Log.Add($line)
    }
} | Out-Null

# Start process and begin async reading
$null = $proc.Start()
$proc.BeginOutputReadLine()
$proc.BeginErrorReadLine()

# Spinner loop in main thread - runs at fixed 0.5s interval
$spinner = @('\', '|', '/', '-')
$spinIdx = 0

while (-not $proc.HasExited) {
    $spinIdx = ($spinIdx + 1) % $spinner.Count
    $text = $shared.Text
    $w = $Host.UI.RawUI.WindowSize.Width
    if (-not $w -or $w -le 0) { $w = 80 }
    $maxLen = $w - 6
    if ($text.Length -gt $maxLen) {
        $text = $text.Substring(0, $maxLen - 3) + "..."
    }
    Write-Host -NoNewline ("`r{0} {1}   " -f $spinner[$spinIdx], $text)
    Start-Sleep -Milliseconds 500
}

$proc.WaitForExit()
$pyExitCode = $proc.ExitCode

# Unregister events
Get-EventSubscriber | Where-Object { $_.SourceObject -eq $proc } | Unregister-Event -Force
$proc.Dispose()

# Copy collected log
$script:buildLog = $shared.Log.ToArray()

# Clear spinner line
Write-Host ""

if ($pyExitCode -ne 0) {
    Write-Host ""
    Write-Host "--- 构建日志 ---" -ForegroundColor Yellow
    foreach ($l in $script:buildLog) { Write-Host $l }
    Write-Host "--- 日志结束 ---" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "[✗] 构建失败！" -ForegroundColor Red
    exit 1
}

Write-Host "[✓] 构建完成" -ForegroundColor Green

# ------------------------------------------------------------
# 4. Verify result
# ------------------------------------------------------------
$exePath = Join-Path $buildDir (Join-Path "pavp" "pavp.exe")
if (Test-Path $exePath) {
    Write-Host "[✓] 构建成功！" -ForegroundColor Green
    Write-Host "    可执行文件: $exePath" -ForegroundColor Green
    # Show file size
    $fileInfo = Get-Item $exePath
    $sizeMB = [math]::Round($fileInfo.Length / 1MB, 1)
    Write-Host "    文件大小: ${sizeMB} MB" -ForegroundColor Green
} else {
    Write-Host "[✗] 构建失败：未找到输出文件 $exePath" -ForegroundColor Red
    pause
    exit 1
}

# ------------------------------------------------------------
# 5. Clean spec file
# ------------------------------------------------------------
if (Test-Path $specFile) { Remove-Item -Force $specFile }

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  构建完成！运行方式:" -ForegroundColor Cyan
Write-Host "    PowerShell: .\run.ps1" -ForegroundColor Cyan
Write-Host "    直接运行:   $exePath" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

exit 0