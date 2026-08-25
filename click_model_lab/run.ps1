$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:CLICK_LAB_PORT = if ($env:CLICK_LAB_PORT) { $env:CLICK_LAB_PORT } else { "8765" }
Write-Host "Starting Click Model Lab on http://127.0.0.1:$env:CLICK_LAB_PORT"
python server.py
