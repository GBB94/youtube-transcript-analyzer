"""api_captions (Phase 2) — existing caption tracks via youtube-transcript-api.

A caption strategy for `url` refs, gated by EgressPolicy.allow_public_url.
Honors the language preference list; maps the library's errors onto the correct
reasons (content vs. operational vs. config). The client is injectable so the
logic is unit-testable without network.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Optional

from ..normalize import Cue, cues_to_text, dedupe_rolling
from ..policy import Policy
from ..quality import evaluate, rejected, rejection_reasons
from ..schema import (
    Attempt, Cost, Language, Provenance, Reason, Result, Segment, TimestampType, VideoRef,
)

_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def extract_video_id(ref: VideoRef) -> Optional[str]:
    if ref.id and re.fullmatch(r"[A-Za-z0-9_-]{11}", ref.id):
        return ref.id
    if ref.url:
        m = _ID.search(ref.url)
        if m:
            return m.group(1)
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", ref.url):
            return ref.url
    return None


def _default_client():
    from youtube_transcript_api import YouTubeTranscriptApi
    return YouTubeTranscriptApi()


class ApiCaptionsStrategy:
    name = "api_captions"

    def __init__(self, client: Any | None = None):
        self._client = client                     # injected in tests

    @property
    def client(self):
        if self._client is None:
            self._client = _default_client()
        return self._client

    def applicable(self, ref: VideoRef, policy: Policy) -> bool:
        return (
            "api_captions" in policy.enabled_strategies
            and ref.source == "url"
            and policy.egress.allow_public_url          # gated capability
            and extract_video_id(ref) is not None
        )

    async def fetch(self, ref: VideoRef, policy: Policy) -> Result:
        t0 = time.monotonic()
        vid = extract_video_id(ref)
        if vid is None:
            return self._failed(ref, Reason.invalid_input, t0)

        try:
            tlist = self.client.list(vid)
            transcript = tlist.find_transcript(list(policy.languages))
            fetched = transcript.fetch()
        except Exception as e:  # noqa: BLE001 — map library errors to reasons
            return self._map_error(ref, e, t0)

        snippets = list(fetched)
        if not snippets:
            return self._unavailable(ref, Reason.captions_unavailable, t0)

        cues = [Cue(start=float(s.start), end=float(s.start) + float(getattr(s, "duration", 0.0)),
                    text=str(s.text)) for s in snippets]
        cues = dedupe_rolling(cues)
        text = cues_to_text(cues)
        if not text.strip():
            return self._unavailable(ref, Reason.captions_unavailable, t0)

        gates = evaluate(cues, text, policy.quality)
        if rejected(gates):
            res = self._unavailable(ref, Reason.no_acceptable_transcript, t0)
            res.quality = gates
            res.attempts[0].quality_rejections = rejection_reasons(gates)
            return res

        is_generated = bool(getattr(transcript, "is_generated", False))
        track_lang = getattr(transcript, "language_code", None)
        raw_ref = "sha256:" + hashlib.sha256(
            json.dumps([(c.start, c.end, c.text) for c in cues]).encode()).hexdigest()

        result = Result.make_success(
            ref,
            provenance=Provenance.platform_auto if is_generated else Provenance.human_caption,
            text=text,
            segments=[Segment(start=c.start, end=c.end, text=c.text) for c in cues],
            language=Language(
                requested=list(policy.languages),
                selected=track_lang,
                track_language=track_lang,
                detection_method=None,            # never *infer* translation
            ),
            timestamp_type=TimestampType.caption_cue,
            raw_text=text,
            raw_cues_ref=raw_ref,
            track_id=track_lang,
            duration_seconds=cues[-1].end if cues else 0.0,
            quality=gates,
        )
        result.attempts = [self._attempt(True, t0)]
        return result

    # --- error mapping -------------------------------------------------------

    def _map_error(self, ref: VideoRef, e: Exception, t0: float) -> Result:
        name = type(e).__name__
        # content-level (unavailable)
        if name == "TranscriptsDisabled":
            return self._unavailable(ref, Reason.captions_unavailable, t0)
        if name in ("NoTranscriptFound", "TranslationLanguageNotAvailable", "NotTranslatable"):
            return self._unavailable(ref, Reason.language_unavailable, t0)
        if name == "AgeRestricted":
            return self._unavailable(ref, Reason.age_restricted, t0)
        if name in ("VideoUnavailable",):
            return self._unavailable(ref, Reason.removed, t0)
        if name in ("VideoUnplayable",):
            return self._unavailable(ref, Reason.unsupported, t0)
        # operational (failed) — retry-eligible / config
        if name in ("IpBlocked", "RequestBlocked"):
            return self._failed(ref, Reason.access_challenge, t0)
        if name == "PoTokenRequired":
            return self._failed(ref, Reason.po_token_rejected, t0)
        if name == "InvalidVideoId":
            return self._failed(ref, Reason.invalid_input, t0)
        return self._failed(ref, Reason.provider_error, t0, detail=name)

    # --- helpers -------------------------------------------------------------

    def _attempt(self, ok: bool, t0: float, reason: Reason | None = None) -> Attempt:
        return Attempt(strategy=self.name, ok=ok, reason=reason,
                       latency_ms=int((time.monotonic() - t0) * 1000),
                       cost=Cost(amount=0.0, unit="none", estimated=False))

    def _failed(self, ref, reason, t0, detail=None):
        res = Result.make_failed(ref, reason)
        res.attempts = [self._attempt(False, t0, reason)]
        if detail:
            res.attempts[0].quality_rejections = [detail]
        return res

    def _unavailable(self, ref, reason, t0):
        res = Result.make_unavailable(ref, reason)
        res.attempts = [self._attempt(False, t0, reason)]
        return res
