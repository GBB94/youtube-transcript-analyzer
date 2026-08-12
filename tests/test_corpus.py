"""R0 — canonical corpus store (RETRIEVAL_DESIGN.md §4, §5, §12).

Ingest runs with an injected pull so no test touches the network. Covers: the
CorpusRecord contract, date__id naming, manifest ledger fields, only-new
incrementality, the content-hash no-op guard, and supersede-resets-derivation."""
import asyncio
import json

import pytest

from transcript_tool.corpus.ingest import corpus_add
from transcript_tool.corpus.records import (
    CORPUS_SCHEMA_VERSION, CorpusVideo, content_hash, record_from_result,
)
from transcript_tool.corpus.store import CorpusStore
from transcript_tool.policy import Policy
from transcript_tool.schema import (
    Language, Provenance, Reason, Result, Segment, TimestampType, VideoRef,
)

VID_A, VID_B = "aaaaaaaaaaa", "bbbbbbbbbbb"


def _ref(vid):
    return VideoRef(platform="youtube", id=vid,
                    url=f"https://www.youtube.com/watch?v={vid}", source="url")


def _success(vid, text="hello world", start=0.0):
    segs = [Segment(start=start, end=start + 2.0, text=text),
            Segment(start=start + 2.0, end=start + 4.0, text=text + " again")]
    return Result.make_success(
        _ref(vid), provenance=Provenance.platform_auto,
        text=" ".join(s.text for s in segs), segments=segs,
        language=Language(requested=["en"], selected="en"),
        timestamp_type=TimestampType.caption_cue, raw_text=text,
        raw_cues_ref="sha256:feed", duration_seconds=4.0)


def _video(vid, upload_date="2026-07-14"):
    return CorpusVideo(id=vid, url=f"https://www.youtube.com/watch?v={vid}",
                       title=f"EP {vid}", upload_date=upload_date)


def _add(store, slug, refs, *, results, meta=None, force=False):
    async def pull(ref, policy, cache):
        return results[ref.id]
    return asyncio.run(corpus_add(store, slug, refs, policy=Policy(), meta=meta,
                                  force=force, pull=pull))


# --- record contract ---------------------------------------------------------

def test_content_hash_covers_text_and_timing_with_ms_rounding():
    a = [Segment(start=0.0, end=1.0, text="x")]
    same = [Segment(start=0.0000004, end=1.0, text="x")]      # sub-ms float noise
    moved = [Segment(start=0.5, end=1.0, text="x")]
    reworded = [Segment(start=0.0, end=1.0, text="y")]
    assert content_hash(a) == content_hash(same)
    assert content_hash(a) != content_hash(moved)
    assert content_hash(a) != content_hash(reworded)


def test_record_refuses_non_success_and_mismatched_speakers():
    bad = Result.make_unavailable(_ref(VID_A), Reason.captions_unavailable)
    with pytest.raises(ValueError):
        record_from_result(bad, source_slug="s", video=_video(VID_A))
    ok = _success(VID_A)
    with pytest.raises(ValueError):
        record_from_result(ok, source_slug="s", video=_video(VID_A), speakers=["S1"])


def test_record_carries_provenance_and_dates_the_filename(tmp_path):
    rec = record_from_result(_success(VID_A), source_slug="pod", video=_video(VID_A))
    assert rec.provenance is Provenance.platform_auto
    assert rec.corpus_schema_version == CORPUS_SCHEMA_VERSION
    assert rec.file_stem() == f"2026-07-14__{VID_A}"
    assert not rec.diarized


# --- store + ingest ----------------------------------------------------------

