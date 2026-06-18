"""Pluggable lock backend (Phase 7 seam).

The cache's singleflight contract — *take the per-request-key lock, then re-check the
cache* — must hold in both deployment profiles. The `local` profile uses a filesystem
lock; the `server` profile swaps in a shared lock (Redis/DB) WITHOUT changing the
contract or the cache code. Choosing the specific datastore is a deployment decision;
this module is the swappable interface plus the local implementation.
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Protocol


class LockTimeout(RuntimeError):
    """Could not acquire the lock within the configured timeout."""


class LockBackend(Protocol):
    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        """Mutually-exclude on `key`. The caller re-checks the cache after acquiring
        it (singleflight). Must release on exit even if the body raises."""
        ...


class FileLockBackend:
    """Local profile: an `fcntl.flock` advisory lock held on an open fd.

    Why flock and not an O_EXCL marker file: an O_EXCL lock file left behind by a
    process that **died holding it** is a permanent stale lock — every future acquirer
    spins forever. A batch with duplicate links + multiple concurrent requests for the
    same video is the worst case (see UI scope §7). An flock is owned by the open file
    description and is **released automatically by the kernel when the process dies**,
    so a crashed holder never wedges the key. The lock file itself is just a handle and
    may safely persist on disk.

    `timeout` (seconds) bounds the wait so a live-but-stuck holder surfaces as a
    LockTimeout instead of an indefinite hang; None blocks until acquired.
    """
    def __init__(self, lock_dir: Path, poll_seconds: float = 0.02,
                 timeout: Optional[float] = 120.0):
        self.lock_dir = Path(lock_dir)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.poll_seconds = poll_seconds
        self.timeout = timeout

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        lock_path = self.lock_dir / f"{key}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self._acquire(fd, key)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)

    def _acquire(self, fd: int, key: str) -> None:
        if self.timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)          # block until acquired
            return
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"timed out acquiring lock for {key!r}")
                time.sleep(self.poll_seconds)


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
