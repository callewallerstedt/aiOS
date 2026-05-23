$ErrorActionPreference = "Stop"

$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "Computer Helper Autocorrect.lnk"

if (Test-Path $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath
    Write-Host "Removed startup shortcut: $shortcutPath"
} else {
    Write-Host "Startup shortcut was not installed."
}

$matches = Get-CimInstance Win32_Process |
    Where-Object {
        ($_.Name -eq "pythonw.exe" -and $_.CommandLine -like "*autocorrect.py*") -or
        ($_.Name -like "AutoHotkey*.exe" -and $_.CommandLine -like "*autocorrect.ahk*")
    }

foreach ($process in $matches) {
    Stop-Process -Id $process.ProcessId -Force
    Write-Host "Stopped autocorrect process: $($process.ProcessId)"
}
