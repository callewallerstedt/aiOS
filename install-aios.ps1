$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $baseDir

python "$baseDir\install_aios.py"
