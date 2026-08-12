"""Chunking — segment-aware, boundary-respecting, timestamp-preserving (§6).

Deterministic and versioned, exactly as normalization is for the acquisition
half: the same CorpusRecord and config always yield byte-identical chunks, and
any behavioural change bumps CHUNKER_VERSION (a rebuild input, §14).

Boundaries in priority order: published chapter marks (hard), utterance/sentence
ends from segment timing, token budget as the fallback cap. Never a blind split
at a fixed token count when a natural boundary is within reach. Overlap between
consecutive chunks prevents the context-cliff failure where an answer straddles
a boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from pydantic import BaseModel

from ..corpus.records import CorpusRecord

CHUNKER_VERSION = "1.0.0"

TokenCounter = Callable[[str], int]

_SENTENCE_ENDS = (".", "!", "?", "…")
_PAUSE_SECONDS = 0.8          # a timing gap this long reads as an utterance break


def approx_token_counter(text: str) -> int:
    """Dependency-free fallback: ~0.75 words/token for English speech."""
    return max(1, round(len(text.split()) / 0.75))


def default_token_counter() -> TokenCounter:
    """tiktoken when present (deterministic for a pinned encoding), else the
    approximation. The chosen counter is named in the build manifest so two
    machines can tell whether their chunk boundaries are comparable."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return lambda text: len(enc.encode(text))
    except ImportError:
        return approx_token_counter


def token_counter_name() -> str:
    try:
        import tiktoken  # noqa: F401
        return "tiktoken:cl100k_base"
    except ImportError:
        return "approx:words/0.75"


@dataclass(frozen=True)
class ChunkConfig:
    """Defaults, not constants — config the eval harness (§13) tunes."""
    max_tokens: int = 600
    min_tokens: int = 300
    overlap_ratio: float = 0.125       # ~10-15% of max_tokens


class Chunk(BaseModel):
    """ChunkMeta (§6) — what makes citations work. start_s/end_s are NEVER
    flattened; they power the &t=<s>s deep links. `text` is verbatim; `context`
    is the R4 blurb and is stored separately so we never cite synthesized text
    as if it were spoken."""
    chunk_id: str                      # video_id + ordinal, stable + rebuildable
    video_id: str
    source_slug: str
    url: str
    title: Optional[str] = None
    upload_date: Optional[str] = None
    start_s: float
    end_s: float
    provenance: str
    speaker: Optional[str] = None      # null unless the episode was ingested --diarize
    text: str
    context: Optional[str] = None
    chunker_version: str = CHUNKER_VERSION
    context_version: Optional[str] = None

    @property
    def ordinal(self) -> int:
        return int(self.chunk_id.rsplit(":", 1)[1])


@dataclass
class _Utterance:
    start: float
    end: float
    text: str
    tokens: int
    speaker: Optional[str]


def _utterances(record: CorpusRecord, count: TokenCounter) -> list[_Utterance]:
    """Merge raw segments into utterances: a unit ends at sentence-final
    punctuation, a timing pause, or a speaker change (diarized episodes)."""
    segs = record.segments
    speakers = record.speakers or [None] * len(segs)
    out: list[_Utterance] = []
    buf: list[str] = []
    buf_start = 0.0
    buf_end = 0.0
    buf_speaker: Optional[str] = None

    def flush():
        if buf:
            text = " ".join(buf).strip()
            out.append(_Utterance(buf_start, buf_end, text, count(text), buf_speaker))
            buf.clear()

    for i, seg in enumerate(segs):
        text = seg.text.strip()
        if not text:
            continue
        if not buf:
            buf_start, buf_speaker = seg.start, speakers[i]
        elif speakers[i] != buf_speaker:
            flush()
            buf_start, buf_speaker = seg.start, speakers[i]
        buf.append(text)
        buf_end = seg.end
        next_gap = (segs[i + 1].start - seg.end) if i + 1 < len(segs) else 0.0
        if text.rstrip('"”’)').endswith(_SENTENCE_ENDS) or next_gap >= _PAUSE_SECONDS:
            flush()
    flush()
    return out


def _chapter_starts(record: CorpusRecord) -> list[float]:
    return sorted(c.start for c in record.video.chapters)


