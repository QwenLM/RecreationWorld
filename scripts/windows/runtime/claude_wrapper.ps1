$ErrorActionPreference = 'Continue'
$dbg = '__DEBUG_LOG__'
$exitFile = '__AGENT_EXIT_PATH__'
Remove-Item $exitFile -Force -EA SilentlyContinue
"$(Get-Date -Format o) START wrapper as $(whoami)" | Out-File $dbg -Encoding utf8
$noBomUtf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $noBomUtf8
$OutputEncoding = $noBomUtf8
chcp 65001 | Out-Null
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
$env:PATH = "C:\Program Files\nodejs;C:\npm-global;C:\cargo\bin;" + $env:PATH
try { Set-Location '__WORKSPACE_DIR__' } catch { "CD FAILED: $_" | Out-File $dbg -Append -Encoding utf8 }
"$(Get-Date -Format o) CWD=$PWD" | Out-File $dbg -Append -Encoding utf8
$env:ANTHROPIC_BASE_URL = '__BASE_URL__'
$env:ANTHROPIC_MODEL = '__CLIENT_MODEL__'
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = '__CLIENT_MODEL__'
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = '__CLIENT_MODEL__'
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = '__CLIENT_MODEL__'
$env:API_FORCE_IDLE_TIMEOUT = '0'
$env:CLAUDE_CODE_MAX_RETRIES = '__CLAUDE_CODE_MAX_RETRIES__'
$env:MAX_STRUCTURED_OUTPUT_RETRIES = '__MAX_STRUCTURED_OUTPUT_RETRIES__'
$env:API_TIMEOUT_MS = '__API_TIMEOUT_MS__'
__MCP_TOOL_TIMEOUT_EXPORT__
__MCP_ENV__
__EXTRA_ENV__"$(Get-Date -Format o) PROMPT file=__PROMPT_PATH__" | Out-File $dbg -Append -Encoding utf8
$preflightMode = '__CUA_PREFLIGHT_MODE__'
$sessionCheckOk = $true
try {
    $wrapperSessionId = (Get-Process -Id $PID -EA Stop).SessionId
    $referenceSessionId = (Get-Process -Id __REFERENCE_PID__ -EA Stop).SessionId
    if ([string]::IsNullOrWhiteSpace($env:RB_CUA_DAEMON_PID)) { throw 'RB_CUA_DAEMON_PID is missing' }
    $daemonProcess = Get-Process -Id ([int]$env:RB_CUA_DAEMON_PID) -EA Stop
    $daemonSessionId = $daemonProcess.SessionId
    "$(Get-Date -Format o) CUA sessions wrapper=$wrapperSessionId reference=$referenceSessionId daemon=$daemonSessionId daemon_pid=$($daemonProcess.Id)" | Out-File $dbg -Append -Encoding utf8
    if ($daemonSessionId -ne $referenceSessionId -or $wrapperSessionId -ne $referenceSessionId) {
        throw "CUA session mismatch: wrapper=$wrapperSessionId reference=$referenceSessionId daemon=$daemonSessionId"
    }
} catch {
    "$(Get-Date -Format o) CUA session check failed: $_" | Out-File $dbg -Append -Encoding utf8
    $sessionCheckOk = $false
}
# Probe from the exact rbagent token used below. A root/SYSTEM CLI check does not prove that
# this principal can open the daemon pipe or read the reference app's UIA tree and pixels.
if ($sessionCheckOk) {
    & '__VM_PYTHON__' '__MCP_PROBE_PATH__' --driver '__CUA_DRIVER_PATH__' --socket '__CUA_DAEMON_PIPE__' --claude-code-compat --kind desktop --pid __REFERENCE_PID__ --include-descendants --timeout 30 --attempts 3 >> $dbg 2>&1
    $mcpProbeEc = $LASTEXITCODE
} else {
    $mcpProbeEc = 86
}
if ($mcpProbeEc -ne 0) {
    if ($preflightMode -eq 'warn') {
        "$(Get-Date -Format o) WARNING desktop-control MCP reference preflight exit=$mcpProbeEc; continuing because RB_CUA_PREFLIGHT_MODE=warn" | Out-File $dbg -Append -Encoding utf8
    } else {
        "$(Get-Date -Format o) FATAL desktop-control MCP reference preflight exit=$mcpProbeEc" | Out-File $dbg -Append -Encoding utf8
        86 | Set-Content $exitFile -Encoding ascii
        exit 86
    }
} else {
    "$(Get-Date -Format o) desktop-control MCP reference preflight passed" | Out-File $dbg -Append -Encoding utf8
}
"$(Get-Date -Format o) RUNNING claude" | Out-File $dbg -Append -Encoding utf8
& '__VM_PYTHON__' '__RUNNER_PATH__' run --spec '__SPEC_PATH__'
$ec = $LASTEXITCODE
"$(Get-Date -Format o) DONE exit=$ec traj=$((Get-Item '__TRAJECTORY_PATH__' -EA SilentlyContinue).Length) stderr=$((Get-Item '__STDERR_PATH__' -EA SilentlyContinue).Length)" | Out-File $dbg -Append -Encoding utf8
$ec | Set-Content $exitFile -Encoding ascii
exit $ec
