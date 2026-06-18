"""Cache lifecycle hardening. A corrupt results file must never crash get() and
must be self-healing: treated as a miss and removed so the next request rebuilds
it cleanly."""
import asyncio
from pathlib import Path

from transcript_tool import get_transcript, Policy, VideoRef
from transcript_tool.cache import Cache

FIX = Path(__file__).parent / "fixtures"


def test_corrupt_json_is_miss_and_removed(tmp_path):
    cache = Cache(tmp_path)
    key = cache.request_key("file:/whatever", "deadbeefdeadbeef", "1.0.0", "3.0.0")
    path = cache._result_path(key)
    path.write_text("{ this is not valid json :::")

    assert cache.get(key) is None          # treated as a miss, not an exception
    assert not path.exists()               # corrupt entry is evicted


def test_valid_json_but_invalid_result_is_miss_and_removed(tmp_path):
    cache = Cache(tmp_path)
    key = cache.request_key("file:/whatever", "cafef00dcafef00d", "1.0.0", "3.0.0")
    path = cache._result_path(key)
    # Parses as JSON, but the payload is not a valid Result.
    path.write_text('{"_cached_at": null, "_expires_at": null, "result": {"nope": 1}}')

    assert cache.get(key) is None
    assert not path.exists()


def test_corrupt_entry_does_not_block_recompute(tmp_path):
    """End-to-end: a poisoned cache file for the exact request key must not stop
    the pipeline from recomputing and re-caching a good result."""
    cache = Cache(tmp_path)
    ref = VideoRef(platform="local", source="uploaded_file",
                   path=str(FIX / "rolling_autocaption.vtt"))

    # Compute the key the orchestrator will use and poison it.
    from transcript_tool.orchestrator import _canonical_source
    from transcript_tool.schema import NORMALIZER_VERSION, SCHEMA_VERSION
    key = cache.request_key(_canonical_source(ref), Policy().policy_hash(),
                            NORMALIZER_VERSION, SCHEMA_VERSION)
    cache._result_path(key).write_text("garbage")

    res = asyncio.run(get_transcript(ref, Policy(), cache))
    assert res.outcome.value == "success"
    assert res.cache.served_from_cache is False     # recomputed, not served stale

    # And the recomputed result is now cached cleanly for next time.
    again = asyncio.run(get_transcript(ref, Policy(), cache))
    assert again.cache.served_from_cache is True
    assert again.attempts == []
