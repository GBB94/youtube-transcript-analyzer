"""Caching with the lifecycle contract from the design review.

Three stores:
  - request-result: keyed by canonical_source + policy_hash + normalizer_version
    + schema_version. "Have we already produced a transcript for this request shape?"
  - artifact: keyed by media/track identity + model revision + decoding settings.
    Reused across requests. (Stub here; populated by acquisition strategies.)
  - metadata: separate, with its own refresh policy (<=30 days, see DESIGN.md §4).

Contract enforced here:
  - Per-request-key lock; RE-CHECK the cache after acquiring the lock (singleflight).
  - A cache HIT is labelled via CacheProvenance — we do NOT replay old `attempts`
    as though acquisition happened again.
  - Reason-specific negative TTLs. CONFIG failures / cancellations are NOT
    persistently negative-cached (fixing config must invalidate them).
  - Referential integrity: a request-result entry referencing an evicted artifact
    (dangling raw_cues_ref) is treated as a miss, not returned.

This is the `local` profile implementation (disk + filesystem lock). The `server`
profile swaps the store/lock for a shared backend (see DESIGN.md §14).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from .schema import CONFIG_FAILURE_REASONS, Outcome, Reason, Result

# Reason-specific negative TTLs (seconds). Absent => not negative-cached.
NEGATIVE_TTL = {
    Reason.removed: 7 * 24 * 3600,
    Reason.private: 24 * 3600,
    Reason.captions_unavailable: 6 * 3600,
    Reason.language_unavailable: 6 * 3600,
    Reason.rate_limited: 60,
    Reason.timeout: 60,
    Reason.provider_error: 300,
    # config failures + cancellations intentionally absent (never persisted)
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Cache:
    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "results").mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(parents=True, exist_ok=True)
        (self.root / "locks").mkdir(parents=True, exist_ok=True)

    # ---- keys ---------------------------------------------------------------

    @staticmethod
    def request_key(canonical_source: str, policy_hash: str,
                    normalizer_version: str, schema_version: str) -> str:
        import hashlib
        raw = "|".join([canonical_source, policy_hash, normalizer_version, schema_version])
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _result_path(self, key: str) -> Path:
        return self.root / "results" / f"{key}.json"

    def _artifact_exists(self, ref: Optional[str]) -> bool:
        if not ref:
            return True  # nothing to dangle
        safe = ref.replace(":", "_").replace("/", "_")
        return (self.root / "artifacts" / safe).exists()

    # ---- locking (local profile: filesystem lock) ---------------------------

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        lock_path = self.root / "locks" / f"{key}.lock"
        # Atomic create; spin briefly. (server profile uses a real distributed lock.)
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    # ---- get / put ----------------------------------------------------------

    def get(self, key: str) -> Optional[Result]:
        path = self._result_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            self._safe_unlink(path)            # corrupt entry recovery
            return None

        # negative-cache expiry
        expires_at = data.get("_expires_at")
        if expires_at and _utcnow().timestamp() > expires_at:
            self._safe_unlink(path)
            return None

        try:
            result = Result.model_validate(data["result"])
        except Exception:
            self._safe_unlink(path)
            return None

        # referential integrity: dangling artifact -> miss
        if result.outcome is Outcome.success and not self._artifact_exists(result.raw_cues_ref):
            self._safe_unlink(path)
            return None

        # label as a cache hit; do NOT replay old attempts as fresh
        result.cache.served_from_cache = True
        result.cache.cached_at = data.get("_cached_at")
        result.cache.cache_layer = "request_result"
        result.attempts = []
        return result

    def put(self, key: str, result: Result) -> None:
        # Decide whether this is cacheable.
        ttl: Optional[int] = None
        if result.outcome is Outcome.success:
            ttl = None  # cache until stale / evicted
        else:
            if result.reason in CONFIG_FAILURE_REASONS or result.reason is Reason.cancelled:
                return  # never persistently negative-cache config failures / cancellations
            ttl = NEGATIVE_TTL.get(result.reason) if result.reason else None
            if ttl is None:
                return  # not negative-cacheable

        now = _utcnow()
        payload = {
            "_cached_at": now.isoformat(),
            "_expires_at": (now + timedelta(seconds=ttl)).timestamp() if ttl else None,
            "result": result.model_dump(mode="json"),
        }
        # Register the artifact the result references, so the referential-integrity
        # check is meaningful: if this artifact is later evicted, get() will treat
        # the (now dangling) result as a miss.
        if result.outcome is Outcome.success and result.raw_cues_ref:
            self._write_artifact(result.raw_cues_ref)
        self._atomic_write(self._result_path(key), json.dumps(payload))

    def _write_artifact(self, ref: str) -> None:
        safe = ref.replace(":", "_").replace("/", "_")
        path = self.root / "artifacts" / safe
        if not path.exists():
            self._atomic_write(path, ref)

    # ---- internals ----------------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, path)              # atomic on POSIX
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
