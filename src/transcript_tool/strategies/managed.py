"""managed (Phase 5) — managed transcript providers.

Three strategies that offload IP/bot-blocking and PO-token churn to a provider's
infrastructure. They are **additional capacity, not "blocking-proof"**: providers
have outages and rate limits and cannot reach private / members-only / age-restricted
videos. They are fallbacks, ordered behind the free strategies by policy.

  managed_native     — a CAPTION strategy: fetch EXISTING captions via the provider's
                       captions-only/native mode. MUST NOT use an `auto` mode that
                       silently performs paid AI transcription (that is managed_asr
                       and would bill the operator unexpectedly).
  managed_asr        — a TRANSCRIPTION strategy over media the pipeline acquired
                       (uploaded file, or media.acquire_audio). provenance=managed_asr.
  managed_url_to_asr — a COMPOUND strategy: hand the provider a URL, get a transcript
                       back, with NO intermediate local media artifact.

Shared contracts (see docs/PHASE_5_6_BUILD.md §5):
- Key-gated: no key => not applicable (skipped, never a hard failure). A configured
  key the provider rejects => access_challenge (auth/bot wall) or provider_error.
- Injectable HTTP client: tests inject a fake returning recorded fixtures; no live
  calls in CI. The default client lazily imports httpx.
- Cost is real: Cost{amount, unit, currency, estimated}. unit in
  {"provider_credits","usd"}; estimated=True unless the provider returns an exact charge.
- Error mapping to the outcome model (see _map_status).
- Translation provenance only when the provider DISCLOSES it (sets detection_method);
  never inferred from text.
- Egress-gated: these are network strategies; they require EgressPolicy.allow_public_url
  even for an uploaded file, because the media/URL is sent to a third party.
- Redaction: keys and provider request IDs are redacted from logs (security.redact).

Reference provider response shape (what a fake client returns in tests):
    {
      "request_id": "req_abc",
      "transcript": {
        "language": "en",
        "is_generated": false,           # human vs platform-auto captions (native mode)
        "translated": false,             # provider-disclosed translation
        "source_language": "es",         # present only when translated
        "segments": [{"start": 0.0, "end": 2.0, "text": "..."}]
      },
      "usage": {"credits": 12, "amount_usd": 0.03, "exact": false}
    }
On error the provider returns an HTTP status + a body like {"error": "<code>"} where
<code> is one of: captions_unavailable, language_unavailable, removed, members_only,
age_restricted, age_gated.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional, Protocol

from ..policy import Policy
from ..quality import evaluate, rejected, rejection_reasons
from ..schema import (
    Attempt, Cost, Language, Provenance, Reason, Result, Retry, Segment,
    TimestampType, VideoRef, _utcnow,
)
from ..security import redact
from .api_captions import extract_video_id

DEFAULT_BASE_URL = "https://api.example-transcripts.com"


# --- transport ---------------------------------------------------------------

@dataclass
class ManagedResponse:
    status_code: int
    body: dict
    headers: dict


class ManagedClient(Protocol):
    """Minimal transport contract. The real client wraps httpx; tests inject a fake
    that returns recorded ManagedResponse fixtures."""
    def request(self, method: str, path: str, *, json: Optional[dict] = None,
                params: Optional[dict] = None) -> ManagedResponse: ...


class HttpxClient:
    """Default client. Lazily imports httpx so CI (which injects fakes) needs no
    network dependency. The API key is sent as a Bearer header and never logged."""
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 60.0):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def request(self, method: str, path: str, *, json=None, params=None) -> ManagedResponse:
        import httpx  # lazy: only needed for live calls
        headers = {"Authorization": f"Bearer {self._api_key}"}
        resp = httpx.request(method, f"{self._base_url}{path}", json=json, params=params,
                             headers=headers, timeout=self._timeout)
        try:
            body = resp.json()
        except Exception:
            body = {}
        return ManagedResponse(resp.status_code, body, dict(resp.headers))


class ProviderError(Exception):
    """Non-2xx from the provider; carries enough to map to the outcome model."""
    def __init__(self, resp: ManagedResponse):
        self.resp = resp
        super().__init__(f"provider returned {resp.status_code}")


# --- error mapping -----------------------------------------------------------

# provider body "error" codes -> unavailable reasons (missing-content 4xx)
_CONTENT_REASONS = {
    "captions_unavailable": Reason.captions_unavailable,
    "language_unavailable": Reason.language_unavailable,
    "removed": Reason.removed,
    "members_only": Reason.members_only,
    "age_restricted": Reason.age_restricted,
    "age_gated": Reason.age_restricted,
}


def _retry_after_seconds(headers: dict) -> Optional[int]:
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _map_status(resp: ManagedResponse) -> tuple[Reason, Optional[int]]:
    """Map a non-2xx provider response to (reason, retry_after_seconds)."""
    status = resp.status_code
    code = str(resp.body.get("error", "")).lower() if isinstance(resp.body, dict) else ""
    if status in (401, 403):
        return Reason.access_challenge, None            # auth / bot wall
    if status == 429:
        return Reason.rate_limited, _retry_after_seconds(resp.headers)
    if 400 <= status < 500:
        if code in _CONTENT_REASONS:
            return _CONTENT_REASONS[code], None
        # an unclassified client error about content -> captions_unavailable; a bad
        # request we caused -> provider_error. Default to the content-missing reading
        # for 404, provider_error otherwise.
        return (Reason.captions_unavailable if status == 404 else Reason.provider_error), None
    return Reason.provider_error, None                  # 5xx / transport


# --- reference adapter -------------------------------------------------------

@dataclass
class ProviderTranscript:
    segments: list[Segment]
    language: Optional[str]
    is_generated: bool
    translated: bool
    source_language: Optional[str]
    cost: Cost
    request_id: Optional[str]


class ReferenceAdapter:
    """Reference provider adapter. One generic implementation; swap the request/parse
    shapes for a specific vendor. `mode` distinguishes captions-only (native) from
    paid transcription (auto)."""

    def __init__(self, client: ManagedClient):
        self.client = client

    def captions(self, ref: VideoRef, languages: list[str]) -> ProviderTranscript:
        # captions-only path: mode MUST be "native" (never "auto" => paid ASR).
        vid = extract_video_id(ref)
        resp = self.client.request("POST", "/v1/transcript", json={
            "video": vid or ref.url, "mode": "native", "languages": languages,
        })
        return self._parse(resp)

    def asr_media(self, media_path: str, languages: list[str]) -> ProviderTranscript:
        resp = self.client.request("POST", "/v1/asr", json={
            "media_ref": media_path, "languages": languages,
        })
        return self._parse(resp)

    def asr_url(self, ref: VideoRef, languages: list[str]) -> ProviderTranscript:
        # compound: provider fetches + transcribes; mode "auto" (paid). No local media.
        resp = self.client.request("POST", "/v1/transcript", json={
            "video": ref.url, "mode": "auto", "languages": languages,
        })
        return self._parse(resp)

    @staticmethod
    def _parse(resp: ManagedResponse) -> ProviderTranscript:
        if not (200 <= resp.status_code < 300):
            raise ProviderError(resp)
        t = resp.body.get("transcript", {})
        usage = resp.body.get("usage", {})
        segs = [Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]).strip())
                for s in t.get("segments", []) if str(s.get("text", "")).strip()]
        if "amount_usd" in usage:
            cost = Cost(amount=float(usage["amount_usd"]), unit="usd", currency="USD",
                        estimated=not bool(usage.get("exact", False)))
        elif "credits" in usage:
            cost = Cost(amount=float(usage["credits"]), unit="provider_credits",
                        estimated=not bool(usage.get("exact", False)))
        else:
            cost = Cost(amount=0.0, unit="provider_credits", estimated=True)
        return ProviderTranscript(
            segments=segs,
            language=t.get("language"),
            is_generated=bool(t.get("is_generated", False)),
            translated=bool(t.get("translated", False)),
            source_language=t.get("source_language"),
            cost=cost,
            request_id=resp.body.get("request_id"),
        )


# --- strategy base -----------------------------------------------------------

class _ManagedBase:
    name = "managed"

    def __init__(self, client: Optional[ManagedClient] = None,
                 api_key: Optional[str] = None, base_url: Optional[str] = None):
        # Key is read from config/env; absence makes the strategy non-applicable.
        self._api_key = api_key if api_key is not None else os.environ.get("MANAGED_API_KEY")
        self._base_url = base_url or os.environ.get("MANAGED_API_BASE_URL", DEFAULT_BASE_URL)
        self._injected = client
        self._adapter: Optional[ReferenceAdapter] = ReferenceAdapter(client) if client else None

    # key-gating is part of applicability: no key => skipped, not a failure
    def _has_key(self) -> bool:
        return self._injected is not None or bool(self._api_key)

    @property
    def adapter(self) -> ReferenceAdapter:
        if self._adapter is None:
            self._adapter = ReferenceAdapter(HttpxClient(self._api_key or "", self._base_url))
        return self._adapter

    # --- result helpers ---
    def _attempt(self, ok: bool, t0: float, cost: Cost, reason: Reason | None = None,
                 request_id: Optional[str] = None, rejections: Optional[list[str]] = None) -> Attempt:
        return Attempt(strategy=self.name, ok=ok, reason=reason,
                       latency_ms=int((time.monotonic() - t0) * 1000), cost=cost,
                       provider_request_id=redact(request_id) if request_id else None,
                       quality_rejections=rejections or [])

    def _from_error(self, ref: VideoRef, err: ProviderError, t0: float) -> Result:
        reason, retry_after = _map_status(err.resp)
        retry = Retry(eligible=retry_after is not None,
                      not_before=(_utcnow() + timedelta(seconds=retry_after)) if retry_after else None,
                      max_attempts=3 if retry_after is not None else 0)
        from ..schema import classify_reason, Outcome
        bucket, _ = classify_reason(reason)
        attempt = self._attempt(False, t0, Cost(unit="provider_credits"),
                                reason=reason, request_id=err.resp.body.get("request_id"))
        if bucket is Outcome.unavailable:
            res = Result.make_unavailable(ref, reason, retry=retry)
        else:
            res = Result.make_failed(ref, reason, retry=retry)
        res.attempts = [attempt]
        return res

    def _success(self, ref: VideoRef, policy: Policy, pt: ProviderTranscript,
                 provenance: Provenance, t0: float) -> Result:
        text = " ".join(s.text for s in pt.segments).strip()
        if not text:
            res = Result.make_unavailable(ref, Reason.captions_unavailable)
            res.attempts = [self._attempt(False, t0, pt.cost, Reason.captions_unavailable, pt.request_id)]
            return res
        gates = evaluate(pt.segments, text, policy.quality)
        if rejected(gates):
            res = Result.make_unavailable(ref, Reason.no_acceptable_transcript)
            res.quality = gates
            res.attempts = [self._attempt(False, t0, pt.cost, Reason.no_acceptable_transcript,
                                          pt.request_id, rejection_reasons(gates))]
            return res
        # Translation provenance only when the provider discloses it.
        prov = Provenance.translated_caption if pt.translated else provenance
        lang = Language(
            requested=list(policy.languages),
            selected=pt.language, track_language=pt.language,
            original_language=pt.source_language if pt.translated else None,
            detection_method="provider_flag" if pt.translated else None,
        )
        import hashlib, json as _json
        raw_ref = "sha256:" + hashlib.sha256(
            _json.dumps([(s.start, s.end, s.text) for s in pt.segments]).encode()).hexdigest()
        res = Result.make_success(
            ref, provenance=prov, text=text, segments=pt.segments, language=lang,
            timestamp_type=TimestampType.caption_cue, raw_text=text, raw_cues_ref=raw_ref,
            track_id=pt.language, duration_seconds=pt.segments[-1].end if pt.segments else 0.0,
            quality=gates,
        )
        res.attempts = [self._attempt(True, t0, pt.cost, request_id=pt.request_id)]
        return res


# --- the three strategies ----------------------------------------------------

class ManagedNativeStrategy(_ManagedBase):
    name = "managed_native"

    def applicable(self, ref: VideoRef, policy: Policy) -> bool:
        return (self.name in policy.enabled_strategies and self._has_key()
                and policy.egress.allow_public_url and ref.source == "url"
                and (extract_video_id(ref) is not None or bool(ref.url)))

    async def fetch(self, ref: VideoRef, policy: Policy) -> Result:
        t0 = time.monotonic()
        try:
            pt = self.adapter.captions(ref, list(policy.languages))
        except ProviderError as e:
            return self._from_error(ref, e, t0)
        except Exception as e:  # noqa: BLE001 — transport/unexpected
            res = Result.make_failed(ref, Reason.provider_error)
            res.attempts = [self._attempt(False, t0, Cost(unit="provider_credits"),
                                          Reason.provider_error, rejections=[type(e).__name__])]
            return res
        # native mode returns existing captions: human vs platform-auto.
        prov = Provenance.platform_auto if pt.is_generated else Provenance.human_caption
        return self._success(ref, policy, pt, prov, t0)


class ManagedAsrStrategy(_ManagedBase):
    name = "managed_asr"
    AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".mp4", ".mkv", ".webm", ".mov"}

    def applicable(self, ref: VideoRef, policy: Policy) -> bool:
        if self.name not in policy.enabled_strategies or not self._has_key():
            return False
        if policy.mode == "captions-only":
            return False
        # Sending media to a third party is egress, even for an uploaded file.
        if not policy.egress.allow_public_url:
            return False
        from pathlib import Path
        if ref.source == "uploaded_file" and ref.path:
            return Path(ref.path).suffix.lower() in self.AUDIO_SUFFIXES
        return ref.source == "url"

    async def fetch(self, ref: VideoRef, policy: Policy) -> Result:
        t0 = time.monotonic()
        media_path = await self._resolve_media(ref, policy)
        if isinstance(media_path, Result):
            return media_path
        try:
            pt = self.adapter.asr_media(media_path, list(policy.languages))
        except ProviderError as e:
            return self._from_error(ref, e, t0)
        except Exception as e:  # noqa: BLE001
            res = Result.make_failed(ref, Reason.provider_error)
            res.attempts = [self._attempt(False, t0, Cost(unit="usd", currency="USD"),
                                          Reason.provider_error, rejections=[type(e).__name__])]
            return res
        return self._success(ref, policy, pt, Provenance.managed_asr, t0)

    async def _resolve_media(self, ref: VideoRef, policy: Policy):
        from pathlib import Path
        if ref.source == "uploaded_file" and ref.path and Path(ref.path).exists():
            return ref.path
        if ref.source == "uploaded_file":
            res = Result.make_failed(ref, Reason.invalid_input)
            res.attempts = [self._attempt(False, time.monotonic(), Cost(unit="usd"), Reason.invalid_input)]
            return res
        import asyncio
        from ..media import acquire_audio, MediaError
        try:
            return await asyncio.to_thread(acquire_audio, ref, policy)
        except MediaError as me:
            res = Result.make_failed(ref, me.reason)
            res.attempts = [self._attempt(False, time.monotonic(), Cost(unit="usd"), me.reason)]
            return res


class ManagedUrlToAsrStrategy(_ManagedBase):
    """Compound: provider takes a URL and returns a transcript with no intermediate
    local media artifact. Distinct cost/latency profile from media + managed_asr."""
    name = "managed_url_to_asr"

    def applicable(self, ref: VideoRef, policy: Policy) -> bool:
        return (self.name in policy.enabled_strategies and self._has_key()
                and policy.mode != "captions-only"
                and policy.egress.allow_public_url and ref.source == "url" and bool(ref.url))

    async def fetch(self, ref: VideoRef, policy: Policy) -> Result:
        t0 = time.monotonic()
        try:
            pt = self.adapter.asr_url(ref, list(policy.languages))
        except ProviderError as e:
            return self._from_error(ref, e, t0)
        except Exception as e:  # noqa: BLE001
            res = Result.make_failed(ref, Reason.provider_error)
            res.attempts = [self._attempt(False, t0, Cost(unit="usd", currency="USD"),
                                          Reason.provider_error, rejections=[type(e).__name__])]
            return res
        return self._success(ref, policy, pt, Provenance.managed_asr, t0)
