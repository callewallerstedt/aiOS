$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$frontend = "https://phonesite-six.vercel.app"
$phoneSite = Join-Path $root "phone_site"
$backendConfig = Join-Path $phoneSite "backend.json"
$backendStatus = "http://127.0.0.1:5000/api/phone/status"
$python = "python"
$tools = Join-Path $root "tools"
$cloudflared = Join-Path $tools "cloudflared.exe"
$backendOut = Join-Path $root "phone-backend-out.log"
$backendErr = Join-Path $root "phone-backend-err.log"
$tunnelOut = Join-Path $root "cloudflared-out.log"
$tunnelErr = Join-Path $root "cloudflared-err.log"
$urlFile = Join-Path $root "phone-bridge-url.txt"

function Test-Backend {
    try {
        $response = Invoke-WebRequest -Uri $backendStatus -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Find-TunnelUrl {
    $text = ""
    if (Test-Path $tunnelOut) {
        $text += Get-Content $tunnelOut -Raw -ErrorAction SilentlyContinue
    }
    if (Test-Path $tunnelErr) {
        $text += "`n" + (Get-Content $tunnelErr -Raw -ErrorAction SilentlyContinue)
    }
    $match = [regex]::Match($text, "https://[a-zA-Z0-9-]+\.trycloudflare\.com")
    if ($match.Success) {
        return $match.Value
    }
    return $null
}

function Test-PublicBackend($url) {
    if (!$url) {
        return $false
    }
    try {
        $response = Invoke-WebRequest -Uri "$url/api/phone/status" -UseBasicParsing -TimeoutSec 8
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (!(Test-Backend)) {
    Start-Process `
        -FilePath $python `
        -ArgumentList @("run.py") `
        -WorkingDirectory (Join-Path $root "agent_clicker") `
        -WindowStyle Hidden `
        -RedirectStandardOutput $backendOut `
        -RedirectStandardError $backendErr

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Backend) {
            break
        }
    }
}

if (!(Test-Backend)) {
    throw "Phone backend did not start. Check $backendErr"
}

New-Item -ItemType Directory -Force -Path $tools | Out-Null
if (!(Test-Path $cloudflared)) {
    Invoke-WebRequest `
        -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
        -OutFile $cloudflared `
        -UseBasicParsing
}

$backendUrl = Find-TunnelUrl
if ($backendUrl -and !(Test-PublicBackend $backendUrl)) {
    $backendUrl = $null
}
if (!$backendUrl) {
    Remove-Item -LiteralPath $tunnelOut, $tunnelErr -ErrorAction SilentlyContinue
    Start-Process `
        -FilePath $cloudflared `
        -ArgumentList @("tunnel", "--url", "http://127.0.0.1:5000", "--no-autoupdate") `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $tunnelOut `
        -RedirectStandardError $tunnelErr

    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        $backendUrl = Find-TunnelUrl
        if ($backendUrl) {
            break
        }
    }
}

if (!$backendUrl) {
    throw "Cloudflare tunnel did not start. Check $tunnelErr"
}

$json = "{`n  `"backend`": `"$backendUrl`"`n}"
Set-Content -Path $backendConfig -Value $json -Encoding UTF8

Push-Location $phoneSite
try {
    npm run build | Out-Host
    npx vercel deploy --prod --yes | Out-Host
} finally {
    Pop-Location
}

$phoneUrl = $frontend
Set-Content -Path $urlFile -Value $phoneUrl -Encoding UTF8

Write-Host ""
Write-Host "aiOS phone URL:"
Write-Host $phoneUrl
Write-Host "Backend:"
Write-Host $backendUrl
Write-Host ""
Write-Host "Saved to: $urlFile"
