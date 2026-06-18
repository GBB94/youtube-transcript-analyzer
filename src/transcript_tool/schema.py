"""Core data contracts for the transcript tool.

This module is the single source of truth for the *shape* of everything the
pipeline produces. Other code switches on these types, so changing them is a
hardening event — keep them stable and versioned.

Folds in the full review punch-list:
- discriminated outcome (success | unavailable | failed) with enforced invariants
- complete reason taxonomy (incl. captions_unavailable, language_unavailable,
  no_acceptable_transcript, invalid_input, access_challenge, po_token_rejected)
- availability_scope + explicit retry (never derived from reason alone)
- structured Cost {amount, unit, currency, estimated}
- Language as a preference list + translation provenance with adapter evidence
- raw_cues_ref (original timing) distinct from raw_text
- attempts with latency/retry/cost/provider_request_id/quality_rejections
- cache provenance (a cache hit is labelled, not replayed)
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "3.0.0"
NORMALIZER_VERSION = "1.0.0"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Outcome(str, Enum):
    success = "success"
    unavailable = "unavailable"      # request-level unavailable (not always permanent)
    failed = "failed"                # operational / config / control


class Reason(str, Enum):
    # --- unavailable: typically permanent ---
    private = "private"
    removed = "removed"
    drm = "drm"
    unsupported = "unsupported"
    no_speech = "no_speech"
    captions_unavailable = "captions_unavailable"
    language_unavailable = "language_unavailable"
    no_acceptable_transcript = "no_acceptable_transcript"   # all candidates failed gates
    # --- unavailable: contextual / transient (may change with time or credentials) ---
    members_only = "members_only"
    age_restricted = "age_restricted"
    geoblocked = "geoblocked"
    bot_gated = "bot_gated"
    live = "live"
    access_challenge = "access_challenge"
    # --- failed: operational ---
    rate_limited = "rate_limited"
    timeout = "timeout"
    audio_download_failed = "audio_download_failed"
    provider_error = "provider_error"
    po_token_rejected = "po_token_rejected"
    invalid_input = "invalid_input"
    # --- failed: configuration ---
    missing_js_runtime = "missing_js_runtime"
    missing_po_token_provider = "missing_po_token_provider"
    missing_dependency = "missing_dependency"
    # --- failed: control / limits ---
    cancelled = "cancelled"
    resource_limit_exceeded = "resource_limit_exceeded"


class AvailabilityScope(str, Enum):
    permanent = "permanent"
    contextual = "contextual"
    transient = "transient"


class Provenance(str, Enum):
    human_caption = "human_caption"
    platform_auto = "platform_auto"
    local_asr = "local_asr"
    managed_asr = "managed_asr"
    translated_caption = "translated_caption"   # only with adapter evidence (see Language)


class TimestampType(str, Enum):
    caption_cue = "caption_cue"
    asr_segment = "asr_segment"


ResourceDimension = Literal["cost", "duration", "bytes", "disk", "memory", "runtime"]

# Reasons that are configuration failures: never persistently negative-cached,
# because fixing config must invalidate them (see cache.py).
CONFIG_FAILURE_REASONS = frozenset({
    Reason.missing_js_runtime,
    Reason.missing_po_token_provider,
    Reason.missing_dependency,
})

# Default classification. retry is still set *explicitly* by callers/strategies
# using context (e.g. Retry-After), but this gives a safe default per reason.
_UNAVAILABLE_PERMANENT = {
    Reason.private, Reason.removed, Reason.drm, Reason.unsupported,
    Reason.no_speech, Reason.captions_unavailable, Reason.language_unavailable,
    Reason.no_acceptable_transcript,
}
_UNAVAILABLE_CONTEXTUAL = {
    Reason.members_only, Reason.age_restricted, Reason.geoblocked,
}
_UNAVAILABLE_TRANSIENT = {Reason.live}


def classify_reason(reason: Reason) -> tuple[Outcome, Optional[AvailabilityScope]]:
    """Map a reason to its outcome bucket and a default availability scope.

    NOTE: `private` is *usually* permanent, but can be contextual if the caller
    holds credentials. Strategies may override the scope with better evidence.
    """
    if reason in _UNAVAILABLE_PERMANENT:
        return Outcome.unavailable, AvailabilityScope.permanent
    if reason in _UNAVAILABLE_CONTEXTUAL:
        return Outcome.unavailable, AvailabilityScope.contextual
    if reason in _UNAVAILABLE_TRANSIENT:
        return Outcome.unavailable, AvailabilityScope.transient
    return Outcome.failed, None


class Cost(BaseModel):
    """API units, provider credits, and dollars are NOT interchangeable."""
    amount: float = 0.0
    unit: Literal["api_units", "provider_credits", "usd", "none"] = "none"
    currency: Optional[str] = None     # e.g. "USD" when unit == "usd"
    estimated: bool = True


class Language(BaseModel):
    requested: list[str] = Field(default_factory=list)   # BCP-47 preference list
    selected: Optional[str] = None                       # the track we used
    spoken_detected: Optional[str] = None                # ASR / detector view of spoken language
    track_language: Optional[str] = None                 # what the track claims to be
    # Translation provenance requires *adapter evidence*, never inference from text.
    original_language: Optional[str] = None
    detection_method: Optional[str] = None               # e.g. "provider_flag"; None => undisclosed
    detection_confidence: Optional[float] = None


class Retry(BaseModel):
    eligible: bool = False
    not_before: Optional[datetime] = None
    max_attempts: int = 0


class Segment(BaseModel):
    start: float
    end: float
    text: str


class GateResult(BaseModel):
    name: str
    result: Literal["pass", "warn", "reject"]
    value: Optional[float] = None
    detail: Optional[str] = None


class Attempt(BaseModel):
    strategy: str
    ok: bool
    reason: Optional[Reason] = None
    latency_ms: Optional[int] = None
    retry_count: int = 0
    cost: Cost = Field(default_factory=Cost)
    provider_request_id: Optional[str] = None      # redact in logs (see security.py)
    quality_rejections: list[str] = Field(default_factory=list)


class ModelInfo(BaseModel):
    name: str
    size: Optional[str] = None
    revision: Optional[str] = None
    compute_type: Optional[str] = None


class VideoRef(BaseModel):
    platform: str = "local"
    id: Optional[str] = None
    url: Optional[str] = None
    source: Literal["url", "uploaded_file"] = "url"
    path: Optional[str] = None      # for uploaded_file


class CacheProvenance(BaseModel):
    served_from_cache: bool = False
    cached_at: Optional[datetime] = None
    cache_layer: Optional[Literal["request_result", "artifact"]] = None


class Result(BaseModel):
    """Discriminated outcome. Invariants are enforced below: a non-success result
    can never carry transcript fields, and a success can never carry a reason."""
    outcome: Outcome
    video_ref: VideoRef

    # failure / unavailable
    reason: Optional[Reason] = None
    availability_scope: Optional[AvailabilityScope] = None
    retry: Retry = Field(default_factory=Retry)
    resource_dimension: Optional[ResourceDimension] = None

    # success payload
    provenance: Optional[Provenance] = None
    language: Optional[Language] = None
    track_id: Optional[str] = None
    model: Optional[ModelInfo] = None
    timestamp_type: Optional[TimestampType] = None
    segments: list[Segment] = Field(default_factory=list)
    raw_cues_ref: Optional[str] = None     # handle/hash to original cues + timing
    raw_text: Optional[str] = None
    text: Optional[str] = None
    word_count: int = 0
    duration_seconds: Optional[float] = None
    quality: list[GateResult] = Field(default_factory=list)

    # always present
    normalizer_version: str = NORMALIZER_VERSION
    schema_version: str = SCHEMA_VERSION
    attempts: list[Attempt] = Field(default_factory=list)
    cache: CacheProvenance = Field(default_factory=CacheProvenance)
    fetched_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _enforce_invariants(self) -> "Result":
        if self.outcome is Outcome.success:
            if self.reason is not None:
                raise ValueError("success result must not carry a reason")
            if not self.text or self.provenance is None:
                raise ValueError("success result requires text and provenance")
        else:
            if self.reason is None:
                raise ValueError(f"{self.outcome} result requires a reason")
            if self.text is not None or self.segments:
                raise ValueError("non-success result must not carry transcript fields")
            if self.reason is Reason.resource_limit_exceeded and self.resource_dimension is None:
                raise ValueError("resource_limit_exceeded requires a resource_dimension")
        return self

    # ---- constructors -------------------------------------------------------

    @classmethod
    def make_success(cls, video_ref: VideoRef, *, provenance: Provenance, text: str,
                     segments: list[Segment], language: Language,
                     timestamp_type: TimestampType, raw_text: str,
                     raw_cues_ref: Optional[str] = None, **kw) -> "Result":
        return cls(outcome=Outcome.success, video_ref=video_ref, provenance=provenance,
                   text=text, segments=segments, language=language,
                   timestamp_type=timestamp_type, raw_text=raw_text,
                   raw_cues_ref=raw_cues_ref, word_count=len(text.split()), **kw)

    @classmethod
    def make_unavailable(cls, video_ref: VideoRef, reason: Reason,
                         scope: Optional[AvailabilityScope] = None,
                         retry: Optional[Retry] = None, **kw) -> "Result":
        bucket, default_scope = classify_reason(reason)
        if bucket is not Outcome.unavailable:
            raise ValueError(f"{reason} is not an 'unavailable' reason")
        return cls(outcome=Outcome.unavailable, video_ref=video_ref, reason=reason,
                   availability_scope=scope or default_scope,
                   retry=retry or Retry(), **kw)

    @classmethod
    def make_failed(cls, video_ref: VideoRef, reason: Reason,
                    retry: Optional[Retry] = None,
                    resource_dimension: Optional[ResourceDimension] = None, **kw) -> "Result":
        bucket, _ = classify_reason(reason)
        if bucket is not Outcome.failed:
            raise ValueError(f"{reason} is not a 'failed' reason")
        return cls(outcome=Outcome.failed, video_ref=video_ref, reason=reason,
                   retry=retry or Retry(), resource_dimension=resource_dimension, **kw)
