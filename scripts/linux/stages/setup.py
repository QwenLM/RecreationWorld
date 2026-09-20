"""Establish and ATTEST the recreation agent's file boundary on Linux.

A thin adapter over ``core.permission_probe``: this file names linux's paths and nothing else. The
probe protocol (stand-ins, the positive control, existence-aware probes, ancestor traversal, the
verdict) and the transport (ship to the VM, run there, judge here) both live in core, because four
copies of the protocol produced four versions of the same three bugs.

The same client contract works in the deployment platform pod and on an explicitly supplied SSH host.
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from core import permission_probe  # noqa: E402

from stages.config import VM_CODE_DIR, vm_task_dir  # noqa: E402


def paths(task_id: str, model: str) -> tuple[list[str], list[str]]:
    """protected = what the agent must not read; writable = the only tree it authors.

    ``<base>/reference`` and ``<base>/tests`` are the frozen release inputs;
    ``VM_CODE_DIR`` is the controller implementation copied into the guest.
    """
    base = vm_task_dir(task_id)
    return (
        [f"{base}/reference", f"{base}/tests", VM_CODE_DIR],
        [f"{base}/recreation_{model.replace('.', '-')}"],
    )


def run(
    client,
    instance_id: str,
    task_id: str,
    model: str,
    *,
    user: str = "",
    timeout: int = 600,
) -> tuple[bool, str]:
    protected, writable = paths(task_id, model)
    return permission_probe.run_remote(
        client,
        "linux",
        task_id,
        protected,
        writable,
        user=user,
        # Every other linux stage that needs root prepends this the same way; the SSH user is in the
        # sudo GROUP but sudo still wants a password, so `sudo -n` alone fails.
        sudo_password=os.environ.get("RB_SSH_PASSWORD", ""),
        timeout=timeout,
    )


def summarize(transcript: str) -> dict:
    """Machine-readable bits for metrics, without re-running anything."""
    return {
        "attested": "SETUP OK" in transcript,
        "denied": transcript.count("denied  :"),
        "skipped": transcript.count("skipped :"),
        "leaks": transcript.count("DENY-EXPECTED but SUCCEEDED"),
    }
