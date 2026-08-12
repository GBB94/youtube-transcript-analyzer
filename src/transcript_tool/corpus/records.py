"""CorpusRecord — the canonical layer's schema (RETRIEVAL_DESIGN.md §5).

A superset of the acquisition `Result`, versioned independently and additive over
`schema.py`: it embeds, never mutates, what the pipeline produced. `provenance`
and `acquisition_attempts` are retained so a downstream answer can be traced all
the way back to *how* the transcript was obtained.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from ..schema import Attempt, Language, Outcome, Provenance, Result, Segment

# Bump on any behavioural change to this schema; it is a rebuild input (§14).
CORPUS_SCHEMA_VERSION = "1.0.0"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Chapter(BaseModel):
    start: float
    title: str


class CorpusVideo(BaseModel):
    """VideoRef + discovery enrichment. `id` is the stable key — titles and
    episode numbers are mutable and are never keyed on."""
    platform: str = "youtube"
    id: str
    url: str
    title: Optional[str] = None
    upload_date: Optional[str] = None          # YYYY-MM-DD
    duration_s: Optional[float] = None
    channel_id: Optional[str] = None
    channel_title: Optional[str] = None
    chapters: list[Chapter] = Field(default_factory=list)


def content_hash(segments: list[Segment]) -> str:
    """Over normalized text + timing; drives the §12 re-ingest no-op guard.
    Timing is rounded to milliseconds so float formatting noise can't fake a change."""
    payload = [(round(s.start, 3), round(s.end, 3), s.text) for s in segments]
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


class CorpusRecord(BaseModel):
    corpus_schema_version: str = CORPUS_SCHEMA_VERSION
    source_slug: str
    video: CorpusVideo
    provenance: Provenance                     # carried through, never dropped
    language: Optional[Language] = None
    segments: list[Segment]                    # canonical timing
    speakers: list[Optional[str]] = Field(default_factory=list)
    # ^ R7: parallel to `segments`; empty unless this episode was ingested --diarize.
    raw_cues_ref: Optional[str] = None
    content_hash: str
    pulled_at: datetime = Field(default_factory=_utcnow)
    acquisition_attempts: list[Attempt] = Field(default_factory=list)

    @property
    def diarized(self) -> bool:
        return bool(self.speakers)

    def file_date(self) -> str:
        """Date half of the `YYYY-MM-DD__<video_id>` naming (§4)."""
        return self.video.upload_date or self.pulled_at.date().isoformat()

    def file_stem(self) -> str:
        return f"{self.file_date()}__{self.video.id}"


def record_from_result(result: Result, *, source_slug: str,
                       video: CorpusVideo,
                       speakers: Optional[list[Optional[str]]] = None) -> CorpusRecord:
    """Build the canonical record from a successful acquisition Result. A
    non-success has no transcript to bank and is refused, mirroring the schema's
    own invariant."""
    if result.outcome is not Outcome.success:
        raise ValueError(f"cannot build a CorpusRecord from a {result.outcome.value} result")
    if speakers is not None and len(speakers) != len(result.segments):
        raise ValueError("speakers must parallel segments one-to-one")
    assert result.provenance is not None  # the Result validator guarantees this
    return CorpusRecord(
        source_slug=source_slug,
        video=video,
        provenance=result.provenance,
        language=result.language,
        segments=result.segments,
        speakers=speakers or [],
        raw_cues_ref=result.raw_cues_ref,
        content_hash=content_hash(result.segments),
        acquisition_attempts=result.attempts,
    )
