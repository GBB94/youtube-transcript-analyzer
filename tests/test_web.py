"""UI-1 — paste -> validate -> synchronous pull -> one Markdown file. The pull is
injected so no network is touched. Acceptance: a multi-link paste returns one .md in
pasted order, with failures listed separately and the batch never blocked by one bad
link."""
from fastapi.testclient import TestClient

from transcript_tool.schema import (
    Language, Outcome, Provenance, Reason, Result, Segment, TimestampType, VideoRef,
)
from transcript_tool.web.app import create_app
from transcript_tool.web.markdown import Record, render
from transcript_tool.web.parse import Target, parse_targets

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"   # 11-char ids


def _ok(target: Target, text: str) -> Result:
    segs = [Segment(start=0.0, end=2.0, text=text)]
    return Result.make_success(
        target.ref(), provenance=Provenance.human_caption, text=text, segments=segs,
        language=Language(requested=["en"], selected="en"),
        timestamp_type=TimestampType.caption_cue, raw_text=text, duration_seconds=2.0)


def _fake_pull(target: Target) -> Result:
    if target.video_id == C:
        return Result.make_unavailable(target.ref(), Reason.captions_unavailable)
    return _ok(target, f"transcript for {target.video_id}")


def _client():
    return TestClient(create_app(pull=_fake_pull))


# --- parsing -----------------------------------------------------------------

def test_parse_dedupes_preserving_order_and_flags_invalid():
    parsed = parse_targets(f"{A}\n{B}, {A}\nnot-a-link\nhttps://youtu.be/{C}")
    assert [t.video_id for t in parsed.valid] == [A, B, C]   # order preserved, A deduped
    assert parsed.duplicates == 1
    assert len(parsed.invalid) == 1 and parsed.invalid[0].raw == "not-a-link"
    assert "3 valid" in parsed.summary() and "1 duplicate" in parsed.summary()


def test_parse_recognizes_url_forms():
    text = " ".join([
        f"https://www.youtube.com/watch?v={A}",
        f"https://youtu.be/{B}",
        f"https://www.youtube.com/shorts/{C}",
    ])
    assert [t.video_id for t in parse_targets(text).valid] == [A, B, C]


# --- markdown ----------------------------------------------------------------

def test_markdown_has_frontmatter_order_and_failures_section():
    recs = [
        Record(Target(A, A, f"https://www.youtube.com/watch?v={A}"), _ok(Target(A, A, f"u{A}"), "first body")),
        Record(Target(C, C, f"https://www.youtube.com/watch?v={C}"),
               Result.make_unavailable(VideoRef(source="url", url="u"), Reason.captions_unavailable)),
    ]
    md = render(recs, language_preferences=["en"], generated_at="2026-06-18T00:00:00+00:00")
    assert "videos_requested: 2" in md and "videos_succeeded: 1" in md and "videos_failed: 1" in md
    assert f"## Video {A}" in md
    assert "Items that could not be transcribed" in md
    assert "No captions available" in md


# --- app end-to-end ----------------------------------------------------------

def test_validate_endpoint_returns_summary():
    r = _client().post("/validate", data={"links": f"{A}\nbad link\n{A}"})
    assert r.status_code == 200
    assert "1 valid" in r.text and "1 duplicate" in r.text and "invalid" in r.text


def test_transcribe_returns_results_and_downloadable_md_in_order():
    client = _client()
    r = client.post("/transcribe", data={"links": f"{A}\n{B}\n{C}"})
    assert r.status_code == 200
    assert "2 of 3 complete" in r.text                 # A,B ok; C failed
    assert "Human captions" in r.text
    assert "No captions available" in r.text           # C's failure surfaced, batch not blocked

    # The results page links to the assembled Markdown; fetch and check order + failures.
    import re
    token = re.search(r"/download/([0-9a-f]+)\.md", r.text).group(1)
    md = client.get(f"/download/{token}.md")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    body = md.text
    assert body.index(f"Video {A}") < body.index(f"Video {B}")     # pasted order preserved
    assert "videos_succeeded: 2" in body and "videos_failed: 1" in body


def test_no_valid_links_is_handled():
    r = _client().post("/transcribe", data={"links": "nonsense, also-bad"})
    assert r.status_code == 200 and "No valid YouTube links" in r.text


def test_one_pull_exception_does_not_break_batch():
    def boom(target):
        if target.video_id == B:
            raise RuntimeError("strategy blew up")
        return _ok(target, "ok body")
    client = TestClient(create_app(pull=boom))
    r = client.post("/transcribe", data={"links": f"{A}\n{B}"})
    assert r.status_code == 200
    assert "1 of 2 complete" in r.text                 # A ok, B failed but batch finished
