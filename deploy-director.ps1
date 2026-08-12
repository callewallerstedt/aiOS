#Requires -Version 5.1
<#
.SYNOPSIS
  Push the Director package from this repo to the Linux box and (re)install it.

.DESCRIPTION
  Copies director/ to ~/aios-director on calle-linux and runs its installer.
  Idempotent — this is the normal way to deploy a change.

.EXAMPLE
  .\deploy-director.ps1
  .\deploy-director.ps1 -TargetHost 100.69.218.63   # over Tailscale instead of LAN
#>
param(
    [string]$TargetHost = "192.168.0.17",
    [string]$User = "calle",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\id_ed25519_robot_printer",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$remote = "$User@$TargetHost"
$ssh = @("-i", $KeyPath, "-o", "StrictHostKeyChecking=accept-new")

if (-not (Test-Path $KeyPath)) { throw "No SSH key at $KeyPath" }
if (-not (Test-Path (Join-Path $here "director"))) { throw "No director/ folder next to this script" }

Write-Host "Deploying Director to $remote ..." -ForegroundColor Cyan

& ssh @ssh $remote "mkdir -p ~/aios-director/director"

# One archive, one copy. tar writes the file itself — piping binary through
# PowerShell corrupts it.
$tmp = Join-Path $env:TEMP "aios-director-deploy.tar.gz"
& tar "-czf" $tmp "--exclude=__pycache__" "--exclude=*.pyc" "-C" $here "director"
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

& scp @ssh $tmp "${remote}:/tmp/aios-director-deploy.tar.gz"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }
Remove-Item $tmp -Force

& ssh @ssh $remote "rm -rf ~/aios-director/director && tar -xzf /tmp/aios-director-deploy.tar.gz -C ~/aios-director && rm -f /tmp/aios-director-deploy.tar.gz && chmod +x ~/aios-director/director/deploy/install.sh"
if ($LASTEXITCODE -ne 0) { throw "unpack failed" }

if ($SkipInstall) {
    Write-Host "Files copied. Skipping install." -ForegroundColor Yellow
    exit 0
}

& ssh @ssh $remote "bash ~/aios-director/director/deploy/install.sh"
if ($LASTEXITCODE -ne 0) { throw "install.sh failed" }

Write-Host "`nDone." -ForegroundColor Green
