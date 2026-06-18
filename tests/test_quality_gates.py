"""Hard-gate failure path: when every candidate fails a hard gate, the result is
`unavailable / no_acceptable_transcript` and the offending gate is preserved in
`quality` so callers can see *why* nothing was acceptable."""
import asyncio
from pathlib import Path

from transcript_tool import get_transcript, Outcome, Policy, Reason, VideoRef
from transcript_tool.cache import Cache

FIX = Path(__file__).parent / "fixtures"


def test_inverted_timestamps_yield_no_acceptable_transcript(tmp_path):
    ref = VideoRef(platform="local", source="uploaded_file",
                   path=str(FIX / "inverted_timestamps.vtt"))
    res = asyncio.run(get_transcript(ref, Policy(), Cache(tmp_path)))

    assert res.outcome is Outcome.unavailable
    assert res.reason is Reason.no_acceptable_transcript
    # No transcript fields leak onto a non-success result.
    assert res.text is None

    # The hard gate that rejected the candidate is captured for diagnosis.
    timestamps_gate = next((g for g in res.quality if g.name == "timestamps"), None)
    assert timestamps_gate is not None
    assert timestamps_gate.result == "reject"

    # The attempt records the rejection rather than claiming success.
    assert res.attempts and res.attempts[-1].ok is False
    assert "timestamps" in res.attempts[-1].quality_rejections
