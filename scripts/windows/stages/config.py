"""Shared configuration for all Windows pipeline stages."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

VM_PIPELINE_BASE = r"C:\rb_pipeline"

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / ".claude" / "agents"

BUILD_PROMPT = """\
You are a build engineer on a Windows machine with pre-installed build tools (Visual Studio Build Tools 2022 with MSVC v143/MFC/ATL/.NET desktop workload, .NET SDK 8/10, .NET Framework 4.7.2/4.8.1 targeting packs, .NET MAUI, Node.js 20, npm, pnpm, yarn, Java 17, Go, Rust, Flutter, Lazarus, 7-Zip, Chocolatey).

The application source is already checked out in your current working directory at a pinned commit, with submodules materialised. build.ps1 MUST NOT acquire source: no git clone, fetch, checkout, reset, clean or submodule commands against it, and never delete it. Fetching a third-party dependency into a different directory is fine.

Your task:
1. Read README.md, .sln/.csproj/CMakeLists.txt/package.json, or any build docs to understand how to compile. Check .github/workflows/ only if unclear.
2. Install any missing dependencies with `choco install -y` or direct download.
3. Build the project (prefer Release mode, install to __INSTALL_DIR__).
   IMPORTANT — dependency installation paths. The build runs as Administrator
   but the recreation agent later runs as an UNPRIVILEGED user (rbagent), so
   every dependency MUST land in a machine-wide GLOBAL location. NEVER install
   into user-specific directories ($env:USERPROFILE, $env:APPDATA,
   $env:LOCALAPPDATA, ~\\.m2, ~\\.gradle, ~\\.nuget, …) — the recreation user
   cannot read them and the recreation will fail for lack of the dependency.
   - Chocolatey `choco install -y` installs machine-wide (preferred).
   - Python (pip): `pip install <pkg>` installs to system site-packages.
     Do NOT use `--user` — that installs into the current user's home.
   - Language toolchains must be redirected OFF their default per-user home
     into a global path: Maven `-Dmaven.repo.local=C:\\m2_repo`, Gradle
     `$env:GRADLE_USER_HOME='C:\\gradle_cache'`, Rust `$env:CARGO_HOME='C:\\cargo'`,
     NuGet `$env:NUGET_PACKAGES='C:\\nuget_packages'`.
   - Do NOT install any dependencies under __INSTALL_DIR__ or the working
     directory, and do NOT create a venv/virtualenv.
   - The application's own compiled binary/executable goes to __INSTALL_DIR__.
   If the build fails, read the error, install missing deps, and retry.
   Do not give up after the first failure.
4. Write a standalone build script to __OUTPUT_DIR__\\build.ps1 that reproduces the entire build from scratch (including dependency install commands). The script must be self-contained.
5. Write a launch script to __OUTPUT_DIR__\\launch.ps1 that starts the application. The launch script MUST:
   - Use Start-Process -PassThru to get the process object
   - Output ONLY the numeric PID as the first line via Write-Output
   - NOT use Write-Host (it does not go to stdout)
   - NOT print any text before the PID
   - Set working directory, environment variables, or app-specific flags as needed
   - Electron/Chromium apps: pass --no-sandbox and a UNIQUE --user-data-dir
     (e.g. Join-Path $env:TEMP (New-Guid)) to avoid single-instance lock
     collisions when the test harness relaunches the app multiple times.
   - launch.ps1 must work standalone without build.ps1 having been run first.
   The script MUST follow this pattern:
     $proc = Start-Process -FilePath "C:\\path\\to\\app.exe" -PassThru
     Write-Output $proc.Id
   You may add flags, env vars, or working directory BEFORE the Start-Process line, but the Start-Process and Write-Output lines are mandatory and must appear exactly as shown (with your actual exe path).
6. Verify from clean state: delete __INSTALL_DIR__, then run build.ps1 to rebuild from scratch:
     Remove-Item -Recurse -Force '__INSTALL_DIR__' -ErrorAction SilentlyContinue
     powershell -ExecutionPolicy Bypass -File __OUTPUT_DIR__\\build.ps1
   After the rebuild, verify launch.ps1 actually works:
     $out = & powershell -ExecutionPolicy Bypass -File __OUTPUT_DIR__\\launch.ps1
     $appPid = [int]$out[0]
   Confirm $appPid is a number, wait 10 seconds, then check the process is still alive with Get-Process -Id $appPid. If it crashed or the PID is missing, fix build.ps1 or launch.ps1 and retry. Kill the process after verification:
     Stop-Process -Id $appPid -Force
   Any dependency install commands, source patches, or build steps needed must be included in build.ps1; any runtime env setup must be in launch.ps1.
   IMPORTANT: Do NOT use $pid as a variable name — it is a read-only PowerShell automatic variable. Use $appPid as shown above.
