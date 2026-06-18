import asyncio
from pathlib import Path

import pytest

from transcript_tool import get_transcript, Policy, VideoRef, Outcome, Reason
from transcript_tool.cache import Cache
from transcript_tool.schema import Result

FIX = Path(__file__).parent / "fixtures"


def test_success_requires_text_and_forbids_reason():
    with pytest.raises(Exception):
        Result(outcome=Outcome.success, video_ref=VideoRef(), reason=Reason.removed)


def test_unavailable_requires_reason_and_forbids_transcript():
    with pytest.raises(Exception):
        Result(outcome=Outcome.unavailable, video_ref=VideoRef())  # no reason


def test_resource_limit_requires_dimension():
    with pytest.raises(Exception):
        Result.make_failed(VideoRef(), Reason.resource_limit_exceeded)  # missing dimension


def test_uploaded_caption_end_to_end(tmp_path):
    ref = VideoRef(platform="local", source="uploaded_file",
                   path=str(FIX / "rolling_autocaption.vtt"))
    res = asyncio.run(get_transcript(ref, Policy(), Cache(tmp_path)))
    assert res.outcome is Outcome.success
    assert res.provenance.value == "human_caption"
    assert "lazy dog" in res.text
    assert res.attempts and res.attempts[0].strategy == "uploaded_caption"


def test_cache_hit_is_labelled_not_replayed(tmp_path):
    ref = VideoRef(platform="local", source="uploaded_file",
                   path=str(FIX / "rolling_autocaption.vtt"))
    cache = Cache(tmp_path)
    first = asyncio.run(get_transcript(ref, Policy(), cache))
    assert first.cache.served_from_cache is False
    second = asyncio.run(get_transcript(ref, Policy(), cache))
    assert second.cache.served_from_cache is True
    assert second.attempts == []          # not replayed as fresh acquisition
    assert second.text == first.text


def test_missing_file_is_invalid_input(tmp_path):
    ref = VideoRef(platform="local", source="uploaded_file", path=str(tmp_path / "nope.vtt"))
    res = asyncio.run(get_transcript(ref, Policy(), Cache(tmp_path)))
    assert res.outcome is Outcome.failed
    assert res.reason is Reason.invalid_input
