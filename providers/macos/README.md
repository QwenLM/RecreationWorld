# RecreationBench macOS provider

This provider connects to a prepared macOS environment over SSH, deploys the
runtime and task input, runs the selected stages, and collects the result
artifacts. See the [macOS provider guide](../../docs/providers/macos.md) for the
complete dependency matrix, SSH configuration, and run procedure.

## Version summary

| Component | Supported version |
| --- | --- |
| macOS | 14.7.5 (Sonoma) |
| Xcode | 15.4 |
| Python | 3.9+ |
| Node.js | 22.x+ |
| npm | 10.x+ |
| Codex CLI | 0.145.0 |
| Claude Code | 2.1.177 |
| Qwen CUA Driver | 0.7.3 |

The provider guide also lists the Python packages and optional build
toolchains required by the supported application set.

Environment setup and validation are implemented by
[`provision/setup_macos.sh`](provision/setup_macos.sh) and
[`provision/check_macos_ready.sh`](provision/check_macos_ready.sh). The runner
invokes them over SSH by default. Target-side Python dependencies are declared
in [`provision/requirements.macos.txt`](provision/requirements.macos.txt);
controller dependencies remain in
[`template/requirements.txt`](template/requirements.txt).

## Run

Configure `providers/macos/template/.env.macos`, then run:

```bash
cd providers/macos/template
uv venv
source .venv/bin/activate
UV_INDEX_URL=https://pypi.org/simple uv pip install -r requirements.txt
python run_one.py \
  --task-id pawelsalawa-letos \
  --unified-cache-dir /path/to/released-dataset
```

The controller uses SSH/SFTP for deployment and result collection. Pass
`--skip-provision` when the target environment has already been prepared.
