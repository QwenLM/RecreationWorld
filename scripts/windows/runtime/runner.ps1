$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force -Path '__TASK_DIR__' | Out-Null
# First thing this script touches, deliberately.  poll_stage used to confirm a stage had begun by
# looking for a process named python*, but everything below runs as pip.exe first -- a pip install
# of 18 packages -- so a slow VM was indistinguishable from a stage that never launched, and
# poll_stage gave up after 45s.  Writing the marker before any of that work makes "did this stage
# start" a recorded fact rather than an inference from process names.
"stage=__STAGE__`nstarted=$(Get-Date -Format o)`npid=$PID" | Out-File -FilePath '__START_FILE__' -Encoding utf8
# Cleared up front, not after the pip install below.  These three are per stage, and while they
# still held the previous stage's content, a stage that failed to start reported that stage's
# result as its own: five eval stages that never ran were recorded with native_exit_code=0, which
# was recreation's success code.  An empty pipeline.log now unambiguously means this stage produced
# nothing (the previous stage's copy already reached artifact store before this one launched).
Remove-Item '__PIPELINE_LOG__','__EXIT_FILE__','__EXIT_MARKER_FILE__' -Force -EA SilentlyContinue
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
chcp 65001 | Out-Null
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'__EXTRA_ENV__

# --- Defender exclusions (best-effort, silent on non-admin or no Defender) ---
Add-MpPreference -ExclusionPath 'C:\rb_pipeline' -EA SilentlyContinue
Add-MpPreference -ExclusionProcess 'python.exe' -EA SilentlyContinue
Add-MpPreference -ExclusionProcess 'claude.exe' -EA SilentlyContinue
Add-MpPreference -ExclusionProcess 'codex.exe' -EA SilentlyContinue

cd "__REMOTE_CODE_DIR__\scripts\windows"
pip install pywinauto psutil Pillow pyperclip pyyaml openai pyinstaller wxPython numpy PyOpenGL PyOpenGL_accelerate pyautogui pynput six requests pytest pytest-timeout -q 2>$null
$exitCode = 1
$cuaDaemon = $null
try {__CUA_DAEMON_START__
    python -u worker.py --task-id '__VM_TASK_ID__' --stage '__STAGE__'__MODEL_ARGS____EVAL_TARGET_ARG____VLM_KEY_ARG____VLM_MODEL_ARG____VLM_BASE_URL_ARG____TIMEOUT_ARG____DIR_SUFFIX_ARG____EFFORT_ARG____COMPACT_WINDOW_ARG____COMPACT_PCT_ARG____CUA_SPACE_ARG____CUA_SCALE_ARG____MCP_PAYLOAD_FILTER_ARG____EXTRA_BODY_ARG____AGENT_CLI_ARG____API_TIMEOUT_ARG____MCP_TIMEOUT_ARG____MAX_OUTPUT_TOKENS_ARG____TIME_HOOK_ARG__ >> '__PIPELINE_LOG__' 2>&1
    $exitCode = $LASTEXITCODE
} catch {
    "$(Get-Date -Format o) FATAL runner exception: $_" | Out-File -FilePath '__PIPELINE_LOG__' -Append -Encoding utf8
    $exitCode = 87
} finally {__CUA_DAEMON_STOP__
}

# --- Record pipeline exit code and collect crash diagnostics ---
"exit_code=$exitCode`ntime=$(Get-Date -Format o)`npid=$PID" | Out-File -FilePath '__EXIT_FILE__' -Encoding utf8
if ($exitCode -ne 0 -and $null -ne $exitCode) {
    try {
        Get-WinEvent -FilterHashtable @{LogName='Application'; Level=1,2,3; StartTime=(Get-Date).AddMinutes(-10)} -MaxEvents 30 -EA SilentlyContinue |
            Format-List TimeCreated,Id,LevelDisplayName,ProviderName,Message |
            Out-File -FilePath '__EVENT_FILE__' -Encoding utf8
    } catch {}
    try {
        Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2,3; StartTime=(Get-Date).AddMinutes(-10)} -MaxEvents 30 -EA SilentlyContinue |
            Format-List TimeCreated,Id,LevelDisplayName,ProviderName,Message |
            Out-File -FilePath '__EVENT_FILE__' -Append -Encoding utf8
    } catch {}
}
exit $exitCode
