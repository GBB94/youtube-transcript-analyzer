"""On-demand diarization (R7, §3/§18) — `corpus add --diarize`.

Speaker attribution is a per-episode OPT-IN, never an automatic cost on every
pull: the standard path stays captions-only and fast, and only the episode you
chose routes through audio -> ASR -> speaker segmentation. The result is a
mixed corpus — most episodes caption-only (`speakers` empty), the chosen few
carrying labels — all queried by the same retrieval layer. ChunkMeta.speaker
fills only for diarized episodes; `transcript ask --speaker` filters on it.

A --diarize ingest of an already-banked caption episode SUPERSEDES it (the ASR
transcript with speakers replaces the caption one; derivation stamps reset), so
"decipher this one podcast" is one flag, not a project."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Callable, Optional, Protocol

from ..cache import Cache
from ..media import MediaError, acquire_audio
from ..orchestrator import get_transcript
from ..policy import Policy
from ..schema import Outcome, Reason, Segment, VideoRef
from .ingest import IngestReport, PullFn, _render_markdown
from .records import CorpusVideo, record_from_result
from .store import CorpusStore


class Diarizer(Protocol):
    """One speaker label per segment (None where attribution is unclear)."""

    def assign_speakers(self, audio_path: str,
                        segments: list[Segment]) -> list[Optional[str]]: ...


class PyannoteDiarizer:
    """Default diarizer (lazy pyannote.audio; the model is provisioned out of
    band and needs a HF token once). Labels each ASR segment with the diarization
    turn covering its midpoint."""

    PIPELINE = "pyannote/speaker-diarization-3.1"

    def __init__(self, pipeline_name: str = PIPELINE):
        self.pipeline_name = pipeline_name
        self._pipeline = None

    def _load(self):
        if self._pipeline is None:
            try:
                from pyannote.audio import Pipeline  # lazy — optional extra
            except ImportError as e:
                raise RuntimeError("diarization needs the 'diarize' extra "
                                   "(pip install '.[diarize]')") from e
            self._pipeline = Pipeline.from_pretrained(self.pipeline_name)
        return self._pipeline

    def assign_speakers(self, audio_path: str,
                        segments: list[Segment]) -> list[Optional[str]]:
        diarization = self._load()(audio_path)
        turns = [(turn.start, turn.end, speaker)
                 for turn, _, speaker in diarization.itertracks(yield_label=True)]
        labels: list[Optional[str]] = []
        for seg in segments:
            mid = (seg.start + seg.end) / 2
            label = None
            for start, end, speaker in turns:
                if start <= mid <= end:
                    label = speaker
                    break
            labels.append(label)
        return labels


AcquireAudio = Callable[[VideoRef, Policy], str]


def _asr_policy(policy: Policy) -> Policy:
    """Diarization needs word timing from OUR ASR run over the same audio the
    diarizer hears — captions can't be aligned to speaker turns."""
    return Policy(mode="asr-only", languages=policy.languages,
                  enabled_strategies=("local_whisper",),
                  quality=policy.quality, egress=policy.egress)


async def _diarize_add(store: CorpusStore, slug: str, refs: list[VideoRef], *,
                       policy: Policy, cache: Optional[Cache],
                       meta: Optional[dict[str, CorpusVideo]],
                       force: bool, diarizer: Diarizer,
                       pull: Optional[PullFn],
                       audio_fn: AcquireAudio,
                       log: Callable[[str], None]) -> IngestReport:
    pull = pull or get_transcript
    meta = meta or {}
    report = IngestReport()
    asr_policy = _asr_policy(policy)

    for ref in refs:
        video_id = ref.id or ""
        if not video_id:
            report.failed.append({"target": ref.url or str(ref.path), "outcome": "failed",
                                  "reason": "invalid_input"})
            continue
        existing = store.entry(slug, video_id)
        if existing and existing.get("diarized") and not force:
            report.skipped_existing.append(video_id)     # already deciphered
            continue

        log(f"corpus add --diarize: {video_id} (audio -> ASR -> speakers)")
        try:
            audio_path = audio_fn(ref, policy)
        except MediaError as e:
            report.failed.append({"target": video_id, "outcome": "failed",
                                  "reason": e.reason.value})
            continue

        try:
            audio_ref = VideoRef(platform=ref.platform, id=video_id, url=ref.url,
                                 source="uploaded_file", path=audio_path)
            result = await pull(audio_ref, asr_policy, cache)
            if result.outcome is not Outcome.success:
                report.failed.append({
                    "target": video_id, "outcome": result.outcome.value,
                    "reason": result.reason.value if result.reason else None})
                continue
            try:
                speakers = diarizer.assign_speakers(audio_path, result.segments)
            except Exception as e:
                report.failed.append({"target": video_id, "outcome": "failed",
                                      "reason": f"diarization_error: {e}"})
                continue
            if len(speakers) != len(result.segments):
                report.failed.append({"target": video_id, "outcome": "failed",
                                      "reason": "diarization_error: label count mismatch"})
                continue

            video = meta.get(video_id) or CorpusVideo(
                id=video_id, url=ref.url or f"https://www.youtube.com/watch?v={video_id}")
            record = record_from_result(result, source_slug=slug, video=video,
                                        speakers=speakers)
            store.add_record(record, markdown=_render_markdown(ref, result))
            (report.superseded if existing else report.pulled).append(video_id)
        finally:
            # ASR audio is GBs across a channel; never keep it past the ingest.
            shutil.rmtree(Path(audio_path).parent, ignore_errors=True)

    return report


def diarize_ingest(store: CorpusStore, slug: str, refs: list[VideoRef], *,
                   policy: Policy, cache: Optional[Cache] = None,
                   meta: Optional[dict[str, CorpusVideo]] = None,
                   force: bool = False,
                   diarizer: Optional[Diarizer] = None,
                   pull: Optional[PullFn] = None,
                   audio_fn: Optional[AcquireAudio] = None,
                   log: Callable[[str], None] = lambda _msg: None) -> IngestReport:
    if not policy.egress.allow_public_url and audio_fn is None:
        # Mirrors the pull gate: --diarize downloads audio from public URLs.
        raise MediaError(Reason.missing_dependency)
    return asyncio.run(_diarize_add(
        store, slug, refs, policy=policy, cache=cache, meta=meta, force=force,
        diarizer=diarizer or PyannoteDiarizer(), pull=pull,
        audio_fn=audio_fn or acquire_audio, log=log))
