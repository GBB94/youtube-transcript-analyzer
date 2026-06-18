"""Markdown assembler (docs/UI_SCOPE.md §9). One file, pasted order: YAML front-matter
summary, a section per successful video with readable paragraphs and clickable timestamp
links every ~45s, then a section listing items that could not be transcribed."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..schema import Outcome, Result
from .copy import badge_for, message_for
from .parse import Target

TIMESTAMP_WINDOW_SECONDS = 45


@dataclass
class Record:
    target: Target
    result: Optional[Result]      # None => not run / errored before producing a Result


def _mmss(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _ts_url(base_url: str, seconds: float) -> str:
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}t={int(seconds)}s"


def _transcript_body(result: Result, base_url: str) -> str:
    """Group segments into ~45s windows; each window is a paragraph led by a clickable
    timestamp. Cue-by-cue timing is intentionally not the default (unreadable)."""
    if not result.segments:
        return result.text or ""
    paras: list[str] = []
    window_start = result.segments[0].start
    buf: list[str] = []
    for seg in result.segments:
        if buf and seg.start - window_start >= TIMESTAMP_WINDOW_SECONDS:
            link = f"[{_mmss(window_start)}]({_ts_url(base_url, window_start)})"
            paras.append(f"{link} {' '.join(buf).strip()}")
            buf, window_start = [], seg.start
        buf.append(seg.text)
    if buf:
        link = f"[{_mmss(window_start)}]({_ts_url(base_url, window_start)})"
        paras.append(f"{link} {' '.join(buf).strip()}")
    return "\n\n".join(paras)


def _video_section(rec: Record) -> str:
    r = rec.result
    title = f"Video {rec.target.video_id}"
    lines = [f"## {title}",
             f"- Source: [YouTube]({rec.target.url})",
             f"- Provenance: {badge_for(r)}"]
    if r.language and r.language.selected:
        lines.append(f"- Language: {r.language.selected}")
    if r.duration_seconds:
        lines.append(f"- Duration: {_mmss(r.duration_seconds)}")
    lines.append("\n### Transcript\n" + _transcript_body(r, rec.target.url))
    return "\n".join(lines)


def render(records: list[Record], language_preferences: list[str],
           generated_at: str) -> str:
    succeeded = [rec for rec in records if rec.result and rec.result.outcome is Outcome.success]
    failed = [rec for rec in records if not (rec.result and rec.result.outcome is Outcome.success)]

    fm = [
        "---",
        f"generated_at: {generated_at}",
        f"videos_requested: {len(records)}",
        f"videos_succeeded: {len(succeeded)}",
        f"videos_failed: {len(failed)}",
        f"language_preferences: [{', '.join(language_preferences)}]",
        "---",
        "",
        "# Video Transcripts",
    ]
    parts = ["\n".join(fm)]
    for rec in succeeded:
        parts.append(_video_section(rec))

    if failed:
        lines = ["## Items that could not be transcribed"]
        for rec in failed:
            msg = message_for(rec.result) if rec.result else "Did not run."
            lines.append(f"- {rec.target.url}\n  - Reason: {msg}")
        parts.append("\n".join(lines))

    return "\n\n---\n\n".join(parts) + "\n"
