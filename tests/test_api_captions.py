import asyncio

from transcript_tool.policy import EgressPolicy, Policy
from transcript_tool.schema import Outcome, Provenance, Reason, VideoRef
from transcript_tool.strategies.api_captions import ApiCaptionsStrategy


# --- fakes mimicking youtube-transcript-api -----------------------------------

class Snip:
    def __init__(self, text, start, duration=2.0):
        self.text, self.start, self.duration = text, start, duration


class FakeTranscript:
    def __init__(self, lang="en", generated=False, snips=None):
        self.language_code, self.is_generated = lang, generated
        self._snips = snips or [Snip("hello world", 0.0), Snip("second line", 2.0)]

    def fetch(self):
        return list(self._snips)


class TranscriptsDisabled(Exception): ...
class NoTranscriptFound(Exception): ...
class IpBlocked(Exception): ...


class FakeList:
    def __init__(self, transcript=None, raise_find=None):
        self._t, self._raise_find = transcript, raise_find

    def find_transcript(self, langs):
        if self._raise_find:
            raise self._raise_find()
        return self._t


class FakeClient:
    def __init__(self, *, tlist=None, raise_list=None):
        self._tlist, self._raise_list = tlist, raise_list

    def list(self, vid):
        if self._raise_list:
            raise self._raise_list()
        return self._tlist


def _policy():
    return Policy(enabled_strategies=("api_captions",),
                  egress=EgressPolicy(allow_public_url=True))


def _ref():
    return VideoRef(platform="youtube", source="url",
                    url="https://www.youtube.com/watch?v=abcdefghijk")


def test_gated_off_by_default():
    s = ApiCaptionsStrategy(client=FakeClient())
    # public-url disabled => not applicable
    assert s.applicable(_ref(), Policy(enabled_strategies=("api_captions",))) is False


def test_success_manual_caption():
    client = FakeClient(tlist=FakeList(FakeTranscript(generated=False)))
    s = ApiCaptionsStrategy(client=client)
    res = asyncio.run(s.fetch(_ref(), _policy()))
    assert res.outcome is Outcome.success
    assert res.provenance is Provenance.human_caption
    assert "hello world" in res.text
    assert res.language.selected == "en"


def test_success_auto_caption_is_platform_auto():
    client = FakeClient(tlist=FakeList(FakeTranscript(generated=True)))
    res = asyncio.run(ApiCaptionsStrategy(client=client).fetch(_ref(), _policy()))
    assert res.provenance is Provenance.platform_auto


def test_captions_disabled_maps_unavailable():
    client = FakeClient(raise_list=TranscriptsDisabled)
    res = asyncio.run(ApiCaptionsStrategy(client=client).fetch(_ref(), _policy()))
    assert res.outcome is Outcome.unavailable
    assert res.reason is Reason.captions_unavailable


def test_language_unavailable():
    client = FakeClient(tlist=FakeList(raise_find=NoTranscriptFound))
    res = asyncio.run(ApiCaptionsStrategy(client=client).fetch(_ref(), _policy()))
    assert res.outcome is Outcome.unavailable
    assert res.reason is Reason.language_unavailable


def test_ip_block_is_failed_access_challenge():
    client = FakeClient(raise_list=IpBlocked)
    res = asyncio.run(ApiCaptionsStrategy(client=client).fetch(_ref(), _policy()))
    assert res.outcome is Outcome.failed
    assert res.reason is Reason.access_challenge
