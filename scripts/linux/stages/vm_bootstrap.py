"""VM bootstrap: install build/GUI packages and start the desktop environment.

Run once at pipeline start on the deployment platform sandbox VM.  Replaces the Docker image
(rb-gui-builder) and container entrypoint so that stages can run directly on
the VM without ``docker run``.
"""

from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import runtime_assets
from infrastructure.artifacts import artifact_environment_for_remote
from vm_utils import check_invocation, run_command

BOOTSTRAP_SCRIPT = runtime_assets.load_text("linux/runtime/bootstrap.sh")


def _wait(client, invoke_id: str, label: str, max_wait: int) -> dict | None:
    deadline = time.time() + max_wait
    interval = 5
    seen = 0
    while time.time() < deadline:
        result = check_invocation(client, invoke_id)
        output = (result or {}).get("output", "")
        if len(output) > seen:
            for line in output[seen:].rstrip("\n").split("\n"):
                print(f"  [{label}] {line}", flush=True)
            seen = len(output)
        if result and result.get("status") not in (None, "Running", "Pending"):
            elapsed = max_wait - int(deadline - time.time())
            status = result["status"]
            print(f"  {label}: {status} ({elapsed // 60}m)", flush=True)
            return result
        time.sleep(interval)
    print(f"  {label}: timeout after {max_wait}s", flush=True)
    return None


def run(client, instance_id: str, timeout: int = 900, sudo_password: str = "") -> bool:
    """Run the bootstrap script on the VM.  Returns True on success."""
    print("Running VM bootstrap (install packages + start GUI)...")
    script = BOOTSTRAP_SCRIPT
    if sudo_password:
        script = f"export RB_SUDO_PASSWORD={shlex.quote(sudo_password)}\n" + script
    cua_ref = os.environ.get("RB_CUA_DRIVER_REF", "")
    if cua_ref:
        script = f"export RB_CUA_DRIVER_REF={shlex.quote(cua_ref)}\n" + script
        print(f"  cua-driver setup-stage override: RB_CUA_DRIVER_REF={cua_ref}")
    cua_coord = os.environ.get("RB_CUA_COORDINATE_SPACE", "")
    if cua_coord:
        script = f"export RB_CUA_COORDINATE_SPACE={shlex.quote(cua_coord)}\n" + script
        print(f"  cua-driver coordinate-space: RB_CUA_COORDINATE_SPACE={cua_coord}")
    # Inject artifact-store settings so the bootstrap's prebuilt cua-driver cache is
    # available in the sandbox shell. It does not inherit controller env by default.
    remote_artifact_env = artifact_environment_for_remote(os.environ)
    cache_prefix = os.environ.get("RB_CUA_DRIVER_CACHE_PREFIX", "")
    if cache_prefix:
        remote_artifact_env["RB_CUA_DRIVER_CACHE_PREFIX"] = cache_prefix
    for name, value in remote_artifact_env.items():
        script = f"export {name}={shlex.quote(value)}\n" + script
    if remote_artifact_env:
        print(
            "  artifact cache settings injected for prebuilt cua-driver: "
            + ",".join(sorted(remote_artifact_env))
        )
    invoke_id = run_command(client, script, instance_id=instance_id, timeout=timeout)
    result = _wait(client, invoke_id, "Bootstrap", max_wait=timeout + 120)
    if not result or result.get("status") != "Success":
        print("ERROR: VM bootstrap failed")
        return False
    return True
