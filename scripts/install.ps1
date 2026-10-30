param(
    [switch]$Force,
    [string]$StatusFile = "",
    [switch]$InstallMissingRuntimes,
    [switch]$IncludeOptionalComponents
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $ProjectRoot "requirements.txt"
$HashMarker = Join-Path $VenvDir ".requirements.sha256"
$RuntimeModule = Join-Path $ProjectRoot "scripts\launcher\runtime_bootstrap.psm1"
$PlaywrightReady = $false
$script:CurrentInstallPercent = 0
$script:CurrentInstallMessage = "正在检查运行环境…"

function Write-InstallerStatus(
    [string]$Message,
    [int]$Percent = -1,
    [string]$Detail = ""
) {
    if ($Percent -ge 0) {
        $script:CurrentInstallPercent = [Math]::Max(0, [Math]::Min(100, $Percent))
    }
    if ($Message) {
        $script:CurrentInstallMessage = $Message
    }
    if (-not $StatusFile) {
        return
    }

    $Payload = [ordered]@{
        message = $script:CurrentInstallMessage
        percent = $script:CurrentInstallPercent
        detail = $Detail
        updated_at = (Get-Date).ToString("o")
        process_id = $PID
    }
    Set-Content -LiteralPath $StatusFile -Value ($Payload | ConvertTo-Json -Compress) -Encoding UTF8
}

function Write-Step([string]$Message, [int]$Percent = -1) {
    Write-InstallerStatus -Message $Message -Percent $Percent
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Stop-Install([string]$Message) {
    Write-InstallerStatus -Message $Message
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    exit 1
}

function Remove-StaleLiteLLMMetadata {
    $SitePackages = Join-Path $VenvDir "Lib\site-packages"
    if (-not (Test-Path $SitePackages)) {
        return
    }

    $StaleEntries = @(
        Get-ChildItem -LiteralPath $SitePackages -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "~itellm*" }
    )
    foreach ($Entry in $StaleEntries) {
        Remove-Item -LiteralPath $Entry.FullName -Recurse -Force
        Write-Step "已清理损坏的 LiteLLM 安装残留：$($Entry.Name)"
    }
}

Set-Location $ProjectRoot
Write-InstallerStatus -Message "正在检查运行环境…" -Percent 2

if (-not (Test-Path $Requirements)) {
    Stop-Install "requirements.txt 不存在。"
}
if (-not (Test-Path -LiteralPath $RuntimeModule)) {
    Stop-Install "基础环境引导模块不存在：$RuntimeModule"
}
Import-Module $RuntimeModule -Force

Remove-StaleLiteLLMMetadata

$RequirementHash = (Get-FileHash $Requirements -Algorithm SHA256).Hash
$EnvironmentReady = $false
$VenvPythonInfo = if (Test-Path -LiteralPath $VenvPython) {
    Get-MabobotPythonInfo -FilePath $VenvPython
}
else {
    $null
}
if (-not $Force -and $null -ne $VenvPythonInfo -and (Test-Path $HashMarker)) {
    $SavedHash = (Get-Content $HashMarker -Raw).Trim()
    if ($SavedHash -eq $RequirementHash) {
        & $VenvPython -c "import fastapi, flask, mabowx, playwright, static_ffmpeg, yt_dlp, youtube_transcript_api, json_repair, webview, pystray, win32api, win32con, win32event, win32gui, win32process, win32security, win32ts, win32ui" 2>$null
        $EnvironmentReady = ($LASTEXITCODE -eq 0)
        if ($EnvironmentReady) {
            & $VenvPython -c "from pathlib import Path; from playwright.sync_api import sync_playwright; p = sync_playwright().start(); path = p.chromium.executable_path; p.stop(); raise SystemExit(0 if Path(path).is_file() else 1)" 2>$null
            $PlaywrightReady = ($LASTEXITCODE -eq 0)
        }
    }
}

if (-not $EnvironmentReady) {
    Remove-Item -LiteralPath $HashMarker -Force -ErrorAction SilentlyContinue
    if ($null -eq $VenvPythonInfo) {
        $PythonInfo = Get-MabobotCompatiblePython
        if ($null -eq $PythonInfo -and $InstallMissingRuntimes) {
            try {
                $PythonInfo = Install-MabobotPython -Status {
                    param([string]$Message)
                    Write-Step $Message
                }
            }
            catch {
                Stop-Install "Python 自动安装失败：$($_.Exception.Message)"
            }
        }
        if ($null -eq $PythonInfo) {
            Stop-Install "未找到 64 位 Python 3.11/3.12。请通过 START.bat 启动，并同意自动准备缺失组件。"
        }

        if (Test-Path -LiteralPath $VenvDir) {
            $BackupName = ".venv.invalid-{0}" -f (Get-Date -Format "yyyyMMdd-HHmmssfff")
            $BackupPath = Join-Path $ProjectRoot $BackupName
            Move-Item -LiteralPath $VenvDir -Destination $BackupPath
            Write-Step "原虚拟环境不可用，已保留为 $BackupName"
        }
        Write-Step "创建虚拟环境 .venv" 8
        & $PythonInfo.FilePath @($PythonInfo.Arguments) -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            Stop-Install "创建虚拟环境失败。"
        }
        $VenvPythonInfo = Get-MabobotPythonInfo -FilePath $VenvPython
        if ($null -eq $VenvPythonInfo) {
            Stop-Install "虚拟环境已创建，但 Python 运行检查失败。"
        }
    }

    Write-Step "更新 pip、setuptools 和 wheel" 12
    & $VenvPython -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "pip 基础工具更新失败，请检查网络或代理。"
    }

    Write-Step "安装项目依赖（首次运行可能需要数分钟）" 20
    & $VenvPython -m pip install -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "依赖安装失败，请检查网络、Python 版本和上方 pip 错误。"
    }

    Write-Step "项目依赖安装完成，正在验证关键模块" 52

    & $VenvPython -c "import fastapi, flask, mabowx, playwright, static_ffmpeg, yt_dlp, youtube_transcript_api, json_repair, webview, pystray, win32api, win32con, win32event, win32gui, win32process, win32security, win32ts, win32ui"
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "依赖安装完成，但关键模块导入检查失败。"
    }

}
else {
    Write-Step "虚拟环境和依赖已就绪，跳过重复安装" 55
}

