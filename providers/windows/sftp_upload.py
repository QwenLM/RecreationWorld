#!/usr/bin/env python3
"""Upload one file to a Windows host over SFTP.

Password is read from RB_WINDOWS_PASSWORD to avoid putting secrets in argv.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: sftp_upload.py <host> <user> <local-path> <remote-path>", file=sys.stderr)
        return 2
    host, user, local_path, remote_path = sys.argv[1:]
    password = os.environ.get("RB_WINDOWS_PASSWORD")
    if not password:
        raise RuntimeError("RB_WINDOWS_PASSWORD is required")
    local = Path(local_path)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=password, timeout=15, banner_timeout=15, auth_timeout=15)
    sftp = ssh.open_sftp()
    sftp.put(str(local), remote_path)
    sftp.close()
    ssh.close()
    print(remote_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
