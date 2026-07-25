$ErrorActionPreference = "Stop"

$taskName = "aiOS Watchdog"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed scheduled task: $taskName"
}

$startupDir = [Environment]::GetFolderPath("Startup")
foreach ($name in @("aiOS Watchdog.lnk", "Computer Helper Autocorrect.lnk")) {
    $shortcutPath = Join-Path $startupDir $name
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
        Write-Host "Removed startup shortcut: $shortcutPath"
    }
}

$matches = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -like "*aios_watchdog.py*") -or
    ($_.Name -like "AutoHotkey*.exe" -and $_.CommandLine -like "*autocorrect.ahk*")
}
foreach ($process in $matches) {
    Stop-Process -Id $process.ProcessId -Force
    Write-Host "Stopped startup process: $($process.ProcessId)"
}

foreach ($file in @(".aios-health.json", ".aios-helper-heartbeat", ".aios-ahk-heartbeat", ".aios-phone-relay-heartbeat")) {
    $path = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) $file
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
}