Write-Step "检查 Microsoft WebView2 Runtime" 58
$WebView2Version = Get-MabobotWebView2Version
if (-not $WebView2Version) {
    if (-not $InstallMissingRuntimes) {
        Stop-Install "未检测到 Microsoft Edge WebView2 Runtime。请通过 START.bat 启动，并同意自动准备缺失组件。"
    }
    try {
        $WebView2Version = Install-MabobotWebView2Runtime -Status {
            param([string]$Message)
            Write-Step $Message
        }
    }
    catch {
        Stop-Install "Microsoft WebView2 Runtime 自动安装失败：$($_.Exception.Message)"
    }
}
Write-Step "Microsoft WebView2 Runtime 已就绪（$WebView2Version）" 62

$EnvFile = Join-Path $ProjectRoot ".env"
$EnvExample = Join-Path $ProjectRoot ".env.example"
if (-not (Test-Path $EnvFile)) {
    Copy-Item $EnvExample $EnvFile
    Write-Step "已从 .env.example 创建本地 .env"
}

foreach ($Directory in @("data", "logs", "tmp")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot $Directory) | Out-Null
}

# This marker covers the base runtime only; optional tools are verified by the wizard.
Set-Content -Path $HashMarker -Value $RequirementHash -Encoding ASCII

if ($IncludeOptionalComponents) {
    if (-not $PlaywrightReady) {
        Write-Step "安装链接摘要脑图所需的 Chromium（首次运行需要下载）" 65
        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $VenvPython -m playwright install chromium 2>&1 | ForEach-Object {
                $Line = [string]$_
                Write-Host $Line
                $OverallPercent = -1
                if ($Line -match '(?<!\d)(\d{1,3})%(?!\d)') {
                    $DownloadPercent = [Math]::Max(0, [Math]::Min(100, [int]$Matches[1]))
                    $OverallPercent = 65 + [Math]::Floor(($DownloadPercent * 18) / 100)
                }
                Write-InstallerStatus `
                    -Message "正在下载并安装 Chromium" `
                    -Percent $OverallPercent `
                    -Detail $Line
            }
            $PlaywrightExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if ($PlaywrightExitCode -ne 0) {
            Stop-Install "Playwright Chromium 安装失败，请检查网络或代理后重试。"
        }

        Write-Step "Chromium 下载完成，正在验证浏览器" 84
        & $VenvPython -c "from pathlib import Path; from playwright.sync_api import sync_playwright; p = sync_playwright().start(); path = p.chromium.executable_path; p.stop(); raise SystemExit(0 if Path(path).is_file() else 1)"
        if ($LASTEXITCODE -ne 0) {
            Stop-Install "Playwright 已执行安装，但 Chromium 运行时检查失败。"
        }
    }

    Write-Step "准备链接摘要所需的 FFmpeg/FFprobe（首次运行需要下载）" 88
    & $VenvPython -c "import subprocess; from static_ffmpeg import run; ffmpeg, ffprobe = run.get_or_fetch_platform_executables_else_raise(); subprocess.run([ffmpeg, '-version'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); subprocess.run([ffprobe, '-version'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(f'FFmpeg: {ffmpeg}'); print(f'FFprobe: {ffprobe}')"
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "FFmpeg/FFprobe 自动安装或运行检查失败，请检查网络、代理或安全软件后重试。"
    }

}
else {
    Write-Step "基础环境已就绪；浏览器与媒体组件可在配置向导中按需安装" 98
}

Write-InstallerStatus -Message "运行环境准备完成" -Percent 100
Write-Host "[OK] Mabobot 运行环境准备完成：$VenvPython" -ForegroundColor Green
exit 0