def _split_oversize(utt: _Utterance, count: TokenCounter, max_tokens: int) -> list[_Utterance]:
    """A single utterance over budget is split by words, timing interpolated
    linearly — the fallback cap, only reached when no natural boundary exists."""
    words = utt.text.split()
    if utt.tokens <= max_tokens or len(words) < 2:
        return [utt]
    pieces: list[_Utterance] = []
    step = max(1, len(words) * max_tokens // max(utt.tokens, 1))
    span = utt.end - utt.start
    for j in range(0, len(words), step):
        part = " ".join(words[j:j + step])
        frac0, frac1 = j / len(words), min(1.0, (j + step) / len(words))
        pieces.append(_Utterance(utt.start + span * frac0, utt.start + span * frac1,
                                 part, count(part), utt.speaker))
    return pieces


def chunk_record(record: CorpusRecord, config: ChunkConfig = ChunkConfig(),
                 token_counter: Optional[TokenCounter] = None) -> list[Chunk]:
    count = token_counter or default_token_counter()
    utts: list[_Utterance] = []
    for u in _utterances(record, count):
        utts.extend(_split_oversize(u, count, config.max_tokens))
    if not utts:
        return []

    chapters = _chapter_starts(record)
    overlap_budget = int(config.max_tokens * config.overlap_ratio)

    groups: list[list[_Utterance]] = []
    chapter_barriers: set[int] = set()     # group indexes whose END is a chapter mark
    current: list[_Utterance] = []
    current_tokens = 0

    def close(next_chapter: bool):
        nonlocal current, current_tokens
        if not current:
            return
        groups.append(current)
        if next_chapter:
            chapter_barriers.add(len(groups) - 1)
            current, current_tokens = [], 0
            return
        # Seed the next chunk with trailing overlap (never more than half of it).
        tail: list[_Utterance] = []
        tokens = 0
        for u in reversed(current):
            if tokens + u.tokens > overlap_budget or len(tail) + 1 > len(current) // 2:
                break
            tail.insert(0, u)
            tokens += u.tokens
        current, current_tokens = list(tail), tokens

    chapter_i = 0
    for utt in utts:
        # A published chapter mark is a hard boundary: close out before crossing it.
        while chapter_i < len(chapters) and utt.start >= chapters[chapter_i]:
            if current and chapters[chapter_i] > current[0].start:
                close(next_chapter=True)
            chapter_i += 1
        if current_tokens + utt.tokens > config.max_tokens and current:
            close(next_chapter=False)
        current.append(utt)
        current_tokens += utt.tokens
    if current:
        groups.append(current)

    # An under-minimum trailing group (video end or just before a chapter mark)
    # merges back into its predecessor when no chapter boundary intervenes —
    # tiny chunks retrieve poorly. Overlap-shared utterances dedupe by identity.
    merged: list[list[_Utterance]] = []
    merged_barriers: set[int] = set()
    for i, group in enumerate(groups):
        tokens = sum(u.tokens for u in group)
        boundary_ok = merged and (len(merged) - 1) not in merged_barriers
        # A merged chunk may exceed max_tokens by at most min_tokens: a slightly
        # long chunk retrieves better than a tiny one.
        fits = boundary_ok and (tokens + sum(u.tokens for u in merged[-1])
                                <= config.max_tokens + config.min_tokens)
        if tokens < config.min_tokens and fits:
            prev = merged[-1]
            seen = set(map(id, prev))
            prev.extend(u for u in group if id(u) not in seen)
        else:
            merged.append(list(group))
        if i in chapter_barriers:
            merged_barriers.add(len(merged) - 1)
    groups = merged

    chunks: list[Chunk] = []
    for ordinal, group in enumerate(groups):
        speakers = {u.speaker for u in group}
        chunks.append(Chunk(
            chunk_id=f"{record.video.id}:{ordinal:04d}",
            video_id=record.video.id,
            source_slug=record.source_slug,
            url=record.video.url,
            title=record.video.title,
            upload_date=record.video.upload_date,
            start_s=group[0].start,
            end_s=group[-1].end,
            provenance=record.provenance.value,
            speaker=next(iter(speakers)) if len(speakers) == 1 else None,
            text=" ".join(u.text for u in group),
        ))
    return chunks
