"""Pluggable lock backend (Phase 7 seam).

The cache's singleflight contract — *take the per-request-key lock, then re-check the
cache* — must hold in both deployment profiles. The `local` profile uses a filesystem
lock; the `server` profile swaps in a shared lock (Redis/DB) WITHOUT changing the
contract or the cache code. Choosing the specific datastore is a deployment decision;
this module is the swappable interface plus the local implementation.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol


class LockBackend(Protocol):
    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        """Mutually-exclude on `key`. The caller re-checks the cache after acquiring
        it (singleflight). Must release on exit even if the body raises."""
        ...


class FileLockBackend:
    """Local profile: atomic O_EXCL create + brief spin. Works across processes on a
    shared filesystem, but not across hosts — that's the server backend's job."""
    def __init__(self, lock_dir: Path, poll_seconds: float = 0.05):
        self.lock_dir = Path(lock_dir)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.poll_seconds = poll_seconds

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        lock_path = self.lock_dir / f"{key}.lock"
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                time.sleep(self.poll_seconds)
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


class SharedLockBackend:
    """Server profile: a cross-host lock (Redis SETNX+TTL, or a DB advisory lock).
    Deploy-time choice — left as a documented stub so the interface is real and the
    `local` path stays dependency-free. The contract it MUST honor:
      - atomic acquire keyed by request-key, with a TTL/lease so a dead worker can't
        wedge the key forever;
      - the caller re-checks the cache after acquiring (singleflight preserved);
      - release is idempotent and safe under crash (lease expiry).
    """
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "SharedLockBackend is a Phase 7 deployment component (Redis/DB). "
            "Implement against the contract in this docstring for the server profile.")

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:  # pragma: no cover - interface only
        raise NotImplementedError
        yield
