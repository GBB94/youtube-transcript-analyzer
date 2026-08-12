"""R1 — the deterministic, versioned chunker (RETRIEVAL_DESIGN.md §6).

Tests inject a whitespace token counter so boundaries are fully deterministic and
independent of whether tiktoken is installed."""
from transcript_tool.corpus.records import Chapter, CorpusVideo, content_hash, CorpusRecord
from transcript_tool.retrieval.chunk import (
    CHUNKER_VERSION, Chunk, ChunkConfig, chunk_record,
)
from transcript_tool.schema import Provenance, Segment

VID = "vvvvvvvvvvv"


def _tokens(text: str) -> int:
    return len(text.split())


def _record(segments, chapters=(), speakers=None, provenance=Provenance.platform_auto):
    video = CorpusVideo(id=VID, url=f"https://www.youtube.com/watch?v={VID}",
                        title="EP 1", upload_date="2026-07-14",
                        chapters=[Chapter(start=s, title=t) for s, t in chapters])
    return CorpusRecord(source_slug="pod", video=video, provenance=provenance,
                        segments=segments, speakers=speakers or [],
                        content_hash=content_hash(segments))


def _sentences(n, words_per=10, seconds_per=5.0, start=0.0, prefix="w"):
    """n one-sentence segments, each `words_per` words and `seconds_per` seconds."""
    segs = []
    for i in range(n):
        text = " ".join(f"{prefix}{i}x{j}" for j in range(words_per - 1)) + " end."
        s = start + i * seconds_per
        segs.append(Segment(start=s, end=s + seconds_per, text=text))
    return segs


CFG = ChunkConfig(max_tokens=60, min_tokens=25, overlap_ratio=0.25)


def test_chunks_are_deterministic_and_versioned():
    rec = _record(_sentences(30))
    a = chunk_record(rec, CFG, token_counter=_tokens)
    b = chunk_record(rec, CFG, token_counter=_tokens)
    assert [c.model_dump() for c in a] == [c.model_dump() for c in b]
    assert all(c.chunker_version == CHUNKER_VERSION for c in a)
    assert all(c.context is None and c.context_version is None for c in a)


def test_chunk_ids_are_video_scoped_ordinals():
    chunks = chunk_record(_record(_sentences(30)), CFG, token_counter=_tokens)
    assert [c.chunk_id for c in chunks] == [f"{VID}:{i:04d}" for i in range(len(chunks))]
    assert chunks[0].ordinal == 0 and chunks[1].ordinal == 1


def test_timestamps_survive_to_every_chunk():
    chunks = chunk_record(_record(_sentences(30)), CFG, token_counter=_tokens)
    assert len(chunks) >= 3
    for c in chunks:
        assert c.video_id == VID and 0.0 <= c.start_s < c.end_s <= 150.0
        assert c.provenance == "platform_auto"
    assert chunks[0].start_s == 0.0 and chunks[-1].end_s == 150.0


def test_token_budget_capped_and_overlap_present():
    chunks = chunk_record(_record(_sentences(30)), CFG, token_counter=_tokens)
    for c in chunks:
        assert _tokens(c.text) <= CFG.max_tokens
    # Consecutive chunks share their boundary text (the context-cliff guard).
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.start_s < prev.end_s
        assert nxt.text.split()[0] in prev.text.split()


def test_chapter_marks_are_hard_boundaries_with_no_overlap_across():
    # Chapter starts at 50s (sentence 10 of 30).
    rec = _record(_sentences(30), chapters=[(0.0, "Intro"), (50.0, "Main")])
    chunks = chunk_record(rec, CFG, token_counter=_tokens)
    boundary = [c for c in chunks if c.end_s <= 50.0]
    after = [c for c in chunks if c.start_s >= 50.0]
    assert boundary and after
    assert len(boundary) + len(after) == len(chunks)   # nothing straddles the mark
    assert after[0].start_s == 50.0


def test_sentence_boundaries_beat_blind_token_splits():
    # Every chunk should end at a sentence end (all our utterances end with '.').
    chunks = chunk_record(_record(_sentences(30)), CFG, token_counter=_tokens)
    for c in chunks:
        assert c.text.rstrip().endswith("end.")


def test_oversize_single_utterance_is_split_not_dropped():
    # One 200-word run-on with no sentence end and no pauses.
    words = " ".join(f"w{j}" for j in range(200))
    rec = _record([Segment(start=0.0, end=100.0, text=words)])
    chunks = chunk_record(rec, CFG, token_counter=_tokens)
    assert len(chunks) >= 3
    # Split pieces respect the cap; the under-min last piece may merge back,
    # bounded by max + min (the documented overshoot allowance).
    assert all(_tokens(c.text) <= CFG.max_tokens + CFG.min_tokens for c in chunks)
    # Interpolated timing still covers the span monotonically.
    assert chunks[0].start_s == 0.0 and chunks[-1].end_s == 100.0
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.start_s >= prev.start_s


def test_small_tail_merges_within_chapter_but_never_across():
    # 7 sentences of 10 words: a 60-token chunk would leave an under-min tail,
    # which merges back (bounded overshoot beats a tiny chunk).
    chunks = chunk_record(_record(_sentences(7)), CFG, token_counter=_tokens)
    assert len(chunks) == 1
    assert chunks[0].start_s == 0.0 and chunks[0].end_s == 35.0
    assert _tokens(chunks[0].text) == 70          # deduped, nothing lost
    # With a chapter mark right before the tail, the tail stays its own chunk.
    chunks2 = chunk_record(_record(_sentences(7), chapters=[(30.0, "Late")]),
                           CFG, token_counter=_tokens)
    assert len(chunks2) == 2 and chunks2[-1].start_s >= 30.0


def test_speaker_labels_flow_to_single_speaker_chunks():
    segs = _sentences(4)
    rec = _record(segs, speakers=["S1", "S1", "S2", "S2"])
    chunks = chunk_record(rec, ChunkConfig(max_tokens=20, min_tokens=5), token_counter=_tokens)
    labelled = [c.speaker for c in chunks]
    assert "S1" in labelled and "S2" in labelled
    # A caption-only record yields null speakers (mixed-corpus rule, §3).
    plain = chunk_record(_record(segs), ChunkConfig(max_tokens=20, min_tokens=5),
                         token_counter=_tokens)
    assert all(c.speaker is None for c in plain)


def test_empty_record_yields_no_chunks():
    assert chunk_record(_record([]), CFG, token_counter=_tokens) == []


def test_chunk_meta_is_complete_for_citations():
    c = chunk_record(_record(_sentences(12)), CFG, token_counter=_tokens)[0]
    assert isinstance(c, Chunk)
    assert c.url.endswith(VID) and c.title == "EP 1" and c.upload_date == "2026-07-14"
    assert c.source_slug == "pod"
