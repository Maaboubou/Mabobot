param(
    [switch]$Startup
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
$Requirements = Join-Path $ProjectRoot "requirements.txt"
$HashMarker = Join-Path $VenvDir ".requirements.sha256"
$InstallScript = Join-Path $ProjectRoot "scripts\install.ps1"
$RuntimeModule = Join-Path $ProjectRoot "scripts\launcher\runtime_bootstrap.psm1"
$AppIcon = Join-Path $ProjectRoot "mabobot_launcher\assets\mabobot.ico"
$AppIconImage = Join-Path $ProjectRoot "mabobot_launcher\assets\mabobot-icon.png"
$LogDir = Join-Path $ProjectRoot "logs"
$BootstrapLog = Join-Path $LogDir "launcher_bootstrap.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-BootstrapLog([string]$Message) {
    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $BootstrapLog -Value "$Timestamp $Message" -Encoding UTF8
}

function Close-BootstrapMutex {
    if ($script:OwnsBootstrapMutex) {
        try { $script:BootstrapMutex.ReleaseMutex() } catch { }
        $script:OwnsBootstrapMutex = $false
    }
    if ($script:BootstrapMutex) {
        $script:BootstrapMutex.Dispose()
        $script:BootstrapMutex = $null
    }
}

function Show-ErrorDialog([string]$Message) {
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            $Message,
            "Mabobot 启动失败",
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        ) | Out-Null
    }
    catch {
        Write-BootstrapLog "无法显示错误窗口：$($_.Exception.Message)"
    }
}

function Confirm-RuntimeInstall([string[]]$Components) {
    Add-Type -AssemblyName PresentationFramework
    [xml]$Xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        Width="476" Height="356" WindowStyle="None" AllowsTransparency="True"
        Background="Transparent" ResizeMode="NoResize" WindowStartupLocation="CenterScreen"
        ShowInTaskbar="True" Topmost="True">
    <Window.Resources>
        <Style TargetType="Button">
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Background="{TemplateBinding Background}" BorderBrush="{TemplateBinding BorderBrush}" BorderThickness="{TemplateBinding BorderThickness}" CornerRadius="8">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True"><Setter Property="Opacity" Value="0.88"/></Trigger>
                            <Trigger Property="IsPressed" Value="True"><Setter Property="Opacity" Value="0.72"/></Trigger>
                            <Trigger Property="IsEnabled" Value="False"><Setter Property="Opacity" Value="0.48"/></Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
    </Window.Resources>
    <Border CornerRadius="18" BorderBrush="#E6DFD8" BorderThickness="1" Background="#FFFEFA" Padding="28">
        <Border.Effect><DropShadowEffect BlurRadius="30" ShadowDepth="8" Opacity="0.18" Color="#3A2D25"/></Border.Effect>
        <Grid>
            <Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/><RowDefinition Height="*"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
            <Grid Name="DragArea">
                <Grid.ColumnDefinitions><ColumnDefinition Width="Auto"/><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions>
                <Image Width="40" Height="40" Source="$AppIconImage" Stretch="Uniform"/>
                <StackPanel Grid.Column="1" Margin="14,0,0,0" VerticalAlignment="Center">
                    <TextBlock Text="首次运行准备" Foreground="#141413" FontFamily="Microsoft YaHei UI" FontSize="17" FontWeight="SemiBold"/>
                    <TextBlock Text="只需确认一次，完成后会自动继续启动" Foreground="#77736C" FontFamily="Microsoft YaHei UI" FontSize="11" Margin="0,5,0,0"/>
                </StackPanel>
                <Button Name="CloseButton" Grid.Column="2" Width="28" Height="28" Content="×" FontSize="18" Foreground="#77736C" Background="Transparent" BorderThickness="0" Cursor="Hand" IsCancel="True"/>
            </Grid>
            <TextBlock Grid.Row="1" Text="Mabobot 检测到以下基础组件尚未就绪：" Foreground="#4E4B46" FontFamily="Microsoft YaHei UI" FontSize="12" Margin="0,24,0,10"/>
            <Border Grid.Row="2" CornerRadius="10" Background="#F7F2EB" Padding="16,13" Margin="0,0,0,18">
                <StackPanel>
                    <TextBlock Name="ComponentText" Foreground="#272522" FontFamily="Microsoft YaHei UI" FontSize="12" FontWeight="SemiBold" LineHeight="24"/>
                    <TextBlock Text="将从官方源下载并验证数字签名，安装到当前用户。通常不需要管理员权限。" TextWrapping="Wrap" Foreground="#77736C" FontFamily="Microsoft YaHei UI" FontSize="10.5" Margin="0,8,0,0" LineHeight="18"/>
                </StackPanel>
            </Border>
            <StackPanel Grid.Row="3" Orientation="Horizontal" HorizontalAlignment="Right">
                <Button Name="CancelButton" Width="92" Height="36" Content="取消" Foreground="#514E49" Background="#F2EEE8" BorderBrush="#E4DDD5" BorderThickness="1" FontFamily="Microsoft YaHei UI" FontSize="11" Cursor="Hand" IsCancel="True" Margin="0,0,10,0"/>
                <Button Name="InstallButton" Width="112" Height="36" Content="继续安装" Foreground="White" Background="#C97055" BorderBrush="#C97055" BorderThickness="1" FontFamily="Microsoft YaHei UI" FontSize="11" FontWeight="SemiBold" Cursor="Hand" IsDefault="True"/>
            </StackPanel>
        </Grid>
    </Border>
</Window>
"@
    $Reader = New-Object System.Xml.XmlNodeReader $Xaml
    $Window = [Windows.Markup.XamlReader]::Load($Reader)
    if (Test-Path -LiteralPath $AppIcon) {
        $Window.Icon = [System.Windows.Media.Imaging.BitmapFrame]::Create([System.Uri]$AppIcon)
    }
    $Window.FindName("ComponentText").Text = ($Components | ForEach-Object { "• $_" }) -join "`n"
    $InstallButton = $Window.FindName("InstallButton")
    $CancelButton = $Window.FindName("CancelButton")
    $CloseButton = $Window.FindName("CloseButton")
    $DragArea = $Window.FindName("DragArea")
    $InstallButton.Add_Click({ $Window.DialogResult = $true })
    $CancelButton.Add_Click({ $Window.DialogResult = $false })
    $CloseButton.Add_Click({ $Window.DialogResult = $false })
    $DragArea.Add_MouseLeftButtonDown({ $Window.DragMove() })
    return ($Window.ShowDialog() -eq $true)
}

