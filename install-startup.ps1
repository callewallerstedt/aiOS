$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidatePaths = @(
    "C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe",
    "$env:LOCALAPPDATA\Programs\AutoHotkey\v2\AutoHotkey64.exe",
    "$env:LOCALAPPDATA\Programs\AutoHotkey\v2\AutoHotkey32.exe"
)
$autohotkey = $candidatePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $autohotkey) {
    $autohotkey = (Get-Command AutoHotkey.exe -ErrorAction Stop).Source
}

$scriptPath = Join-Path $baseDir "autocorrect.ahk"
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "Computer Helper Autocorrect.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $autohotkey
$shortcut.Arguments = "`"$scriptPath`""
$shortcut.WorkingDirectory = $baseDir
$shortcut.WindowStyle = 7
$shortcut.Description = "Computer Helper Autocorrect"
$shortcut.Save()

Start-Process -FilePath $autohotkey -ArgumentList "`"$scriptPath`"" -WorkingDirectory $baseDir -WindowStyle Hidden

Write-Host "Autocorrect installed and started."
Write-Host "Startup shortcut: $shortcutPath"
