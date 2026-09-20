"""linux's VM command surface — a thin compatibility layer over core.vmclient.

This module used to drive a cloud RunCommand API. That path is gone: production never
took it (the deployment platform controller has always exported ``RB_SSH_*``, so the SSH branch won), and its SDK
was never a declared dependency, so a clean install could only ever raise ImportError on it.

The functions here keep their old signatures on purpose. Thirty-seven call sites across
``pipeline.py`` and ``stages/`` pass ``(client, script, instance_id=...)`` and poll for an
invoke_id; that async submit/poll shape is ECS-inherited but it is also what lets a 20h recreation
survive a dropped connection, so it stays. Only the transport underneath changed.

``instance_id`` is accepted and ignored. Under SSH the client already knows its host, and renaming
the parameter would touch every one of those call sites for no behaviour change.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.vmclient import VmClient, get_vm_client  # noqa: E402,F401


def load_env(path: str | Path = ".env") -> None:
    """Load environment variables from a .env file. Supports `export` prefix."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            line = line.removeprefix("export ")
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip("'").strip('"'))


def run_command(
    client: VmClient,
    script: str,
    instance_id: str | None = None,
    timeout: int = 3600,
) -> str:
    """Launch a shell script on the VM detached. Returns an invoke_id to poll.

    ``instance_id`` is ignored -- see the module docstring.
    """
    return client.submit(script, timeout=timeout)


def check_invocation(client: VmClient, invoke_id: str) -> dict | None:
    """Status of a launched script: {status, exit_code, output}."""
    return client.poll(invoke_id)


def upload_to_vm(
    client: VmClient,
    content: str,
    remote_path: str,
    instance_id: str | None = None,
    chunk_size: int | None = None,
) -> None:
    """Write text to a file on the VM.

    The ECS version chunked base64 through RunCommand 8000 characters at a time with a sleep
    between chunks; this is one SFTP write. ``instance_id``/``chunk_size`` are ignored.
    """
    client.upload_text(content, remote_path)
