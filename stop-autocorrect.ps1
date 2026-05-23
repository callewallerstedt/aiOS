$ErrorActionPreference = "Stop"

$matches = Get-CimInstance Win32_Process |
    Where-Object {
        ($_.Name -eq "pythonw.exe" -and $_.CommandLine -like "*autocorrect.py*") -or
        ($_.Name -like "AutoHotkey*.exe" -and $_.CommandLine -like "*autocorrect.ahk*")
    }

if (-not $matches) {
    Write-Host "Autocorrect is not running."
    exit 0
}

foreach ($process in $matches) {
    Stop-Process -Id $process.ProcessId -Force
    Write-Host "Stopped autocorrect process: $($process.ProcessId)"
}
