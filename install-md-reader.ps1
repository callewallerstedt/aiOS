$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Reader = Join-Path $Root "md_reader.pyw"
$Icon = Join-Path $Root "assets\aios-logo.ico"
$ProgId = "AIOS.MarkdownReader"

if (-not (Test-Path -LiteralPath $Reader)) {
    throw "Reader script not found: $Reader"
}

$Pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
if (-not $Pythonw) {
    $Python = (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    if ($Python) {
        $Candidate = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
        if (Test-Path -LiteralPath $Candidate) {
            $Pythonw = $Candidate
        }
    }
}
if (-not $Pythonw) {
    throw "pythonw.exe was not found on PATH."
}

function Set-DefaultValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $RelativePath = $Path -replace "^HKCU:\\", ""
    if ($RelativePath -eq $Path) {
        throw "Set-DefaultValue only supports HKCU paths: $Path"
    }

    $Key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($RelativePath)
    try {
        $Key.SetValue("", $Value, [Microsoft.Win32.RegistryValueKind]::String)
    }
    finally {
        $Key.Close()
    }
}

function Add-OpenWithProgId {
    param([Parameter(Mandatory = $true)][string]$Extension)

    $OpenWithPath = "HKCU:\Software\Classes\$Extension\OpenWithProgids"
    if (-not (Test-Path -LiteralPath $OpenWithPath)) {
        New-Item -Path $OpenWithPath -Force | Out-Null
    }

    $Key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("Software\Classes\$Extension\OpenWithProgids")
    try {
        $Key.SetValue($ProgId, [byte[]]@(), [Microsoft.Win32.RegistryValueKind]::None)
    }
    finally {
        $Key.Close()
    }
}

$Command = "`"$Pythonw`" `"$Reader`" `"%1`""

Set-DefaultValue "HKCU:\Software\Classes\$ProgId" "Markdown Document"
Set-DefaultValue "HKCU:\Software\Classes\$ProgId\shell\open\command" $Command

if (Test-Path -LiteralPath $Icon) {
    Set-DefaultValue "HKCU:\Software\Classes\$ProgId\DefaultIcon" "`"$Icon`""
}

foreach ($Extension in ".md", ".markdown") {
    Set-DefaultValue "HKCU:\Software\Classes\$Extension" $ProgId
    Add-OpenWithProgId $Extension

    $UserChoicePath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$Extension\UserChoice"
    if (Test-Path -LiteralPath $UserChoicePath) {
        try {
            Remove-Item -LiteralPath $UserChoicePath -Recurse -Force -ErrorAction Stop
        }
        catch {
            Write-Warning "Windows kept a protected UserChoice for $Extension. The reader is registered, but Windows may require selecting it once in Default apps."
        }
    }
}

$Signature = @"
using System;
using System.Runtime.InteropServices;

public static class ShellNotify {
    [DllImport("shell32.dll")]
    public static extern void SHChangeNotify(int wEventId, uint uFlags, IntPtr dwItem1, IntPtr dwItem2);
}
"@
Add-Type -TypeDefinition $Signature -ErrorAction SilentlyContinue
[ShellNotify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)

Write-Host "Registered aiOS Markdown Reader (WebView2)."
Write-Host "Command: $Command"
