"""local_whisper (Phase 4) — ASR floor via faster-whisper.

Operates on operator-supplied audio/video (the tested path) and, when the
public-URL capability is enabled, on audio acquired by media.acquire_audio
(live-only path). The transcriber is injectable so segment mapping, no-speech
handling, timeout, and the missing-model contract are unit-testable without a
multi-GB model.

Contracts enforced:
- model loaded lazily from local-only storage (provisioning.load_lazy); a missing
  model => missing_dependency, never a silent download
- separate CPU/GPU semaphores; ASR runs in a worker thread under a wall-clock timeout
- provenance = local_asr; timestamp_type = asr_segment; capture language + no_speech
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

from ..policy import Policy
from ..provisioning import ModelSpec, ModelUnavailable, load_lazy
from ..quality import evaluate, rejected, rejection_reasons
from ..schema import (
    Attempt, Cost, Language, ModelInfo, Provenance, Reason, Result, Segment,
    TimestampType, VideoRef,
)
from ..normalize import raw_cues_ref as _raw_cues_ref

AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".mp4", ".mkv", ".webm", ".mov"}

# Separate semaphores so `--concurrency N` can't launch N heavy ASR jobs blindly.
CPU_SEM = asyncio.Semaphore(1)
GPU_SEM = asyncio.Semaphore(1)
DEFAULT_TIMEOUT_S = 1800


@dataclass
class ASRSegment:
    start: float
    end: float
    text: str
    no_speech_prob: float = 0.0


@dataclass
class ASRResult:
    segments: list[ASRSegment]
    language: Optional[str] = None
    language_probability: Optional[float] = None
    raw_ref: Optional[str] = None
    extra: dict = field(default_factory=dict)


class Transcriber(Protocol):
    def __call__(self, media_path: str, languages: list[str], spec: ModelSpec) -> ASRResult: ...


def faster_whisper_transcriber(media_path: str, languages: list[str], spec: ModelSpec) -> ASRResult:
    """Real transcriber. Lazy-loads the local-only model; never downloads here."""
    model = load_lazy(spec, store_dir=_store_dir())     # raises ModelUnavailable if absent
    lang = languages[0] if languages else None
    segments, info = model.transcribe(media_path, vad_filter=True, language=lang)
    segs = [ASRSegment(start=float(s.start), end=float(s.end), text=str(s.text).strip(),
                       no_speech_prob=float(getattr(s, "no_speech_prob", 0.0)))
            for s in segments]
    return ASRResult(segments=segs, language=getattr(info, "language", None),
                     language_probability=getattr(info, "language_probability", None),
                     raw_ref=_raw_cues_ref(media_path))


def _store_dir() -> str:
    import os
    return os.environ.get("TRANSCRIPT_MODEL_DIR", str(Path.home() / ".cache" / "transcript-tool" / "models"))


class LocalWhisperStrategy:
    name = "local_whisper"

    def __init__(self, transcriber: Optional[Transcriber] = None,
                 spec: Optional[ModelSpec] = None, timeout_s: int = DEFAULT_TIMEOUT_S):
        self.transcriber = transcriber or faster_whisper_transcriber
        self.spec = spec or ModelSpec()
        self.timeout_s = timeout_s

    def applicable(self, ref: VideoRef, policy: Policy) -> bool:
        if "local_whisper" not in policy.enabled_strategies or policy.mode == "captions-only":
            return False
        if ref.source == "uploaded_file" and ref.path:
            return Path(ref.path).suffix.lower() in AUDIO_SUFFIXES
        # URL -> audio acquisition -> ASR is a live-only path, gated like other public-URL work.
        return ref.source == "url" and policy.egress.allow_public_url

    async def fetch(self, ref: VideoRef, policy: Policy) -> Result:
        t0 = time.monotonic()
        media_path = await self._resolve_media(ref, policy)
        if isinstance(media_path, Result):     # acquisition failed -> propagate
            return media_path

        sem = GPU_SEM if self.spec.device == "cuda" else CPU_SEM
        try:
            async with sem:
                asr: ASRResult = await asyncio.wait_for(
                    asyncio.to_thread(self.transcriber, media_path, list(policy.languages), self.spec),
                    timeout=self.timeout_s,
                )
        except asyncio.TimeoutError:
            return self._fail(ref, Reason.timeout, t0)
        except ModelUnavailable:
            return self._fail(ref, Reason.missing_dependency, t0)
        except Exception as e:  # noqa: BLE001
            return self._fail(ref, Reason.provider_error, t0, detail=type(e).__name__)

        # no-speech handling
        speaking = [s for s in asr.segments if s.text and s.no_speech_prob < 0.6]
        if not speaking:
            return self._unavail(ref, Reason.no_speech, t0)

        cues = [Segment(start=s.start, end=s.end, text=s.text) for s in speaking]
        text = " ".join(s.text for s in speaking).strip()
        # reuse the same gate machinery (on Segment-shaped objects)
        gates = evaluate(speaking, text, policy.quality)  # type: ignore[arg-type]
        if rejected(gates):
            res = self._unavail(ref, Reason.no_acceptable_transcript, t0)
            res.quality = gates
            res.attempts[0].quality_rejections = rejection_reasons(gates)
            return res

        result = Result.make_success(
            ref,
            provenance=Provenance.local_asr,
            text=text,
            segments=cues,
            language=Language(
                requested=list(policy.languages),
                selected=asr.language,
                spoken_detected=asr.language,
                detection_method="asr",
                detection_confidence=asr.language_probability,
            ),
            timestamp_type=TimestampType.asr_segment,
            raw_text=text,
            raw_cues_ref=asr.raw_ref,
            model=ModelInfo(name=self.spec.name, size=self.spec.size,
                            revision=self.spec.revision or None, compute_type=self.spec.compute_type),
            duration_seconds=cues[-1].end if cues else 0.0,
            quality=gates,
        )
        result.attempts = [self._attempt(True, t0)]
        return result

    async def _resolve_media(self, ref: VideoRef, policy: Policy):
        if ref.source == "uploaded_file" and ref.path and Path(ref.path).exists():
            return ref.path
        if ref.source == "uploaded_file":
            return self._fail(ref, Reason.invalid_input, time.monotonic())
        # url path (live-only): acquire audio with yt-dlp
        from ..media import acquire_audio, MediaError
        try:
            return await asyncio.to_thread(acquire_audio, ref, policy)
        except MediaError as me:
            return self._fail(ref, me.reason, time.monotonic())

    # --- helpers -------------------------------------------------------------

    def _attempt(self, ok, t0, reason=None):
        return Attempt(strategy=self.name, ok=ok, reason=reason,
                       latency_ms=int((time.monotonic() - t0) * 1000),
                       cost=Cost(amount=0.0, unit="none", estimated=False))

    def _fail(self, ref, reason, t0, detail=None):
        res = Result.make_failed(ref, reason)
        res.attempts = [self._attempt(False, t0, reason)]
        if detail:
            res.attempts[0].quality_rejections = [detail]
        return res

    def _unavail(self, ref, reason, t0):
        res = Result.make_unavailable(ref, reason)
        res.attempts = [self._attempt(False, t0, reason)]
        return res
