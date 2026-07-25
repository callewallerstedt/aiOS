$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (!(Test-Path $python)) { $python = "python" }
$backendStatus = "http://127.0.0.1:5000/api/phone/status"
$backendOut = Join-Path $root "phone-backend-out.log"
$backendErr = Join-Path $root "phone-backend-err.log"
$relayOut = Join-Path $root "phone-relay-out.log"
$relayErr = Join-Path $root "phone-relay-err.log"

function Test-Backend {
    try {
        $response = Invoke-WebRequest -Uri $backendStatus -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch { return $false }
}

if (!(Test-Backend)) {
    Start-Process -FilePath $python -ArgumentList @("run.py") `
        -WorkingDirectory (Join-Path $root "agent_clicker") -WindowStyle Hidden `
        -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Backend) { break }
    }
}

if (!(Test-Backend)) { throw "The local OPERATOR bridge did not start. Check $backendErr" }

$config = Get-Content (Join-Path $root "helper_config.json") -Raw | ConvertFrom-Json
if (!$config.phone_relay.enabled -or !$config.phone_relay.machine_token) {
    throw "This PC is not paired yet. Open aiOS Settings, then Mobile remote."
}

$existing = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*phone_relay.py*run*" }
if (!$existing) {
    Start-Process -FilePath $python -ArgumentList @((Join-Path $root "phone_relay.py"), "run") `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput $relayOut -RedirectStandardError $relayErr
}

Write-Host "aiOS Remote is connected."
Write-Host $config.phone_relay.url
