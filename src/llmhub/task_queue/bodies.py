"""Request/result body files (0600 files in a 0700 directory, tmp + rename)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class BodyStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)

    def _write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        tmp = self.root / (name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        tmp.rename(path)
        return path

    def write_request(self, job_id: str, body: bytes, meta: dict[str, Any]) -> Path:
        self._write(f"{job_id}.meta.json", json.dumps(meta).encode())
        return self._write(f"{job_id}.req", body)

    def write_result(self, job_id: str, body: bytes) -> Path:
        return self._write(f"{job_id}.res", body)

    def read_request(self, job_id: str) -> bytes:
        return (self.root / f"{job_id}.req").read_bytes()

    def read_result(self, job_id: str) -> bytes:
        return (self.root / f"{job_id}.res").read_bytes()

    def iter_recoverable(self) -> Iterator[tuple[str, dict[str, Any], bytes]]:
        """Requests that have no result file (for safe-mode recovery)."""
        for req in sorted(self.root.glob("*.req")):
            job_id = req.stem
            if (self.root / f"{job_id}.res").exists():
                continue
            meta_path = self.root / f"{job_id}.meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            yield job_id, meta, req.read_bytes()

    def purge(self, older_than: float) -> None:
        for p in self.root.iterdir():
            if p.stat().st_mtime < older_than:
                p.unlink(missing_ok=True)
