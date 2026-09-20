$ErrorActionPreference = 'Continue'
$dbg = '__DEBUG_LOG__'
$exitFile = '__AGENT_EXIT_PATH__'
Remove-Item $exitFile -Force -EA SilentlyContinue
"$(Get-Date -Format o) START codex wrapper as $(whoami)" | Out-File $dbg -Encoding utf8
$noBomUtf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $noBomUtf8
$OutputEncoding = $noBomUtf8
chcp 65001 | Out-Null
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
$env:PATH = "C:\Program Files\nodejs;C:\npm-global;C:\cargo\bin;" + $env:PATH
__MCP_ENV__
__EXTRA_ENV__
try { Set-Location '__WORKSPACE_DIR__' } catch { "CD FAILED: $_" | Out-File $dbg -Append -Encoding utf8 }
"$(Get-Date -Format o) CWD=$PWD" | Out-File $dbg -Append -Encoding utf8

# --- Codex config.toml ---
$codexDir = Join-Path $env:USERPROFILE '.codex'
New-Item -ItemType Directory -Path $codexDir -Force | Out-Null
$env:CODEX_HOME = $codexDir
$configBytes = [Convert]::FromBase64String('__CODEX_CONFIG_B64__')
$configPath = Join-Path $codexDir 'config.toml'
[System.IO.File]::WriteAllBytes($configPath, $configBytes)
if (-not (Select-String -Path $configPath -SimpleMatch '[mcp_servers.desktop-control]' -Quiet)) {
    "FATAL: Codex config does not contain desktop-control MCP" | Out-File $dbg -Append -Encoding utf8
    2 | Set-Content $exitFile -Encoding ascii
    exit 2
}
"$(Get-Date -Format o) config.toml written" | Out-File $dbg -Append -Encoding utf8

# --- Git repo init (codex requires it) ---
if (-not (Test-Path .git)) {
    git init -q 2>$null
    git config user.email "rb@bench"
    git config user.name "rb"
    git add -A 2>$null
    git commit -q -m "init" --allow-empty 2>$null
    "$(Get-Date -Format o) git init done" | Out-File $dbg -Append -Encoding utf8
}

"$(Get-Date -Format o) PROMPT file=__PROMPT_PATH__" | Out-File $dbg -Append -Encoding utf8
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

# Validate the exact stdio bridge, daemon pipe and rbagent token Codex will use. A process-only
# check cannot distinguish a responsive daemon from the stale named-pipe state seen in canary.
if ($sessionCheckOk) {
    $mcpProbePath = Join-Path $env:TEMP 'rb_windows_mcp_preflight.py'
    Copy-Item '__MCP_PROBE_PATH__' $mcpProbePath -Force
    & '__VM_PYTHON__' $mcpProbePath --driver '__CUA_DRIVER_PATH__' --socket '__CUA_DAEMON_PIPE__' --kind desktop --pid __REFERENCE_PID__ --include-descendants --timeout 30 --attempts 3 >> $dbg 2>&1
    $mcpProbeEc = $LASTEXITCODE
    Remove-Item $mcpProbePath -Force -EA SilentlyContinue
} else {
    $mcpProbeEc = 86
}
if ($mcpProbeEc -ne 0) {
    if ($preflightMode -eq 'warn') {
        "$(Get-Date -Format o) WARNING desktop-control MCP reference preflight exit=$mcpProbeEc; continuing because RB_CUA_PREFLIGHT_MODE=warn" | Out-File $dbg -Append -Encoding utf8
    } else {
        "$(Get-Date -Format o) FATAL desktop-control MCP preflight exit=$mcpProbeEc" | Out-File $dbg -Append -Encoding utf8
        86 | Set-Content $exitFile -Encoding ascii
        exit 86
    }
} else {
    "$(Get-Date -Format o) desktop-control MCP reference preflight passed" | Out-File $dbg -Append -Encoding utf8
}

& '__VM_PYTHON__' '__RUNNER_PATH__' run --spec '__SPEC_PATH__'
$lastEc = $LASTEXITCODE

"$(Get-Date -Format o) DONE exit=$lastEc traj=$((Get-Item '__TRAJECTORY_PATH__' -EA SilentlyContinue).Length) stderr=$((Get-Item '__STDERR_PATH__' -EA SilentlyContinue).Length)" | Out-File $dbg -Append -Encoding utf8
$lastEc | Set-Content $exitFile -Encoding ascii
exit $lastEc
