import asyncio
from pathlib import Path

from transcript_tool.policy import EgressPolicy, Policy
from transcript_tool.schema import Outcome, Reason, VideoRef
from transcript_tool.strategies.ytdlp_subs import (
    ProcResult, YtdlpSubsStrategy, map_ytdlp_error,
)

VTT = """WEBVTT

00:00:00.000 --> 00:00:02.000
hello from yt-dlp

00:00:02.000 --> 00:00:04.000
second cue
"""


def _policy():
    return Policy(enabled_strategies=("ytdlp_subs",),
                  egress=EgressPolicy(allow_public_url=True))


def _ref():
    return VideoRef(platform="youtube", source="url",
                    url="https://www.youtube.com/watch?v=abcdefghijk")


def make_runner(write_vtt=True, returncode=0, stderr=""):
    def runner(args, workdir):
        if write_vtt:
            (Path(workdir) / "abcdefghijk.en.vtt").write_text(VTT)
        return ProcResult(returncode, "", stderr, Path(workdir))
    return runner


def test_success_parses_vtt():
    s = YtdlpSubsStrategy(runner=make_runner())
    res = asyncio.run(s.fetch(_ref(), _policy()))
    assert res.outcome is Outcome.success
    assert "hello from yt-dlp" in res.text


def test_bot_wall_maps_bot_gated():
    s = YtdlpSubsStrategy(runner=make_runner(
        write_vtt=False, returncode=1, stderr="ERROR: Sign in to confirm you're not a bot"))
    res = asyncio.run(s.fetch(_ref(), _policy()))
    assert res.outcome is Outcome.failed
    assert res.reason is Reason.bot_gated


def test_missing_js_runtime():
    s = YtdlpSubsStrategy(runner=make_runner(
        write_vtt=False, returncode=1, stderr="WARNING: No supported JavaScript runtime could be found"))
    res = asyncio.run(s.fetch(_ref(), _policy()))
    assert res.reason is Reason.missing_js_runtime


def test_no_subs_is_captions_unavailable():
    s = YtdlpSubsStrategy(runner=make_runner(write_vtt=False, returncode=0))
    res = asyncio.run(s.fetch(_ref(), _policy()))
    assert res.outcome is Outcome.unavailable
    assert res.reason is Reason.captions_unavailable


def test_error_map_units():
    assert map_ytdlp_error("HTTP Error 429: Too Many Requests") is Reason.rate_limited
    assert map_ytdlp_error("This video is private") is Reason.members_only
