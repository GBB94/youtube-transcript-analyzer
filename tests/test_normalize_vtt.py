from pathlib import Path

from transcript_tool.normalize import normalize_caption_file

FIX = Path(__file__).parent / "fixtures"


def test_rolling_autocaption_dedup():
    raw = (FIX / "rolling_autocaption.vtt").read_text()
    _, text, ref = normalize_caption_file(raw)
    # The rolling overlap ("the quick brown fox", "jumps over", "the lazy dog")
    # must be collapsed into a single clean reading.
    assert text == "the quick brown fox jumps over the lazy dog and then runs away"
    assert ref.startswith("sha256:")


def test_chorus_repetition_preserved():
    raw = (FIX / "chorus_repeat.vtt").read_text()
    _, text, _ = normalize_caption_file(raw)
    # Intentional repetition (a chorus) must survive: dedup only removes rolling
    # prefix-overlap, never distinct repeated lines.
    assert text == "na na na na hey jude na na na na hey jude"
