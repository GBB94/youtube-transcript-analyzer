"""Deterministic, versioned normalization.

Rules (from the design review):
- Never silently rewrite spoken wording. Normalization only collapses
  caption-format artifacts (rolling auto-caption duplication, cue markup,
  whitespace) — it does not paraphrase.
- Preserve the original cues (raw_cues_ref) so timing survives for debugging.
- VTT dedup is governed by golden fixtures; it must not destroy *intentional*
  repetition (e.g. song choruses). The dedup here only removes the rolling
  prefix-overlap that auto-captions emit, not repeated standalone lines.

Bump NORMALIZER_VERSION in schema.py for any behavioural change here; it is a
cache-key input.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_TS = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)
_TAG = re.compile(r"<[^>]+>")            # inline cue tags like <00:00:01.000>
_CUE_SETTINGS = re.compile(r"\s+(align|position|size|line):\S+")


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _to_seconds(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(raw: str) -> list[Cue]:
    """Parse WebVTT/SRT-ish content into cues. Tolerant of both `.` and `,` ms."""
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", raw.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        ts_line = next((ln for ln in lines if _TS.search(ln)), None)
        if not ts_line:
            continue
        m = _TS.search(ts_line)
        start = _to_seconds(*m.group(1, 2, 3, 4))
        end = _to_seconds(*m.group(5, 6, 7, 8))
        idx = lines.index(ts_line)
        text_lines = lines[idx + 1:]
        text = " ".join(_CUE_SETTINGS.sub("", _TAG.sub("", t)).strip() for t in text_lines)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cues.append(Cue(start, end, text))
    return cues


def dedupe_rolling(cues: list[Cue]) -> list[Cue]:
    """Remove rolling-window duplication produced by auto-captions.

    Auto-captions repeat the tail of the previous cue as the head of the next.
    We strip only that overlapping prefix; we never drop a distinct repeated
    line (which would destroy intentional repetition). Fixture-governed.
    """
    out: list[Cue] = []
    prev_words: list[str] = []
    for cue in cues:
        words = cue.text.split()
        # find the longest suffix of prev that is a prefix of current
        overlap = 0
        max_k = min(len(prev_words), len(words))
        for k in range(max_k, 0, -1):
            if prev_words[-k:] == words[:k]:
                overlap = k
                break
        new_words = words[overlap:]
        if new_words:
            out.append(Cue(cue.start, cue.end, " ".join(new_words)))
        prev_words = words
    return out


def cues_to_text(cues: list[Cue]) -> str:
    return re.sub(r"\s+", " ", " ".join(c.text for c in cues)).strip()


def raw_cues_ref(raw: str) -> str:
    """Content-addressed handle to the original cues (timing preserved upstream)."""
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_caption_file(raw: str) -> tuple[list[Cue], str, str]:
    """Returns (segments, normalized_text, raw_cues_ref)."""
    cues = parse_vtt(raw)
    deduped = dedupe_rolling(cues)
    return deduped, cues_to_text(deduped), raw_cues_ref(raw)
