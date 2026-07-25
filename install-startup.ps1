$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$watchdog = Join-Path $baseDir "aios_watchdog.py"
$pythonCandidates = @(
    (Join-Path $baseDir ".venv\Scripts\pythonw.exe"),
    "C:\Python313\pythonw.exe",
    "C:\Python312\pythonw.exe",
    "C:\Python311\pythonw.exe"
)
$pythonw = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonw) {
    $command = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($command) { $pythonw = $command.Source }
}
if (-not $pythonw) {
    $command = Get-Command python.exe -ErrorAction Stop
    $pythonw = $command.Source
}
if (!(Test-Path -LiteralPath $watchdog)) {
    throw "aiOS watchdog was not found: $watchdog"
}

$taskName = "aiOS Watchdog"
$userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$watchdog`"" -WorkingDirectory $baseDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description "Keeps aiOS and its remote bridge healthy" `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "aiOS watchdog installed as a Windows logon task."
    Write-Host "It is running now and will restart automatically if it exits."
} catch {
    Write-Warning "Scheduled Task registration failed; installing a Startup shortcut instead."
    $startupDir = [Environment]::GetFolderPath("Startup")
    $shortcutPath = Join-Path $startupDir "aiOS Watchdog.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = "`"$watchdog`""
    $shortcut.WorkingDirectory = $baseDir
    $shortcut.WindowStyle = 7
    $shortcut.Description = "Keep aiOS running"
    $shortcut.Save()
    Start-Process -FilePath $pythonw -ArgumentList "`"$watchdog`"" -WorkingDirectory $baseDir -WindowStyle Hidden
    Write-Host "Startup fallback installed: $shortcutPath"
}

# Remove the legacy AutoHotkey-only shortcut if an older aiOS version installed it.
$legacyShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Computer Helper Autocorrect.lnk"
if (Test-Path -LiteralPath $legacyShortcut) {
    Remove-Item -LiteralPath $legacyShortcut -Force
}