def test_ingest_writes_all_three_ledger_layers(tmp_path):
    store = CorpusStore(tmp_path / "corpus")
    report = _add(store, "pod", [_ref(VID_A)], results={VID_A: _success(VID_A)},
                  meta={VID_A: _video(VID_A)})
    assert report.pulled == [VID_A] and not report.failed

    raw = tmp_path / "corpus" / "pod" / "raw" / f"2026-07-14__{VID_A}.json"
    md = tmp_path / "corpus" / "pod" / "markdown" / f"2026-07-14__{VID_A}.md"
    assert raw.exists() and md.exists()
    assert "t=0s" in md.read_text()                        # deep-linked readable layer

    entry = store.entry("pod", VID_A)
    assert entry["provenance"] == "platform_auto"
    assert entry["content_hash"].startswith("sha256:")
    assert entry["indexed_at"] is None and entry["chunker_version"] is None

    rec = store.load_record("pod", VID_A)
    assert [s.text for s in rec.segments] == ["hello world", "hello world again"]


def test_ingest_is_only_new_then_content_hash_noop(tmp_path):
    store = CorpusStore(tmp_path / "corpus")
    results = {VID_A: _success(VID_A), VID_B: _success(VID_B)}
    _add(store, "pod", [_ref(VID_A)], results=results)

    # Second run with both: A is skipped without pulling, B is pulled.
    report = _add(store, "pod", [_ref(VID_A), _ref(VID_B)], results=results)
    assert report.skipped_existing == [VID_A] and report.pulled == [VID_B]

    # Forced re-pull of an unchanged transcript is a no-op (hash guard).
    report = _add(store, "pod", [_ref(VID_A)], results=results, force=True)
    assert report.unchanged == [VID_A] and not report.pulled


def test_changed_transcript_supersedes_and_resets_derivation_stamps(tmp_path):
    store = CorpusStore(tmp_path / "corpus")
    _add(store, "pod", [_ref(VID_A)], results={VID_A: _success(VID_A)},
         meta={VID_A: _video(VID_A)})
    store.update_entry("pod", VID_A, indexed_at="2026-08-01T00:00:00+00:00",
                       chunker_version="1.0.0")

    report = _add(store, "pod", [_ref(VID_A)],
                  results={VID_A: _success(VID_A, text="better caption")},
                  meta={VID_A: _video(VID_A)}, force=True)
    assert report.superseded == [VID_A]
    entry = store.entry("pod", VID_A)
    assert entry["indexed_at"] is None and entry["chunker_version"] is None
    # Still exactly one raw file for the video.
    raws = list((tmp_path / "corpus" / "pod" / "raw").glob("*.json"))
    assert len(raws) == 1


def test_failures_are_reported_not_banked(tmp_path):
    store = CorpusStore(tmp_path / "corpus")
    results = {VID_A: Result.make_unavailable(_ref(VID_A), Reason.captions_unavailable)}
    report = _add(store, "pod", [_ref(VID_A)], results=results)
    assert report.failed == [{"target": VID_A, "outcome": "unavailable",
                              "reason": "captions_unavailable"}]
    assert store.video_ids("pod") == set()


def test_remove_record_cascades(tmp_path):
    store = CorpusStore(tmp_path / "corpus")
    _add(store, "pod", [_ref(VID_A)], results={VID_A: _success(VID_A)},
         meta={VID_A: _video(VID_A)})
    store.remove_record("pod", VID_A)
    assert store.video_ids("pod") == set()
    assert list((tmp_path / "corpus" / "pod" / "raw").glob("*")) == []
    assert list((tmp_path / "corpus" / "pod" / "markdown").glob("*")) == []


def test_status_reports_counts_and_staleness(tmp_path):
    from transcript_tool.corpus.status import corpus_status
    store = CorpusStore(tmp_path / "corpus")
    _add(store, "pod", [_ref(VID_A)], results={VID_A: _success(VID_A)})
    status = corpus_status(store, "pod")
    assert status["videos"] == 1 and status["indexed"] == 0
    assert status["stale_video_ids"] == [VID_A]      # never indexed => stale
    assert status["provenance"] == {"platform_auto": 1}


def test_manifest_roundtrips_as_json(tmp_path):
    store = CorpusStore(tmp_path / "corpus")
    _add(store, "pod", [_ref(VID_A)], results={VID_A: _success(VID_A)})
    manifest = json.loads(store.manifest_path("pod").read_text())
    assert manifest["source_slug"] == "pod" and VID_A in manifest["videos"]
