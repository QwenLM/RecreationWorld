"""Filesystem implementation of the artifact-store contract."""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from core.artifact_store import Pathish, normalize_artifact_key


class FilesystemArtifactStore:
    """Map object keys to files below one local root.

    This backend supports local and open-source runs without object-store
    credentials.  Keys use the same forward-slash form as the remote backend.
    """

    def __init__(self, root: Pathish):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        relative = normalize_artifact_key(key)
        return self.root.joinpath(*relative.split("/"))

    def get_file(self, key: str, destination: Pathish) -> None:
        source = self._path(key)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put_file(self, key: str, source: Pathish) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(source), target)

    def put_bytes(self, key: str, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("artifact payload must be bytes")
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def iter_keys(self, prefix: str) -> Iterable[str]:
        if not isinstance(prefix, str) or "\\" in prefix:
            raise ValueError("artifact prefix must be a POSIX path")
        if prefix:
            self._path(prefix)
        if not self.root.exists():
            return iter(())
        keys = sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
        )
        return (key for key in keys if key.startswith(prefix))

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def last_modified(self, key: str) -> datetime | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
