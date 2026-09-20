
$target = '__AGENT_USER__'
function Get-AgentProcesses {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
        $proc = $_
        try {
            $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction Stop
            if ($owner.ReturnValue -eq 0 -and $owner.User -ieq $target) { $proc }
        } catch {}
    })
}
function Describe-Process($proc) {
    $cmd = ''
    try { $cmd = [string]$proc.CommandLine } catch {}
    if ($cmd.Length -gt 160) { $cmd = $cmd.Substring(0, 160) + '...' }
    $tail = if ($cmd) { ' ' + $cmd } else { '' }
    return ('{0}(pid {1}){2}' -f $proc.Name, $proc.ProcessId, $tail)
}
# Backoff, and a second mechanism: Stop-Process asks the process to die and cannot reach a tree,
# while taskkill /T walks children -- a GUI app flushing on exit routinely needs more than the
# 3 x 1s this used to allow, and 3s is not evidence that a process will never leave.
$seen = @{}
$delays = @(1, 2, 4, 8)
foreach ($delay in $delays) {
    $owned = @(Get-AgentProcesses)
    if ($owned.Count -eq 0) { exit 0 }
    foreach ($proc in $owned) {
        $seen[[string]$proc.ProcessId] = $true
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        & taskkill.exe /F /T /PID $proc.ProcessId 2>$null | Out-Null
    }
    Start-Sleep -Seconds $delay
}
$remaining = @(Get-AgentProcesses)
if ($remaining.Count -gt 0) {
    # Naming them matters more than counting them: the sandbox is destroyed right after this, so
    # a bare pid cannot be looked up afterwards -- which is exactly the position one run left us
    # in. Also separate "this process will not die" from "new ones keep appearing", because only
    # the first is something a longer retry could ever fix.
    $fresh = @($remaining | Where-Object { -not $seen.ContainsKey([string]$_.ProcessId) })
    $desc = (($remaining | ForEach-Object { Describe-Process $_ }) -join '; ')
    $kind = if ($fresh.Count -gt 0) { 'respawning' } else { 'unkillable' }
    Write-Error ('agent processes survived shutdown [' + $kind + ']: ' + $desc)
    exit 1
}
