$ErrorActionPreference = 'Stop'
$taskName = 'RB_Pipeline'
# Every stage reuses this one task name, and MultipleInstances defaults to IgnoreNew, so starting
# it while the previous stage's instance is still alive is REFUSED -- LastTaskResult 0x800710E0,
# "the operator or administrator has refused the request" -- and the new stage simply never runs.
# That is a real overlap, not a stale flag: after worker.py exits, runner.ps1 still has to kill the
# CUA daemon, write pipeline_exit.txt and, when the stage failed, pull two 30-event Get-WinEvent
# queries.  One eval launch landed 18s after recreation's python exited and was refused.
#
# Waiting rather than forcing parallelism (a per-stage task name, or -MultipleInstances Parallel):
# the next runner's first act is to delete pipeline.log, pipeline_exit.txt and
# pipeline_exit_marker.txt, so overlapping the two would race the epilogue that is still writing
# them and hand the new stage the old stage's exit code -- the exact staleness that deletion is
# there to prevent.
$deadline = (Get-Date).AddSeconds(__TASK_DRAIN_TIMEOUT__)
$drained = 'not_running'
while ((Get-Date) -lt $deadline) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $existing -or $existing.State -ne 'Running') { break }
    $drained = 'waited'
    Start-Sleep -Seconds 3
}
# Bail out rather than proceed. Unregister-ScheduledTask removes the DEFINITION and leaves the
# running powershell.exe detached, and the fresh registration has no running instance of its own,
# so IgnoreNew no longer applies and the new runner starts anyway -- two runners, the old one still
# in its epilogue writing pipeline_exit.txt while the new one deletes it. That is the exact
# staleness the wait above exists to prevent, so reaching the deadline has to be a failure, not a
# warning: a stage that never starts is recoverable, a stage that reports the previous stage's exit
# code is not.
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existing -and $existing.State -eq 'Running') {
    $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
    $res = if ($null -ne $info) { $info.LastTaskResult } else { 'unknown' }
    Write-Host "DRAIN_TIMEOUT lastTaskResult=$res state=$($existing.State) drained=timeout"
    exit 1
}
# The wait above already proved no instance is running, so this cannot interrupt an epilogue -- the
# reason that wait exists instead of a kill. It is here to force the task object itself out of
# whatever transitional state the service left it in when the previous instance completed seconds
# ago; tmp's orchestrator does the same thing before every launch and does not see the inert-launch
# failure this file is otherwise full of defences against.
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
# A previous stage's marker is not launch evidence.  The old task has drained, so it is now safe to
# remove; runner.ps1 recreates it as its first instruction, before dependency installation.
$startFile = '__START_PATH__'
Remove-Item -LiteralPath $startFile -Force -ErrorAction SilentlyContinue
try {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File __RUNNER_PATH__' `
        -ErrorAction Stop
    # Two independent activation paths on purpose: this timer and the explicit Start below.  Whichever
    # loses is refused by IgnoreNew, harmlessly, and only LastTaskResult records the loss -- which is
    # why the confirmation loop no longer treats that code as terminal.
    #
    # This trigger was removed once, on the grounds that the refusal it causes had made a single read
    # of LastTaskResult report REFUSED on 11 of 50 healthy jobs.  Gating the verdict on State fixed
    # that; removing the trigger was the part that was not needed, and it cost measurably: eval-launch
    # failures increased measurably in the regression without the trigger.
    # A client-side retry does not substitute for it (poll_stage's re-activation rounds tried): by the
    # time the orchestrator can retry, our Start has already put an instance in the pending state, so
    # IgnoreNew declines every later activation with 0x800710E0.
    # The scheduler's own timer is the only activation that arrives inside the window where there is
    # still nothing to collide with.
    $trigger = New-ScheduledTaskTrigger -Once `
        -At (Get-Date).AddSeconds(__TASK_TRIGGER_DELAY__) -ErrorAction Stop
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 48) -ErrorAction Stop
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Highest -ErrorAction Stop
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
} catch {
    Write-Host "LAUNCH_FAILED error=$($_.Exception.Message)"
    exit 1
}

# Start-ScheduledTask is fire-and-forget.  Confirm either a live task or the fresh marker written
# by a runner that started and completed quickly; anything else must not be labelled Started.
$confirmDeadline = (Get-Date).AddSeconds(__TASK_START_CONFIRM_TIMEOUT__)
$state = 'unknown'
$res = 'unknown'
while ((Get-Date) -lt $confirmDeadline) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
    $state = if ($null -ne $task) { [string]$task.State } else { 'missing' }
    $res = if ($null -ne $info) { $info.LastTaskResult } else { 'unknown' }
    $detail = "lastTaskResult=$res state=$state drained=$drained"
    if ($state -eq 'Running' -or (Test-Path -LiteralPath $startFile)) {
        Write-Host "Started $detail"
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
# The refusal code is annotated, never terminal.  With two activation paths one of them is always
# refused, so reading 0x800710E0 says nothing about whether the stage started -- State and the marker
# do.  Failing on it early is what reported REFUSED on 11 of 50 healthy jobs; the only thing lost by
# waiting out the window instead is __TASK_START_CONFIRM_TIMEOUT__s, and both verdicts abort the stage
# identically.
$note = if ($res -eq __TASK_REFUSED_CODE__) { 'refused_an_instance_was_pending' } else { 'no_start_evidence' }
Write-Host "LAUNCH_FAILED lastTaskResult=$res state=$state drained=$drained error=$note"
exit 1
