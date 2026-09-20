# Running RecreationBench on macOS

The macOS provider connects to a prepared macOS environment over SSH. It uploads
the runtime and local evaluation input, runs recreation and evaluation, and then
collects the result artifacts. The environment's provisioning method is outside
the scope of this repository.

## Access requirements

- An administrator account reachable over SSH, using either a password or an SSH key.
  Automated preparation requires `MACOS_PASSWORD`; key-only access requires an
  already-prepared environment, non-interactive `sudo`, and `--skip-provision`.
- An active GUI session with Accessibility and Screen Recording permissions.
- Local released evaluation data.
- Reachable agent-model and visual-judge APIs.
- A non-administrator isolation account. The default name is `devagent`.

## Base environment

| Item | Requirement |
| --- | --- |
| OS | macOS 14.7.5 (Sonoma) |
| Xcode | 15.4, with Command Line Tools (provides `xcodebuild`, `codesign`, `swift`, `clang`, `git`) |
| CPU | Apple Silicon or Intel |

## Preflight-gating commands

`check_macos_ready.sh` requires all of these commands on `PATH`:

```
bash   git   python3   node   npm   xcodebuild   codesign   sqlite3   timeout
```

It also verifies every package declared by the target-side Python requirements.

## Core dependency versions

| Dependency | Version | Notes |
| --- | --- | --- |
| bash | 3.2+ (system) | |
| git | 2.39+ | from Xcode CLT |
| node | 22.x+ | |
| npm | 10.x+ | ships with node |
| python3 | 3.9+ (system `/usr/bin/python3`) | pyobjc + pytest run on 3.9 |
| sqlite3 | 3.x (system) | |
| coreutils | provides `timeout` on `PATH` | several stages call `timeout N …` (macOS has no native `timeout`) |

## Python packages (system `python3`)

The installable target-side packages and their supported ranges are maintained
in
[`requirements.macos.txt`](../../providers/macos/provision/requirements.macos.txt).

| Package | Version | Required for |
| --- | --- | --- |
| pytest | >=8.3,<9 | AX evaluation runs each `test_ax_*.py` via `python3 -m pytest` |
| pytest-timeout | >=2.3,<3 | per-test timeouts |
| pytest-json-report | >=1.5,<2 | result capture |
| pyobjc-framework-ApplicationServices | >=10.3,<11 | reads the a11y tree |
| pyobjc-framework-Cocoa | >=10.3,<11 | Cocoa integration |
| pyobjc-framework-Quartz | >=10.3,<11 | screen capture |
| pyyaml | >=6.0,<7 | YAML parsing |

Controller-side dependencies are maintained in
[`requirements.txt`](../../providers/macos/template/requirements.txt):

| Package | Version |
| --- | --- |
| paramiko | >=3.5,<4 |
| python-dotenv | >=1.0,<2 |

`jq` and the Python `mcp` package are not required.

## Set up and validate the macOS environment

Use the provided scripts to prepare and validate the environment:

- [`setup_macos.sh`](../../providers/macos/provision/setup_macos.sh) applies the
  required system settings, prepares the isolation account, installs
  `requirements.macos.txt`, and installs the selected agent CLI.
- [`check_macos_ready.sh`](../../providers/macos/provision/check_macos_ready.sh)
  performs the read-only readiness check.

`run_one.py` uploads and runs both scripts over SSH before starting a task. Pass
`--skip-provision` to skip `setup_macos.sh` when the environment is already
prepared; the readiness check still runs.

To run the scripts directly from a repository checkout on macOS, first create a
mode-`600` shell environment file containing `MACOS_PASSWORD` and, when needed,
`MACOS_DEVAGENT_USER`, `MACOS_DEVAGENT_PASSWORD`, `RB_AGENT_CLI`,
`RB_CODEX_VERSION`, and `RB_CLAUDE_VERSION`, then run:

```bash
bash providers/macos/provision/setup_macos.sh /path/to/runtime_env
bash providers/macos/provision/check_macos_ready.sh
```

## Runtime component versions

| Component | Version |
| --- | --- |
| Agent CLI — `@openai/codex` (default) | 0.145.0 |
| Agent CLI — `@anthropic-ai/claude-code` (`RB_AGENT_CLI=claude`) | 2.1.177 |
| CUA desktop-control driver — `qwen-cua-driver` (`QwenCuaDriver.app`) | 0.7.3 |

## Build toolchain versions

The required toolchain depends on the applications included in a run.

| Tool | Version |
| --- | --- |
| cmake | 3.x |
| pkg-config, automake, autoconf, libtool, wget | any recent |
| meson | 1.x |
| ninja | 1.x |
| swift | 5.x (from Xcode) |
| swiftlint | 0.x |
| swiftgen | 6.x |
| cocoapods (`pod`) | 1.x |
| carthage | 0.x |
| yarn | 1.x |
| pnpm | 9.x |
| rustc / cargo | 1.x (+ `tauri-cli`) |
| Qt | 6.x |
| go | 1.x |
| .NET SDK | 8.x or 9.x |
| OpenJDK | 17.x |
| gradle | 8.x |

## Evaluation data

The provider accepts an extracted dataset directory or a per-task archive. The
directory form is:

```text
<dataset-root>/macos/<task-id>/instance.json
<dataset-root>/macos/<task-id>/reference/
<dataset-root>/macos/<task-id>/tests/
```

Configure either `RB_UNIFIED_CACHE_DIR` or `RB_UNIFIED_ARCHIVE`.

## Configure SSH and model access

From the repository root:

```bash
cd providers/macos/template
cp .env.example .env.macos
uv venv
source .venv/bin/activate
UV_INDEX_URL=https://pypi.org/simple uv pip install -r requirements.txt
```

Set the following values in `.env.macos`:

- `MACOS_HOST`, `MACOS_SSH_PORT`, and `MACOS_USERNAME`.
- Either `MACOS_PASSWORD` or `MACOS_SSH_KEY`.
- `MACOS_DEVAGENT_PASSWORD` when the isolation account does not already exist.
- `RB_MODEL`, `RB_MODEL_BASE_URL`, and `RB_MODEL_API_KEY`.
- `RB_VLM_MODEL`, `RB_VLM_BASE_URL`, and `RB_VLM_KEY` when evaluation is enabled.
- One of the evaluation-data paths described above.

## Deploy and run

Run one task from `providers/macos/template`:

```bash
python run_one.py \
  --task-id pawelsalawa-letos \
  --unified-cache-dir /path/to/released-dataset
```

The controller performs the following sequence:

1. Validates and packages the selected local evaluation input.
2. Packages the runtime from the current repository checkout.
3. Transfers both archives over SSH/SFTP.
4. Prepares dependencies and runs the readiness checks.
5. Installs the selected agent CLI and CUA driver at their pinned versions.
6. Runs the selected `recreation`, `eval`, or `recreation_eval` stage.
7. Downloads metrics, logs, trajectories, screenshots, and recreated artifacts.

Use `--skip-provision` when the target environment already satisfies the dependency
matrix. Set `RB_STAGE` or pass `--stage` to select the lifecycle stage.

Results are written below `RB_RESULT_DIR/<task-id>/`; the default root is
`results/macos`.
