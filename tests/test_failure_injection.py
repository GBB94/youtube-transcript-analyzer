"""Phase 8 — failure-injection suite. Deterministic faults (not luck), each asserting
the CORRECT reason and a clean end state (no half-written cache, exceptions that must
propagate do propagate). Faults are injected through the strategies' existing seams
(fake client / runner / transcriber)."""
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from transcript_tool.cache import Cache
from transcript_tool.policy import EgressPolicy, Policy
from transcript_tool.provisioning import ModelUnavailable
from transcript_tool.schema import Outcome, Reason, VideoRef
from transcript_tool.strategies.api_captions import ApiCaptionsStrategy
from transcript_tool.strategies.local_whisper import LocalWhisperStrategy
from transcript_tool.strategies.managed import ManagedNativeStrategy, ManagedResponse
from transcript_tool.strategies.ytdlp_subs import ProcResult, YtdlpSubsStrategy

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
GATED = Policy(egress=EgressPolicy(allow_public_url=True),
               enabled_strategies=("managed_native", "ytdlp_subs", "api_captions", "local_whisper"))


def _run(strat, ref=None, policy=GATED):
    return asyncio.run(strat.fetch(ref or VideoRef(source="url", url=URL), policy))


# --- managed provider faults -------------------------------------------------

class _FakeManaged:
    def __init__(self, response=None, exc=None):
        self.response, self.exc = response, exc

    def request(self, *a, **k):
        if self.exc:
            raise self.exc
        return self.response


def test_429_maps_to_rate_limited_with_retry():
    r = _run(ManagedNativeStrategy(client=_FakeManaged(ManagedResponse(429, {"error": "x"}, {"Retry-After": "12"}))))
    assert r.outcome is Outcome.failed and r.reason is Reason.rate_limited
    assert r.retry.eligible and r.retry.not_before is not None


def test_5xx_maps_to_provider_error():
    r = _run(ManagedNativeStrategy(client=_FakeManaged(ManagedResponse(503, {"error": "x"}, {}))))
    assert r.reason is Reason.provider_error


def test_managed_outage_transport_exception_is_provider_error():
    r = _run(ManagedNativeStrategy(client=_FakeManaged(exc=ConnectionError("upstream down"))))
    assert r.outcome is Outcome.failed and r.reason is Reason.provider_error


# --- yt-dlp subprocess faults ------------------------------------------------

def _runner_raising(exc):
    def run(args, workdir):
        raise exc
    return run


def _runner_returning(returncode, stderr):
    def run(args, workdir):
        return ProcResult(returncode, "", stderr, workdir)
    return run


def test_missing_yt_dlp_binary_is_missing_dependency():
    r = _run(YtdlpSubsStrategy(runner=_runner_raising(FileNotFoundError())))
    assert r.reason is Reason.missing_dependency


def test_subprocess_timeout_is_timeout():
    r = _run(YtdlpSubsStrategy(runner=_runner_raising(subprocess.TimeoutExpired("yt-dlp", 120))))
    assert r.reason is Reason.timeout


def test_no_js_runtime_is_missing_js_runtime():
    r = _run(YtdlpSubsStrategy(runner=_runner_returning(1, "ERROR: No supported JavaScript runtime found")))
    assert r.outcome is Outcome.failed and r.reason is Reason.missing_js_runtime


def test_po_token_rejection_ytdlp():
    r = _run(YtdlpSubsStrategy(runner=_runner_returning(1, "ERROR: PO Token required for this request")))
    assert r.reason is Reason.po_token_rejected


# --- api_captions faults -----------------------------------------------------

class _PoTokenRequired(Exception):
    pass


class _FakeApi:
    def __init__(self, exc):
        self.exc = exc

    def list(self, vid):
        raise self.exc


def test_po_token_rejection_api_captions():
    # api_captions maps an exception class named 'PoTokenRequired' -> po_token_rejected.
    _PoTokenRequired.__name__ = "PoTokenRequired"
    r = _run(ApiCaptionsStrategy(client=_FakeApi(_PoTokenRequired())))
    assert r.reason is Reason.po_token_rejected


# --- local_whisper / ASR faults ----------------------------------------------

def _audio_file(tmp_path) -> VideoRef:
    p = tmp_path / "clip.wav"
    p.write_bytes(b"\x00")
    return VideoRef(source="uploaded_file", path=str(p))


def test_unprovisioned_model_is_missing_dependency(tmp_path):
    def t(media, langs, spec):
        raise ModelUnavailable("model 'small' not provisioned")
    strat = LocalWhisperStrategy(transcriber=t)
    r = _run(strat, ref=_audio_file(tmp_path), policy=Policy(enabled_strategies=("local_whisper",)))
    assert r.reason is Reason.missing_dependency


def test_asr_timeout_is_timeout(tmp_path):
    import time as _t

    def slow(media, langs, spec):
        _t.sleep(0.5)
        raise AssertionError("should have timed out")
    strat = LocalWhisperStrategy(transcriber=slow, timeout_s=0)
    r = _run(strat, ref=_audio_file(tmp_path), policy=Policy(enabled_strategies=("local_whisper",)))
    assert r.reason is Reason.timeout


def test_cancellation_propagates_not_swallowed(tmp_path):
    """A CancelledError must NOT be converted into provider_error — cancellation is
    control flow, not a provider fault."""
    def cancel(media, langs, spec):
        raise asyncio.CancelledError()
    strat = LocalWhisperStrategy(transcriber=cancel)
    with pytest.raises(asyncio.CancelledError):
        _run(strat, ref=_audio_file(tmp_path), policy=Policy(enabled_strategies=("local_whisper",)))


# --- cache / storage faults (clean state) ------------------------------------

def test_corrupt_cache_entry_is_removed_clean(tmp_path):
    cache = Cache(tmp_path)
    key = cache.request_key("file:/x", "h", "1.0.0", "3.0.0")
    path = cache._result_path(key)
    path.write_text("{ corrupt")
    assert cache.get(key) is None
    assert not path.exists()                      # clean: poisoned entry evicted


def test_atomic_write_failure_leaves_no_partial_or_temp(tmp_path, monkeypatch):
    """Simulated full disk mid-write: the target must not appear half-written and no
    temp file may be orphaned (tempfile + os.replace discipline)."""
    cache = Cache(tmp_path)
    target = tmp_path / "results" / "x.json"

    real_replace = os.replace
    def boom(src, dst):
        raise OSError("No space left on device")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        cache._atomic_write(target, "data")
    monkeypatch.setattr(os, "replace", real_replace)

    assert not target.exists()                    # no half-written target
    leftover = [p for p in (tmp_path / "results").iterdir() if p.name != "x.json"]
    assert leftover == []                         # no orphaned temp file