function Test-EnvironmentReady {
    if (-not (Test-Path $VenvPython) -or -not (Test-Path $VenvPythonw)) {
        return $false
    }
    if ($null -eq (Get-MabobotPythonInfo -FilePath $VenvPython)) {
        return $false
    }
    if (-not (Test-Path $Requirements) -or -not (Test-Path $HashMarker)) {
        return $false
    }
    foreach ($RequiredPath in @(".env", "data", "logs", "tmp")) {
        if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $RequiredPath))) { return $false }
    }
    $CurrentHash = (Get-FileHash $Requirements -Algorithm SHA256).Hash
    $SavedHash = (Get-Content $HashMarker -Raw).Trim()
    if ($CurrentHash -ne $SavedHash) {
        return $false
    }
    try {
        & $VenvPython -c "import fastapi, flask, sqlalchemy, mabowx, webview, pystray, psutil, dotenv, win32api, win32con, win32event, win32gui, win32process, win32security, win32ts, win32ui" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function New-ProgressWindow {
    Add-Type -AssemblyName PresentationFramework
    Add-Type -AssemblyName System.Windows.Forms
    [xml]$Xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        Width="520" Height="326" WindowStyle="None" AllowsTransparency="True"
        Background="Transparent" ResizeMode="NoResize" WindowStartupLocation="CenterScreen"
        ShowInTaskbar="True" Topmost="True">
    <Border CornerRadius="16" BorderBrush="#E6DFD8" BorderThickness="1" Background="#FFFEFA" Padding="26">
        <Border.Effect><DropShadowEffect BlurRadius="28" ShadowDepth="8" Opacity="0.18" Color="#3A2D25"/></Border.Effect>
        <Grid>
            <Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/><RowDefinition Height="*"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
            <Grid Name="DragArea">
                <Grid.ColumnDefinitions><ColumnDefinition Width="Auto"/><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions>
                <Image Width="38" Height="38" Source="$AppIconImage" Stretch="Uniform"/>
                <StackPanel Grid.Column="1" Margin="13,0,0,0">
                    <TextBlock Text="正在准备 Mabobot" Foreground="#141413" FontFamily="Microsoft YaHei UI" FontSize="16" FontWeight="SemiBold"/>
                    <TextBlock Text="首次运行准备基础环境，其他组件可在向导中选装" Foreground="#6C6A64" FontFamily="Microsoft YaHei UI" FontSize="11" Margin="0,5,0,0"/>
                </StackPanel>
                <Button Name="CloseButton" Grid.Column="2" Width="28" Height="28" Content="×" FontSize="18" Foreground="#77736C" Background="Transparent" BorderThickness="0" Cursor="Hand"/>
            </Grid>
            <Grid Grid.Row="1" Margin="0,22,0,0">
                <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="44"/></Grid.ColumnDefinitions>
                <ProgressBar Name="ProgressBar" Height="6" Minimum="0" Maximum="100" Value="2" Foreground="#CC785C" Background="#F5F0E8" BorderThickness="0" VerticalAlignment="Center"/>
                <TextBlock Name="ProgressPercent" Grid.Column="1" Text="2%" Foreground="#77736C" FontFamily="Microsoft YaHei UI" FontSize="10" HorizontalAlignment="Right" VerticalAlignment="Center"/>
            </Grid>
            <TextBlock Name="StatusText" Grid.Row="2" Text="正在检查运行环境…" Foreground="#4D4943" FontFamily="Microsoft YaHei UI" FontSize="11.5" FontWeight="SemiBold" Margin="0,14,0,0" TextWrapping="Wrap"/>
            <Border Grid.Row="3" CornerRadius="8" Background="#F7F2EB" Padding="11,8" Margin="0,9,0,10">
                <TextBlock Name="DetailText" Text="正在启动安装进程…" Foreground="#77736C" FontFamily="Consolas, Microsoft YaHei UI" FontSize="10" TextWrapping="Wrap" TextTrimming="CharacterEllipsis"/>
            </Border>
            <Grid Grid.Row="4">
                <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions>
                <TextBlock Name="ActivityText" Text="已用时 00:00 · 正在启动" Foreground="#96928B" FontFamily="Microsoft YaHei UI" FontSize="10" VerticalAlignment="Center"/>
                <Button Name="OpenLogButton" Grid.Column="1" Width="82" Height="30" Content="查看日志" Foreground="#514E49" Background="#F2EEE8" BorderBrush="#E4DDD5" BorderThickness="1" FontFamily="Microsoft YaHei UI" FontSize="10" Cursor="Hand" Margin="0,0,8,0"/>
                <Button Name="CancelButton" Grid.Column="2" Width="82" Height="30" Content="取消安装" Foreground="#A24D3D" Background="#FFF6F2" BorderBrush="#E7C7BD" BorderThickness="1" FontFamily="Microsoft YaHei UI" FontSize="10" Cursor="Hand"/>
            </Grid>
        </Grid>
    </Border>
</Window>
"@
    $Reader = New-Object System.Xml.XmlNodeReader $Xaml
    $Window = [Windows.Markup.XamlReader]::Load($Reader)
    if (Test-Path -LiteralPath $AppIcon) {
        $Window.Icon = [System.Windows.Media.Imaging.BitmapFrame]::Create([System.Uri]$AppIcon)
    }
    return $Window
}

function Stop-MabobotProcessTree([int]$ProcessId) {
    if ($ProcessId -le 0 -or $ProcessId -eq $PID) {
        return $false
    }
    try {
        $TaskkillOutput = & taskkill.exe /PID $ProcessId /T /F 2>&1
        $TaskkillExitCode = $LASTEXITCODE
        foreach ($Line in @($TaskkillOutput)) {
            if ($null -ne $Line -and ([string]$Line).Trim()) {
                Write-BootstrapLog "taskkill[$ProcessId]: $Line"
            }
        }
        return ($TaskkillExitCode -eq 0)
    }
    catch {
        Write-BootstrapLog "结束进程树 PID=$ProcessId 失败：$($_.Exception.Message)"
        try {
            Stop-Process -Id $ProcessId -Force -ErrorAction Stop
            return $true
        }
        catch {
            Write-BootstrapLog "结束根进程 PID=$ProcessId 仍然失败：$($_.Exception.Message)"
            return $false
        }
    }
}

function Get-OtherBootstrapProcesses {
    try {
        $BootstrapPath = (Resolve-Path $PSCommandPath).Path
        return @(
            Get-CimInstance Win32_Process -ErrorAction Stop |
                Where-Object {
                    $ProcessName = [string]$_.Name
                    $CommandLine = [string]$_.CommandLine
                    $_.ProcessId -ne $PID -and
                    $ProcessName -in @("powershell.exe", "pwsh.exe") -and
                    $CommandLine.IndexOf(
                        "-File",
                        [System.StringComparison]::OrdinalIgnoreCase
                    ) -ge 0 -and
                    ([string]$_.CommandLine).IndexOf(
                        $BootstrapPath,
                        [System.StringComparison]::OrdinalIgnoreCase
                    ) -ge 0
                } |
                Sort-Object CreationDate
        )
    }
    catch {
        Write-BootstrapLog "无法枚举已有启动引导进程：$($_.Exception.Message)"
        return @()
    }
}

function Confirm-ReplaceExistingBootstrap([object[]]$Processes) {
    Add-Type -AssemblyName PresentationFramework
    $ProcessSummary = if ($Processes.Count -gt 0) {
        ($Processes | ForEach-Object {
            "• $($_.Name)（PID $($_.ProcessId)）"
        }) -join "`n"
    }
    else {
        "• 未能读取后台进程详情"
    }
    $Message = @"
检测到上一次 Mabobot 环境准备仍在后台运行：

$ProcessSummary

这通常是安装窗口被关闭、但隐藏的安装进程没有结束。是否结束旧进程树并立即重新开始？

已经完成的安装内容会保留。
"@
    $Choice = [System.Windows.MessageBox]::Show(
        $Message,
        "恢复 Mabobot 启动",
        [System.Windows.MessageBoxButton]::YesNo,
        [System.Windows.MessageBoxImage]::Warning
    )
    return ($Choice -eq [System.Windows.MessageBoxResult]::Yes)
}

function Invoke-Installer([switch]$InstallMissingRuntimes) {
    if (-not (Test-Path $InstallScript)) {
        throw "找不到环境安装脚本：$InstallScript"
    }

    $Window = New-ProgressWindow
    $StatusText = $Window.FindName("StatusText")
    $DetailText = $Window.FindName("DetailText")
    $ActivityText = $Window.FindName("ActivityText")
    $ProgressBar = $Window.FindName("ProgressBar")
    $ProgressPercent = $Window.FindName("ProgressPercent")
    $OpenLogButton = $Window.FindName("OpenLogButton")
    $CancelButton = $Window.FindName("CancelButton")
    $CloseButton = $Window.FindName("CloseButton")
    $DragArea = $Window.FindName("DragArea")
    $StatusFile = Join-Path $LogDir "launcher_bootstrap.status"
    Set-Content -LiteralPath $StatusFile -Value "正在启动环境检查…" -Encoding UTF8
    $script:CancelInstallerRequested = $false
    $script:AllowProgressWindowClose = $false
    $RequestInstallerCancel = {
        if ($script:CancelInstallerRequested) {
            return
        }
        $Choice = [System.Windows.MessageBox]::Show(
            $Window,
            "确定取消本次环境准备吗？`n`nMabobot 会结束本次启动器及其 Python、Playwright 下载子进程。已经完成的安装内容会保留。",
            "取消 Mabobot 环境准备",
            [System.Windows.MessageBoxButton]::YesNo,
            [System.Windows.MessageBoxImage]::Warning
        )
        if ($Choice -eq [System.Windows.MessageBoxResult]::Yes) {
            $script:CancelInstallerRequested = $true
            $StatusText.Text = "正在取消安装并清理后台进程…"
            $DetailText.Text = "正在结束 PowerShell、Python 和 Playwright 子进程，请稍候。"
            $CancelButton.Content = "正在取消…"
            $CancelButton.IsEnabled = $false
            $CloseButton.IsEnabled = $false
        }
    }
    $CancelButton.Add_Click({ & $RequestInstallerCancel })
    $CloseButton.Add_Click({ & $RequestInstallerCancel })
    $DragArea.Add_MouseLeftButtonDown({ $Window.DragMove() })
    $OpenLogButton.Add_Click({
        try {
            Start-Process -FilePath "notepad.exe" -ArgumentList @($BootstrapLog) | Out-Null
        }
        catch {
            $DetailText.Text = "无法打开日志：$($_.Exception.Message)"
        }
    })
    $Window.Add_Closing({
        param($Sender, $EventArgs)
        if (-not $script:AllowProgressWindowClose) {
            $EventArgs.Cancel = $true
            & $RequestInstallerCancel
        }
    })
    $Window.Show()
    $InstallerCancelled = $false
    try {
        $Info = New-Object System.Diagnostics.ProcessStartInfo
        $Info.FileName = "powershell.exe"
        $InstallerArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$InstallScript`" -StatusFile `"$StatusFile`""
        if ($InstallMissingRuntimes) {
            $InstallerArguments += " -InstallMissingRuntimes"
        }
        $Info.Arguments = $InstallerArguments
        $Info.WorkingDirectory = $ProjectRoot
        $Info.UseShellExecute = $false
        $Info.CreateNoWindow = $true
        $Info.RedirectStandardOutput = $true
        $Info.RedirectStandardError = $true

        $Process = New-Object System.Diagnostics.Process
        $Process.StartInfo = $Info
        if (-not $Process.Start()) {
            throw "无法启动环境安装进程"
        }
        Write-BootstrapLog "环境安装进程已启动，PID=$($Process.Id)"
        $OutputTask = $Process.StandardOutput.ReadLineAsync()
        $ErrorTask = $Process.StandardError.ReadLineAsync()
        $LastStatus = ""
        $LastActivity = Get-Date
        $StartedAt = Get-Date
        while (-not $Process.HasExited -or $null -ne $OutputTask -or $null -ne $ErrorTask) {
            if ($script:CancelInstallerRequested -and -not $InstallerCancelled) {
                $InstallerCancelled = $true
                Write-BootstrapLog "用户请求取消环境安装，正在结束 PID=$($Process.Id) 的进程树"
                $ProcessTreeStopped = Stop-MabobotProcessTree -ProcessId $Process.Id
                if (-not $ProcessTreeStopped -and -not $Process.HasExited) {
                    $InstallerCancelled = $false
                    $script:CancelInstallerRequested = $false
                    $StatusText.Text = "未能结束后台安装进程"
                    $DetailText.Text = "请点击下方查看日志按钮了解详情，或在任务管理器中结束 PID $($Process.Id) 的进程树。"
                    $CancelButton.Content = "重试取消"
                    $CancelButton.IsEnabled = $true
                    $CloseButton.IsEnabled = $true
                }
            }

            if (Test-Path $StatusFile) {
                $RawStatus = Get-Content -LiteralPath $StatusFile -Raw -ErrorAction SilentlyContinue
                $CurrentStatus = if ($null -eq $RawStatus) { "" } else { ([string]$RawStatus).Trim() }
                if ($CurrentStatus -and $CurrentStatus -ne $LastStatus) {
                    $LastStatus = $CurrentStatus
                    $LastActivity = Get-Date
                    try {
                        $Status = $CurrentStatus | ConvertFrom-Json -ErrorAction Stop
                        $StatusText.Text = [string]$Status.message
                        if ($null -ne $Status.percent) {
                            $Percent = [Math]::Max(0, [Math]::Min(100, [int]$Status.percent))
                            $ProgressBar.Value = $Percent
                            $ProgressPercent.Text = "$Percent%"
                        }
                        if ($Status.detail) {
                            $DetailText.Text = [string]$Status.detail
                        }
                    }
                    catch {
                        $StatusText.Text = $CurrentStatus
                    }
                }
            }

            while ($null -ne $OutputTask -and $OutputTask.IsCompleted) {
                try {
                    $Line = $OutputTask.GetAwaiter().GetResult()
                }
                catch {
                    if (-not $InstallerCancelled) {
                        throw
                    }
                    $Line = $null
                }
                if ($null -eq $Line) {
                    $OutputTask = $null
                }
                else {
                    Add-Content -LiteralPath $BootstrapLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [OUT] $Line" -Encoding UTF8
                    if ($Line.Trim()) {
                        $DetailText.Text = $Line.Trim()
                        $LastActivity = Get-Date
                    }
                    $OutputTask = $Process.StandardOutput.ReadLineAsync()
                }
            }
            while ($null -ne $ErrorTask -and $ErrorTask.IsCompleted) {
                try {
                    $Line = $ErrorTask.GetAwaiter().GetResult()
                }
                catch {
                    if (-not $InstallerCancelled) {
                        throw
                    }
                    $Line = $null
                }
                if ($null -eq $Line) {
                    $ErrorTask = $null
                }
                else {
                    Add-Content -LiteralPath $BootstrapLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [ERR] $Line" -Encoding UTF8
                    if ($Line.Trim()) {
                        $DetailText.Text = $Line.Trim()
                        $LastActivity = Get-Date
                    }
                    $ErrorTask = $Process.StandardError.ReadLineAsync()
                }
            }

            $Elapsed = (Get-Date) - $StartedAt
            $QuietSeconds = [int]((Get-Date) - $LastActivity).TotalSeconds
            $ElapsedText = "{0:00}:{1:00}:{2:00}" -f ([int]$Elapsed.TotalHours), $Elapsed.Minutes, $Elapsed.Seconds
            if ($QuietSeconds -ge 300 -and -not $InstallerCancelled) {
                $ActivityText.Text = "已用时 $ElapsedText · 已 $QuietSeconds 秒无新活动，可能卡住 · PID $($Process.Id)"
                $ActivityText.Foreground = "#B45D45"
            }
            elseif ($QuietSeconds -ge 60 -and -not $InstallerCancelled) {
                $ActivityText.Text = "已用时 $ElapsedText · $QuietSeconds 秒无新活动 · PID $($Process.Id)"
                $ActivityText.Foreground = "#8A744F"
            }
            else {
                $ActivityText.Text = "已用时 $ElapsedText · 最后活动 $QuietSeconds 秒前 · PID $($Process.Id)"
                $ActivityText.Foreground = "#96928B"
            }
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 100
        }
        $Process.WaitForExit()
        if ($InstallerCancelled) {
            Write-BootstrapLog "环境安装已由用户取消"
            return $false
        }
        if ($Process.ExitCode -ne 0) {
            throw "环境安装失败，退出代码 $($Process.ExitCode)"
        }
        return $true
    }
    finally {
        $script:AllowProgressWindowClose = $true
        $Window.Close()
        Remove-Item -LiteralPath $StatusFile -Force -ErrorAction SilentlyContinue
    }
}

$BootstrapMutex = New-Object System.Threading.Mutex($false, "Local\MabobotBootstrapV3")
$OwnsBootstrapMutex = $false
try {
    $OwnsBootstrapMutex = $BootstrapMutex.WaitOne(0, $false)
}
catch [System.Threading.AbandonedMutexException] {
    $OwnsBootstrapMutex = $true
}
if (-not $OwnsBootstrapMutex) {
    $ExistingBootstrapProcesses = @(Get-OtherBootstrapProcesses)
    Write-BootstrapLog "已有启动引导正在运行，检测到 $($ExistingBootstrapProcesses.Count) 个候选进程"
    if (-not (Confirm-ReplaceExistingBootstrap $ExistingBootstrapProcesses)) {
        Write-BootstrapLog "用户选择保留已有启动引导，本次请求退出"
        Close-BootstrapMutex
        exit 0
    }

    foreach ($ExistingProcess in $ExistingBootstrapProcesses) {
        Stop-MabobotProcessTree -ProcessId ([int]$ExistingProcess.ProcessId) | Out-Null
    }

    for ($Attempt = 0; $Attempt -lt 50 -and -not $OwnsBootstrapMutex; $Attempt++) {
        try {
            $OwnsBootstrapMutex = $BootstrapMutex.WaitOne(200, $false)
        }
        catch [System.Threading.AbandonedMutexException] {
            $OwnsBootstrapMutex = $true
        }
    }
    if (-not $OwnsBootstrapMutex) {
        Write-BootstrapLog "旧启动引导未能在预期时间内结束"
        Show-ErrorDialog "后台仍有 Mabobot 安装进程未能结束。`n`n请在任务管理器的详细信息页面结束对应的 powershell.exe 进程树，或重启电脑后再运行 START.bat。`n`n详细日志：$BootstrapLog"
        Close-BootstrapMutex
        exit 1
    }
    Write-BootstrapLog "旧启动引导进程已结束，本次启动接管成功"
}

try {
    Set-Location $ProjectRoot
    Write-BootstrapLog "启动引导开始，Startup=$Startup"
    if (-not (Test-Path -LiteralPath $RuntimeModule)) {
        throw "找不到基础环境引导模块：$RuntimeModule"
    }
    Import-Module $RuntimeModule -Force

    $EnvironmentReady = Test-EnvironmentReady
    $MissingRuntimes = @()
    $NeedsPythonInstall = $false
    $VenvPythonInfo = if (Test-Path -LiteralPath $VenvPython) {
        Get-MabobotPythonInfo -FilePath $VenvPython
    }
    else {
        $null
    }
    if (-not $EnvironmentReady -and $null -eq $VenvPythonInfo) {
        $NeedsPythonInstall = ($null -eq (Get-MabobotCompatiblePython))
        if ($NeedsPythonInstall) {
            $MissingRuntimes += "Python 3.12（64 位）"
        }
    }
    $NeedsWebView2Install = -not (Test-MabobotWebView2Runtime)
    if ($NeedsWebView2Install) {
        $MissingRuntimes += "Microsoft Edge WebView2 Runtime"
    }

    $InstallMissingRuntimes = $false
    if ($MissingRuntimes.Count -gt 0) {
        Write-BootstrapLog "检测到缺失基础组件：$($MissingRuntimes -join ', ')"
        if (-not (Confirm-RuntimeInstall $MissingRuntimes)) {
            Write-BootstrapLog "用户取消基础组件安装，本次启动结束"
            Close-BootstrapMutex
            exit 0
        }
        $InstallMissingRuntimes = $true
    }

    if (-not $EnvironmentReady -or $NeedsWebView2Install) {
        Write-BootstrapLog "环境未就绪，开始执行安装脚本"
        $InstallerCompleted = Invoke-Installer -InstallMissingRuntimes:$InstallMissingRuntimes
        if (-not $InstallerCompleted) {
            Write-BootstrapLog "用户取消环境准备，本次启动结束"
            Close-BootstrapMutex
            exit 0
        }
    }
    if (-not (Test-EnvironmentReady)) {
        throw "安装结束后环境检查仍未通过"
    }
    if (-not (Test-MabobotWebView2Runtime)) {
        throw "安装结束后仍未检测到 Microsoft Edge WebView2 Runtime"
    }

    $Arguments = @("-m", "mabobot_launcher")
    if ($Startup) {
        $Arguments += "--startup"
    }
    Start-Process -FilePath $VenvPythonw -ArgumentList $Arguments -WorkingDirectory $ProjectRoot
    Write-BootstrapLog "桌面启动器进程已创建"
    Close-BootstrapMutex
    exit 0
}
catch {
    $Message = $_.Exception.Message
    Write-BootstrapLog "启动失败：$Message"
    Show-ErrorDialog "Mabobot 无法启动。`n`n$Message`n`n详细日志：$BootstrapLog"
    Close-BootstrapMutex
    exit 1
}
