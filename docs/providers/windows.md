# Running RecreationBench on Windows

The Windows controller uploads the fixed runtime and local evaluation input to a
user-managed Windows host over SSH/SFTP, then runs
`recreation -> eval`.

## Prepare the Windows target

The target needs an active interactive desktop, OpenSSH Server, Python 3.12, Node.js,
the selected agent CLI, and Qwen CUA Driver 0.7.3. From a repository checkout on the target,
run:

```powershell
Set-Location <repo>\providers\windows
powershell -ExecutionPolicy Bypass -File .\provision\Provision-RecreationBenchWindows.ps1
powershell -ExecutionPolicy Bypass -File .\provision\Verify-RecreationBenchWindows.ps1
```

## Prepare local data

```text
<dataset-root>/windows/<task-id>/instance.json
<dataset-root>/windows/<task-id>/reference/
<dataset-root>/windows/<task-id>/tests/
```

The controller also accepts `<dataset-root>/<task-id>/...`. It does not fetch
missing tasks from the network.

## Configure and run

```bash
cd <repo>/providers/windows/template
cp .env.example .env.windows
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

Configure Windows SSH, model, judge, and `RB_UNIFIED_LOCAL_DIR`, then run:

```bash
python run_one.py \
  --task-id <windows-task-id> \
  --unified-dir /path/to/released-dataset \
  --stage recreation_eval
```

The provisioner installs pinned Python and agent dependencies. Normal runs reuse them;
set `RB_WINDOWS_SKIP_DEP_INSTALL=0` or `RB_WINDOWS_SKIP_AGENT_INSTALL=0` only to
refresh a prepared target from public package registries.

For a group:

```bash
python run_many.py \
  --tasks-file ../../../tasks/windows.jsonl \
  --unified-cache-dir /path/to/released-dataset \
  --concurrency 2 \
  --out-dir parallel_results
```

For stages other than `setup`, the runner verifies an active desktop and
`explorer.exe` before starting. Results under `results/<task-id>/` include
`metrics.json`, logs, trajectory, recreated artifacts, and tool-use screenshots.

Network isolation is enabled by default. After an interrupted run, restore the
host firewall policy before retrying; leftover RecreationBench outbound allow
rules cause the next run to stop before changing the policy. Completed runs
restore their saved policy automatically.
