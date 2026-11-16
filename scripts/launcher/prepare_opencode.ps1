# Explicit optional install. Never change the default distribution used by Codex.
$ErrorActionPreference = "Stop"
try {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $TargetFile = Join-Path $ProjectRoot "data\opencode-wsl-target.json"
    $Distro = ""
    if (Test-Path -LiteralPath $TargetFile) {
        $Distro = (Get-Content -LiteralPath $TargetFile -Raw | ConvertFrom-Json).distribution
    }
    $Distros = & wsl.exe --list --quiet 2>$null
    $Names = @(($Distros -join "`n").Replace([string][char]0, '').Split("`n") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if (-not $Distro -and $LASTEXITCODE -eq 0 -and $Names.Count) {
        $DefaultName = (& wsl.exe --exec printenv WSL_DISTRO_NAME 2>$null)
        $Distro = ($DefaultName -join '').Trim()
        & wsl.exe --exec sh -c 'command -v apt-get >/dev/null' 2>$null
        if ($LASTEXITCODE -ne 0) { $Distro = "Ubuntu" }
    }
    if (-not $Distro) { $Distro = "Ubuntu" }
    if ($Distro.Length -gt 128 -or $Distro.Contains("`n") -or $Distro.Contains("`r")) { throw "Invalid WSL target." }
    if ($Names -notcontains $Distro) {
        # Bootstrap only the supported Ubuntu distribution, not arbitrary browser input.
        if ($Distro -ne "Ubuntu") { throw "The configured WSL distribution is missing; restore it or clear the OpenCode target setting." }
        Write-Host "Installing WSL and Ubuntu. Windows may request administrator permission."
        $Install = Start-Process -FilePath "wsl.exe" -ArgumentList @("--install", "-d", "Ubuntu", "--no-launch") -Verb RunAs -Wait -PassThru
        if ($Install.ExitCode -notin @(0, 3010)) { throw "WSL installation failed." }
        if ($Install.ExitCode -eq 3010) {
            Write-Host "Restart Windows, then click Install OpenCode again to continue."
            exit 3010
        }
        Write-Host "Complete Ubuntu's first-time Linux user setup, then type exit to continue installation."
        & wsl.exe -d $Distro
        if ($LASTEXITCODE -ne 0) { throw "Complete WSL initialization and retry." }
    }
    $Kernel = (& wsl.exe -d $Distro --exec uname -r) -join ''
    if ($LASTEXITCODE -ne 0) { throw "WSL is not ready. Restart Windows if requested, then retry." }
    if ($Kernel -notmatch 'microsoft-standard|WSL2') {
        Write-Host "Converting the selected distribution to WSL 2. This may take several minutes."
        & wsl.exe --set-version $Distro 2
        if ($LASTEXITCODE -ne 0) { throw "WSL 2 preparation failed. Follow the Windows diagnostic and retry." }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetFile) | Out-Null
    $Temporary = $TargetFile + ".tmp"
    @{distribution=$Distro} | ConvertTo-Json | Set-Content -LiteralPath $Temporary -Encoding UTF8
    Move-Item -LiteralPath $Temporary -Destination $TargetFile -Force
    $LinuxScript = (& wsl.exe -d $Distro --exec wslpath -a -u (Join-Path $ProjectRoot "scripts\launcher\prepare_linux.sh")).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $LinuxScript.StartsWith('/')) { throw "Cannot locate project inside WSL." }
    & wsl.exe -d $Distro --exec bash $LinuxScript opencode
    if ($LASTEXITCODE -ne 0) { throw "OpenCode installation failed. Check the preceding output and retry." }
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host "Press Enter to return to Mabobot"
    exit 1
}
