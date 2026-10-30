# Opened explicitly by the setup wizard. Never reboot the user's machine.
$ErrorActionPreference = "Stop"
try {
    Write-Host "Mabobot: installing WSL and Ubuntu. Windows may ask for administrator permission."
    $Process = Start-Process -FilePath "wsl.exe" -ArgumentList @("--install", "-d", "Ubuntu", "--no-launch") -Verb RunAs -Wait -PassThru
    if ($Process.ExitCode -notin @(0, 3010)) {
        throw "WSL installer exited with code $($Process.ExitCode)."
    }
    Write-Host "Save your work and restart Windows if requested, then open START.bat to continue."
    Read-Host "Press Enter to return to Mabobot"
    exit 0
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host "Press Enter to return and retry"
    exit 1
}
