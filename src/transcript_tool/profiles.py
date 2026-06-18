"""Deployment profiles + hard resource limits (Phase 7 seam).

Same core, two profiles (DESIGN.md §14). The `local` profile stays simple (advisory
memory, filesystem lock, lazy model load). The `server` profile makes limits hard
(container/cgroup), uses a shared lock/store, and warms models at startup. The actual
datastore / container runtime is a deployment choice; this module is the swappable
configuration, not a specific orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from .schema import Reason, Result, ResourceDimension, VideoRef


@dataclass(frozen=True)
class ResourceLimits:
    """Hard ceilings (DESIGN.md §11). Duration/bytes/cost/runtime are hard in BOTH
    profiles; memory is advisory locally and hard via cgroup on the server. Exceeding
    a hard limit => failed/resource_limit_exceeded with the named dimension."""
    max_duration_seconds: Optional[float] = None     # media duration
    max_bytes: Optional[int] = None                  # downloaded media size
    max_cost_usd: Optional[float] = None             # provider spend per request
    max_runtime_seconds: Optional[float] = None      # wall-clock per request
    max_memory_bytes: Optional[int] = None           # advisory local / hard server


def enforce_limit(ref: VideoRef, dimension: ResourceDimension, measured: float,
                  limit: Optional[float], *, hard: bool = True) -> Optional[Result]:
    """Return a failed/resource_limit_exceeded Result if a hard limit is exceeded,
    else None. An advisory limit (hard=False, e.g. memory locally) never fails the
    request — the caller may warn instead."""
    if limit is None or measured <= limit:
        return None
    if not hard:
        return None
    return Result.make_failed(ref, Reason.resource_limit_exceeded, resource_dimension=dimension)


@dataclass(frozen=True)
class Profile:
    name: Literal["local", "server"]
    limits: ResourceLimits
    memory_is_hard: bool          # advisory locally; hard via cgroup on the server
    warm_models_at_startup: bool  # server warms once at boot; local loads lazily
    lock_backend: Literal["file", "shared"]
    sandbox_media_decode: bool    # decode untrusted media in a supervised child (server)


LOCAL_PROFILE = Profile(
    name="local",
    limits=ResourceLimits(max_duration_seconds=6 * 3600, max_bytes=2 * 1024**3,
                          max_cost_usd=5.0, max_runtime_seconds=1800),
    memory_is_hard=False, warm_models_at_startup=False,
    lock_backend="file", sandbox_media_decode=False,
)

SERVER_PROFILE = Profile(
    name="server",
    limits=ResourceLimits(max_duration_seconds=4 * 3600, max_bytes=1 * 1024**3,
                          max_cost_usd=2.0, max_runtime_seconds=900,
                          max_memory_bytes=4 * 1024**3),
    memory_is_hard=True, warm_models_at_startup=True,
    lock_backend="shared", sandbox_media_decode=True,
)
