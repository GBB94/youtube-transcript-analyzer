"""uploaded_caption — the Phase 1 vertical slice.

Operator supplies a caption/subtitle file (.vtt/.srt). This is the
compliance-safe, offline path: no network, no platform access. ASR over
operator-supplied *audio* is a separate strategy (local_whisper, Phase 4).

The operator asserts they have sufficient rights to the file (see DESIGN.md §4);
the tool validates the file but cannot verify licensing.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..normalize import normalize_caption_file
from ..policy import Policy
from ..quality import evaluate, rejected, rejection_reasons
from ..schema import (
    Attempt, Cost, GateResult, Language, Provenance, Reason, Result, Segment,
    TimestampType, VideoRef,
)

CAPTION_SUFFIXES = {".vtt", ".srt"}


class UploadedCaptionStrategy:
    name = "uploaded_caption"

    def applicable(self, ref: VideoRef, policy: Policy) -> bool:
        return (
            "uploaded_caption" in policy.enabled_strategies
            and ref.source == "uploaded_file"
            and ref.path is not None
            and Path(ref.path).suffix.lower() in CAPTION_SUFFIXES
        )

    async def fetch(self, ref: VideoRef, policy: Policy) -> Result:
        t0 = time.monotonic()
        path = Path(ref.path) if ref.path else None

        # Preflight-style authoritative validation -> invalid_input (a hard failure).
        if path is None or not path.exists() or not path.is_file():
            return self._failed(ref, Reason.invalid_input, t0, "file not found")
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:  # authoritative: cannot read the file
            return self._failed(ref, Reason.invalid_input, t0, f"unreadable: {e}")

        segments_raw, text, cues_ref = normalize_caption_file(raw)
        if not text.strip():
            # Parsed but empty -> captions unavailable from this source.
            return self._unavailable(ref, Reason.captions_unavailable, t0)

        gates = evaluate(segments_raw, text, policy.quality)
        attempt = self._attempt(ok=not rejected(gates), t0=t0,
                                quality_rejections=rejection_reasons(gates))
        if rejected(gates):
            # Let the orchestrator decide escalation; report no_acceptable_transcript
            # only if it is the *last* strategy (orchestrator handles that).
            res = self._unavailable(ref, Reason.no_acceptable_transcript, t0)
            res.attempts = [attempt]
            res.quality = gates
            return res

        segments = [Segment(start=c.start, end=c.end, text=c.text) for c in segments_raw]
        # Track language is the operator's declared preference; we cannot detect
        # spoken language from a caption file, and we never *infer* translation.
        lang = Language(
            requested=list(policy.languages),
            selected=(policy.languages[0] if policy.languages else None),
            track_language=(policy.languages[0] if policy.languages else None),
            detection_method=None,   # undisclosed: not a translated-track claim
        )
        result = Result.make_success(
            ref,
            provenance=Provenance.human_caption,   # operator-supplied; assume authored unless told otherwise
            text=text,
            segments=segments,
            language=lang,
            timestamp_type=TimestampType.caption_cue,
            raw_text=text,
            raw_cues_ref=cues_ref,
            duration_seconds=(segments_raw[-1].end if segments_raw else 0.0),
            quality=gates,
        )
        result.attempts = [attempt]
        return result

    # --- helpers -------------------------------------------------------------

    def _attempt(self, ok: bool, t0: float, reason: Reason | None = None,
                 quality_rejections: list[str] | None = None) -> Attempt:
        return Attempt(
            strategy=self.name, ok=ok, reason=reason,
            latency_ms=int((time.monotonic() - t0) * 1000),
            cost=Cost(amount=0.0, unit="none", estimated=False),
            quality_rejections=quality_rejections or [],
        )

    def _failed(self, ref: VideoRef, reason: Reason, t0: float, detail: str) -> Result:
        res = Result.make_failed(ref, reason)
        res.attempts = [self._attempt(ok=False, t0=t0, reason=reason)]
        return res

    def _unavailable(self, ref: VideoRef, reason: Reason, t0: float) -> Result:
        res = Result.make_unavailable(ref, reason)
        res.attempts = [self._attempt(ok=False, t0=t0, reason=reason)]
        return res
