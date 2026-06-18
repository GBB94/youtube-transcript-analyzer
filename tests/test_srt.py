"""SRT-specific coverage. The parser is format-tolerant (comma or dot ms, with or
without numeric index lines), but the design says: prove it, don't assume it."""
import asyncio
from pathlib import Path

from transcript_tool import get_transcript, Outcome, Policy, VideoRef
from transcript_tool.cache import Cache
from transcript_tool.normalize import normalize_caption_file
from transcript_tool.schema import Provenance

FIX = Path(__file__).parent / "fixtures"


def test_srt_index_lines_and_comma_ms_parse():
    raw = (FIX / "basic.srt").read_text()
    segments, text, ref = normalize_caption_file(raw)
    # Numeric index lines ("1", "2", "3") are not mistaken for cue text, and the
    # comma millisecond separator parses the same as VTT's dot.
    assert text == (
        "hello from an srt file with numeric index lines "
        "and comma millisecond separators"
    )
    assert [round(c.start, 3) for c in segments] == [0.0, 2.0, 4.0]
    assert [round(c.end, 3) for c in segments] == [2.0, 4.0, 6.0]
    assert ref.startswith("sha256:")


def test_srt_rolling_overlap_dedup():
    raw = (FIX / "rolling_autocaption.srt").read_text()
    _, text, _ = normalize_caption_file(raw)
    # Same rolling-overlap collapse as the VTT fixture — format must not matter.
    assert text == "the quick brown fox jumps over the lazy dog and then runs away"


def test_srt_end_to_end_uploaded_caption(tmp_path):
    ref = VideoRef(platform="local", source="uploaded_file",
                   path=str(FIX / "basic.srt"))
    res = asyncio.run(get_transcript(ref, Policy(), Cache(tmp_path)))
    assert res.outcome is Outcome.success
    assert res.provenance is Provenance.human_caption
    assert "comma millisecond separators" in res.text
    assert res.attempts and res.attempts[0].strategy == "uploaded_caption"
