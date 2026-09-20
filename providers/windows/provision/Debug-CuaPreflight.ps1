[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TaskDir
)

$ErrorActionPreference = "Continue"
$exe = Join-Path $TaskDir "install\bin\BMPEditor.exe"
$driver = "qwen-cua-driver"
Write-Host "TaskDir=$TaskDir"
Write-Host "Exe=$exe"
Write-Host "Driver=$driver"

if (-not (Test-Path $exe)) {
    throw "exe not found: $exe"
}
if (-not (Get-Command $driver -ErrorAction SilentlyContinue)) {
    throw "driver not found on PATH: $driver"
}

$p = Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe) -PassThru
Write-Host "Started PID=$($p.Id)"
Start-Sleep -Seconds 5

function Invoke-CuaCall([string]$Tool, [hashtable]$Payload) {
    $json = $Payload | ConvertTo-Json -Compress
    $json | & $driver call $Tool
}

Write-Host "--- list_windows pid ---"
$windowsJson = Invoke-CuaCall list_windows @{ pid = $p.Id; on_screen_only = $true }
$windowsJson
Write-Host "--- list_windows all ---"
$allWindowsJson = Invoke-CuaCall list_windows @{ on_screen_only = $true }
$allWindowsJson
Write-Host "--- get_window_state candidates ---"
try {
    $obj = $allWindowsJson | ConvertFrom-Json
    $wins = @()
    if ($obj.structuredContent.windows) { $wins = @($obj.structuredContent.windows) }
    elseif ($obj.windows) { $wins = @($obj.windows) }
    foreach ($w in ($wins | Where-Object { $_.app_name -eq "BMPEditor.exe" -or $_.title -eq "BMP Editor" })) {
        $wid = if ($w.window_id) { $w.window_id } else { 0 }
        Write-Host "window_id=$wid pid=$($w.pid) title=$($w.title)"
        Invoke-CuaCall get_window_state @{ pid = $w.pid; window_id = $wid; capture_mode = "ax" }
    }
} catch {
    Write-Host "parse/probe failed: $_"
}

Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
