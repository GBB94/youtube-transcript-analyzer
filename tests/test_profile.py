"""Phase 7 — server-profile seam. Resource-limit enforcement, warm-at-startup, the
pluggable lock backend, and the distributed-singleflight contract (preserved across
the file lock; the shared backend swaps in for cross-host without changing it)."""
import threading
import time

from transcript_tool.cache import Cache
from transcript_tool.locking import FileLockBackend, SharedLockBackend
from transcript_tool.policy import EgressPolicy, Policy
from transcript_tool.profiles import (
    LOCAL_PROFILE, SERVER_PROFILE, ResourceLimits, enforce_limit,
)
from transcript_tool.provisioning import ModelSpec, warm
from transcript_tool.schema import Outcome, Reason, Result, Segment, Provenance, Language, TimestampType, VideoRef


# --- resource limits ---------------------------------------------------------

def test_enforce_limit_fails_with_named_dimension():
    ref = VideoRef(source="url", url="https://x/y")
    r = enforce_limit(ref, "duration", measured=7200, limit=3600)
    assert r is not None and r.outcome is Outcome.failed
    assert r.reason is Reason.resource_limit_exceeded
    assert r.resource_dimension == "duration"


def test_enforce_limit_under_ceiling_is_none():
    assert enforce_limit(VideoRef(), "bytes", measured=10, limit=100) is None
    assert enforce_limit(VideoRef(), "cost", measured=1.0, limit=None) is None


def test_advisory_limit_never_fails():
    # memory is advisory locally (hard=False) -> exceeding it does not fail the request
    assert enforce_limit(VideoRef(), "memory", measured=10**9, limit=1, hard=False) is None


def test_profiles_differ_on_hardness_and_warm():
    assert LOCAL_PROFILE.memory_is_hard is False and LOCAL_PROFILE.warm_models_at_startup is False
    assert SERVER_PROFILE.memory_is_hard is True and SERVER_PROFILE.warm_models_at_startup is True
    assert SERVER_PROFILE.lock_backend == "shared" and LOCAL_PROFILE.lock_backend == "file"


# --- warm-at-startup ---------------------------------------------------------

def test_warm_loads_each_spec_once():
    calls = []
    def fake_loader(spec, store_dir):
        calls.append(spec.size)
        return object()
    n = warm([ModelSpec(size="small"), ModelSpec(size="tiny")], "/tmp/models", loader=fake_loader)
    assert n == 2 and calls == ["small", "tiny"]


# --- lock backend / singleflight contract ------------------------------------

def test_file_lock_is_mutually_exclusive(tmp_path):
    backend = FileLockBackend(tmp_path / "locks", poll_seconds=0.001)
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()

    def worker():
        with backend.lock("samekey"):
            with lock:
                concurrent["now"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["now"])
            time.sleep(0.05)
            with lock:
                concurrent["now"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert concurrent["max"] == 1                 # never two holders at once


def test_shared_lock_backend_is_a_documented_stub():
    import pytest
    with pytest.raises(NotImplementedError):
        SharedLockBackend()


class _CountingStrategy:
    name = "counting"

    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def applicable(self, ref, policy):
        return True

    async def fetch(self, ref, policy):
        with self._lock:
            self.calls += 1
        time.sleep(0.2)                            # hold long enough for the other thread to contend
        return Result.make_success(
            ref, provenance=Provenance.human_caption, text="hello world",
            segments=[Segment(start=0.0, end=1.0, text="hello world")],
            language=Language(requested=["en"], selected="en"),
            timestamp_type=TimestampType.caption_cue, raw_text="hello world")


def test_concurrent_requests_cause_one_acquisition(tmp_path, monkeypatch):
    """Distributed-singleflight contract: two concurrent calls on the same key cause
    exactly ONE underlying acquisition; the second is served from cache."""
    from transcript_tool import orchestrator
    counting = _CountingStrategy()
    monkeypatch.setitem(orchestrator.REGISTRY, "counting", counting)

    cache = Cache(tmp_path)
    ref = VideoRef(source="url", url="https://example.com/v")
    policy = Policy(enabled_strategies=("counting",), egress=EgressPolicy(allow_public_url=True))

    results = []
    def call():
        results.append(orchestrator.get_transcript_sync(ref, policy, cache))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert counting.calls == 1                     # singleflight: one acquisition only
    assert all(r.outcome is Outcome.success for r in results)
    assert any(r.cache.served_from_cache for r in results)   # the loser got the cached result
