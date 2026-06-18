"""Forgiving link parsing (docs/UI_SCOPE.md §4). Accepts watch / youtu.be / Shorts /
embed URLs and bare 11-char ids, split on newline / comma / whitespace. Dedupes by
video id PRESERVING pasted order, and reports invalid links individually with a plain
reason. Server-side mirror of the client validation (validate on both sides)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schema import VideoRef
from ..strategies.api_captions import extract_video_id

_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SPLIT = re.compile(r"[\s,]+")
WATCH_URL = "https://www.youtube.com/watch?v={vid}"


@dataclass
class Target:
    raw: str
    video_id: str
    url: str

    def ref(self) -> VideoRef:
        return VideoRef(platform="youtube", id=self.video_id, url=self.url, source="url")


@dataclass
class Invalid:
    raw: str
    reason: str


@dataclass
class ParsedInput:
    valid: list[Target] = field(default_factory=list)
    invalid: list[Invalid] = field(default_factory=list)
    duplicates: int = 0

    def summary(self) -> str:
        parts = [f"{len(self.valid)} valid"]
        if self.duplicates:
            parts.append(f"{self.duplicates} duplicate{'s' if self.duplicates != 1 else ''} removed")
        if self.invalid:
            parts.append(f"{len(self.invalid)} invalid")
        return " · ".join(parts)


def _resolve_id(token: str) -> str | None:
    if _BARE_ID.match(token):
        return token
    if token.startswith("http://") or token.startswith("https://"):
        return extract_video_id(VideoRef(url=token))
    return None


def parse_targets(text: str) -> ParsedInput:
    out = ParsedInput()
    seen: set[str] = set()
    for token in _SPLIT.split((text or "").strip()):
        if not token:
            continue
        vid = _resolve_id(token)
        if vid is None:
            out.invalid.append(Invalid(raw=token, reason="not a YouTube link"))
            continue
        if vid in seen:
            out.duplicates += 1
            continue
        seen.add(vid)
        out.valid.append(Target(raw=token, video_id=vid, url=WATCH_URL.format(vid=vid)))
    return out
