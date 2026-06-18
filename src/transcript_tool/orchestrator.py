"""Orchestrator: preflight (hints) -> policy-ordered strategies -> result, with
singleflight via the cache lock and a post-lock re-check.

Phase 1 wires the uploaded_caption strategy; later strategies register here.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

from .cache import Cache
from .policy import Policy
from .preflight import preflight
from .schema import (
    Attempt, Outcome, Reason, Result, VideoRef,
    NORMALIZER_VERSION, SCHEMA_VERSION,
)
from .strategies.base import Strategy
from .strategies.uploaded_caption import UploadedCaptionStrategy
from .strategies.api_captions import ApiCaptionsStrategy
from .strategies.ytdlp_subs import YtdlpSubsStrategy
from .strategies.local_whisper import LocalWhisperStrategy
from .strategies.managed import (
    ManagedNativeStrategy, ManagedAsrStrategy, ManagedUrlToAsrStrategy,
)

# Registry: name -> strategy instance. Order is resolved per-request from policy.
REGISTRY: dict[str, Strategy] = {
    "uploaded_caption": UploadedCaptionStrategy(),   # Phase 1
    "api_captions": ApiCaptionsStrategy(),           # Phase 2
    "ytdlp_subs": YtdlpSubsStrategy(),               # Phase 3
    "local_whisper": LocalWhisperStrategy(),         # Phase 4
    "managed_native": ManagedNativeStrategy(),       # Phase 5
    "managed_asr": ManagedAsrStrategy(),             # Phase 5
    "managed_url_to_asr": ManagedUrlToAsrStrategy(), # Phase 5
}


def _canonical_source(ref: VideoRef) -> str:
    if ref.source == "uploaded_file" and ref.path:
        return f"file:{Path(ref.path).resolve()}"
    return f"{ref.platform}:{ref.id or ref.url}"


async def get_transcript(ref: VideoRef, policy: Optional[Policy] = None,
                         cache: Optional[Cache] = None) -> Result:
    policy = policy or Policy()
    key = None
    if cache is not None:
        key = cache.request_key(_canonical_source(ref), policy.policy_hash(),
                                NORMALIZER_VERSION, SCHEMA_VERSION)
        hit = cache.get(key)
        if hit is not None:
            return hit

    # Singleflight: take the lock, then RE-CHECK before doing work.
    if cache is not None and key is not None:
        with cache.lock(key):
            hit = cache.get(key)
            if hit is not None:
                return hit
            result = await _run_pipeline(ref, policy)
            cache.put(key, result)
            return result

    return await _run_pipeline(ref, policy)


async def _run_pipeline(ref: VideoRef, policy: Policy) -> Result:
    # Stage 1 - preflight produces HINTS; only authoritative terminals short-circuit.
    pf = preflight(ref, policy)
    if pf is not None:
        return pf

    ordered = [REGISTRY[n] for n in policy.enabled_strategies if n in REGISTRY]
    attempts: list[Attempt] = []
    last: Optional[Result] = None

    for strat in ordered:
        if not strat.applicable(ref, policy):
            continue
        try:
            res = await strat.fetch(ref, policy)
        except NotImplementedError:
            continue
        except Exception as e:  # unexpected -> provider_error, keep going
            t = time.monotonic()
            attempts.append(Attempt(strategy=strat.name, ok=False,
                                    reason=Reason.provider_error,
                                    quality_rejections=[type(e).__name__]))
            continue

        attempts.extend(res.attempts)
        if res.outcome is Outcome.success:
            res.attempts = attempts
            return res
        last = res  # remember last non-success to surface if nothing succeeds

    # Nothing succeeded.
    if last is not None:
        last.attempts = attempts
        return last
    res = Result.make_unavailable(ref, Reason.no_acceptable_transcript)
    res.attempts = attempts
    return res


# --- sync convenience wrapper (guarded against a running loop) ----------------

def get_transcript_sync(ref: VideoRef, policy: Optional[Policy] = None,
                        cache: Optional[Cache] = None) -> Result:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(get_transcript(ref, policy, cache))
    raise RuntimeError(
        "get_transcript_sync() called from a running event loop; "
        "await get_transcript(...) instead."
    )