7. Write a short JSON summary to __OUTPUT_DIR__\\build_result.json:
   {"success": true/false, "executable": "<absolute path to .exe>",
    "build_system": "<msbuild/dotnet/cmake/npm/...>",
    "installed_deps": ["pkg1", ...], "notes": "..."}
   "executable" must point to the GUI desktop application .exe.
   Even if the build fails, always create build_result.json (with success=false).

If cmake/msbuild/dotnet build fails, read the error, install missing deps, and retry. Use `choco search` to find packages. If the project requires a newer toolchain not pre-installed, download and install it with silent/unattended flags.

IMPORTANT: These are GUI desktop applications. The verification in step 6 must actually launch the GUI (not just --help/--version). The pipeline reads the PID from launch.ps1 stdout to manage the process — if Write-Output $proc.Id is missing or replaced with Write-Host, the entire pipeline fails. You MUST delete __INSTALL_DIR__, rebuild via build.ps1, verify that launch.ps1 outputs a numeric PID and the process stays alive for 10 seconds. If it crashes, fix build.ps1 or launch.ps1 and retry.

Do NOT sanity-check the built binary with `<app> --version` or `<app> --help`:
GUI toolkits (Electron, WPF, WinUI, Qt) launch the full GUI on any argument and
never exit, which HANGS the build and burns the entire time budget. Wrap EVERY
direct invocation of the app in a timeout; if it hangs, kill it (taskkill /F /T)
and move on. As soon as the binary exists and stays alive for 10 seconds under
launch.ps1, write build_result.json and STOP — do not keep rebuilding.\
"""


# RECREATION_PROMPT moved to core/recreation_prompt.py — ONE template for all five platforms.
# windows' fork had accumulated a block of isolation rules that existed nowhere else, and the
# other platforms lacked rules windows had; the shared template makes each difference an explicit
# placeholder instead of a fork. stages/recreation.py renders it at the point of use so the
# selected agent CLI can provide its tool catalogue; workspace paths come from the fixed contract.


def vm_task_dir(task_id: str) -> str:
    return f"{VM_PIPELINE_BASE}\\{task_id}"


_CUA_DRIVER_CANDIDATES = ["qwen-cua-driver", "cua-driver"]

_CUA_DRIVER_FALLBACK_DIRS = [
    r"C:\Program Files\Cua\cua-driver\bin",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Cua\cua-driver\bin"),
]


def resolve_cua_driver(path: str | None = None) -> str:
    """Find the CUA driver binary on PATH, trying both trycua and qwen names."""
    preferred = os.environ.get("RB_CUA_DRIVER_BINARY", "").strip()
    # A release-selected binary is a constraint, not a preference. Falling back
    # to the other fork would make the version check and the actual MCP process
    # refer to different drivers.
    candidates = [preferred] if preferred else list(_CUA_DRIVER_CANDIDATES)
    for name in candidates:
        found = shutil.which(name, path=path)
        if found:
            return found
    for d in _CUA_DRIVER_FALLBACK_DIRS:
        for name in candidates:
            candidate = os.path.join(d, f"{name}.exe")
            if os.path.isfile(candidate):
                return candidate
    if preferred:
        raise FileNotFoundError(
            f"release-selected CUA driver executable not found: {preferred}"
        )
    return "cua-driver"


# ---------------------------------------------------------------------------
# Codex CLI (OpenAI) — for GPT-series model evaluation
# ---------------------------------------------------------------------------


def _codex_template(platform: str) -> str:
    """The platform's config.toml as a {model}/{base_url} format string."""
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from core import agent_config as _ac
    from core.model_endpoint import from_environment as _endpoint_from_environment

    if platform != "windows":
        raise ValueError(f"unsupported Windows config platform: {platform!r}")
    endpoint = _endpoint_from_environment(
        os.environ,
        provider="recreationbench",
        name="RecreationBench model endpoint",
        base_url="{base_url}",
        wire_api="responses",
    )
    return _ac.codex_config_toml("{model}", endpoint)


# Built by the shared endpoint and config renderers, which reproduce
# this template byte-for-byte. `.format(model=…, base_url=…)` still works, so no caller
# changes. The provider name and wire_api stay per-platform on purpose: Linux keeps its
# historical implicit wire selection while Windows sets wire_api="responses", so folding
# them into one value would be a silent protocol change.
CODEX_CONFIG_TOML_TEMPLATE = _codex_template("windows")
