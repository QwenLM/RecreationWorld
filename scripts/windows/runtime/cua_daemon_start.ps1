
# --- Start the trusted CUA daemon in this interactive RB_Pipeline session ---
schtasks.exe /End /TN "cua-driver-serve" 2>$null | Out-Null
schtasks.exe /End /TN "qwen-cua-driver-serve" 2>$null | Out-Null
Get-Process -Name "cua-driver","cua-driver-uia","qwen-cua-driver","qwen-cua-driver-uia" -EA SilentlyContinue |
    Stop-Process -Force -EA SilentlyContinue
Start-Sleep -Milliseconds 500

$driverName = $env:RB_CUA_DRIVER_BINARY
if ([string]::IsNullOrWhiteSpace($driverName)) {
    if (Get-Command 'qwen-cua-driver' -EA SilentlyContinue) { $driverName = 'qwen-cua-driver' }
    else { $driverName = 'cua-driver' }
}
$driverCommand = Get-Command $driverName -CommandType Application -EA Stop | Select-Object -First 1
$runnerSessionId = (Get-Process -Id $PID).SessionId
$cuaDaemon = Start-Process -FilePath $driverCommand.Source `
    -ArgumentList @('serve', '--socket', '__DAEMON_PIPE__') `
    -RedirectStandardOutput '__DAEMON_STDOUT__' `
    -RedirectStandardError '__DAEMON_STDERR__' `
    -WindowStyle Hidden -PassThru -EA Stop
Start-Sleep -Seconds 2
$cuaDaemon.Refresh()
if ($cuaDaemon.HasExited) { throw "CUA daemon exited during startup (exit=$($cuaDaemon.ExitCode))" }
$daemonSessionId = $cuaDaemon.SessionId
if ($daemonSessionId -ne $runnerSessionId) {
    throw "CUA daemon session mismatch: runner=$runnerSessionId daemon=$daemonSessionId"
}
$env:RB_CUA_DAEMON_PID = [string]$cuaDaemon.Id
$env:RB_CUA_DAEMON_SESSION_ID = [string]$daemonSessionId
"$(Get-Date -Format o) CUA daemon pid=$($cuaDaemon.Id) session=$daemonSessionId runner_pid=$PID pipe=__DAEMON_PIPE__" |
    Out-File -FilePath '__PIPELINE_LOG__' -Encoding utf8
