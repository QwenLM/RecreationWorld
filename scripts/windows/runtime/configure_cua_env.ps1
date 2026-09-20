[System.Environment]::SetEnvironmentVariable("CUA_DRIVER_RS_COORDINATE_SPACE", "__COORDINATE_SPACE__", "Machine")
[System.Environment]::SetEnvironmentVariable("CUA_DRIVER_RS_COORDINATE_SCALE", "__COORDINATE_SCALE__", "Machine")
[System.Environment]::SetEnvironmentVariable("MCP_MODEL_PAYLOAD_FILTER", "__MCP_MODEL_PAYLOAD_FILTER__", "Machine")
$v1 = [System.Environment]::GetEnvironmentVariable("CUA_DRIVER_RS_COORDINATE_SPACE", "Machine")
$v2 = [System.Environment]::GetEnvironmentVariable("CUA_DRIVER_RS_COORDINATE_SCALE", "Machine")
$v3 = [System.Environment]::GetEnvironmentVariable("MCP_MODEL_PAYLOAD_FILTER", "Machine")
[System.Environment]::SetEnvironmentVariable("RB_CUA_DRIVER_BINARY", "__DRIVER_BINARY__", "Machine")
Write-Host "  CUA_DRIVER_RS_COORDINATE_SPACE=$v1"
Write-Host "  CUA_DRIVER_RS_COORDINATE_SCALE=$v2"
Write-Host "  MCP_MODEL_PAYLOAD_FILTER=$v3"
if ($v1 -ne "__COORDINATE_SPACE__" -or $v2 -ne "__COORDINATE_SCALE__" -or $v3 -ne "__MCP_MODEL_PAYLOAD_FILTER__") { throw "CUA driver env verification failed" }
schtasks.exe /End /TN "cua-driver-serve" 2>$null | Out-Null
schtasks.exe /End /TN "qwen-cua-driver-serve" 2>$null | Out-Null
Start-Sleep -Milliseconds 500
taskkill.exe /F /IM "cua-driver.exe" /T 2>$null | Out-Null
taskkill.exe /F /IM "qwen-cua-driver.exe" /T 2>$null | Out-Null
Get-Process -Name "cua-driver","qwen-cua-driver" -EA SilentlyContinue | Stop-Process -Force -EA SilentlyContinue
Start-Sleep -Milliseconds 500
if (-not (Get-Command "__DRIVER_BINARY__" -EA SilentlyContinue)) { throw "Pinned CUA driver executable not found: __DRIVER_BINARY__" }
& "__DRIVER_BINARY__" autostart kick 2>&1 | Out-Null
Start-Sleep -Seconds 2
$serve = Get-Process -Name "cua-driver","qwen-cua-driver" -EA SilentlyContinue
if ($serve) { Write-Host "  CUA Driver serve running (PID=$($serve.Id -join ','))" }
else { throw "CUA Driver serve process not found after kick" }
