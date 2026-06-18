"""UI-2 — worker drains a job with independent failures and stores Markdown sections."""
from transcript_tool.schema import (
    Language, Provenance, Reason, Result, Segment, TimestampType,
)
from transcript_tool.web import markdown as md
from transcript_tool.web.jobs import JobStore, STATUS_COMPLETE, STATUS_FAILED
from transcript_tool.web.parse import Target
from transcript_tool.web.worker import process_job

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


def _ok(target, text):
    return Result.make_success(
        target.ref(), provenance=Provenance.human_caption, text=text,
        segments=[Segment(start=0.0, end=2.0, text=text)],
        language=Language(requested=["en"], selected="en"),
        timestamp_type=TimestampType.caption_cue, raw_text=text, duration_seconds=2.0)


def _fake_pull(target):
    if target.video_id == C:
        return Result.make_unavailable(target.ref(), Reason.captions_unavailable)
    return _ok(target, f"body for {target.video_id}")


def _job(tmp_path):
    store = JobStore(tmp_path / "j.sqlite")
    targets = [Target(v, v, f"https://youtu.be/{v}") for v in (A, B, C)]
    store.create_job("j", "2026-06-18T00:00:00+00:00", ["en"], targets)
    return store


def test_process_job_sets_statuses_independently(tmp_path):
    store = _job(tmp_path)
    process_job(str(tmp_path / "j.sqlite"), "j", pull=_fake_pull)
    items = {i["idx"]: i for i in store.get_items("j")}
    assert items[0]["status"] == STATUS_COMPLETE and items[0]["badge"] == "Human captions"
    assert items[1]["status"] == STATUS_COMPLETE
    assert items[2]["status"] == STATUS_FAILED          # C failed; A,B unaffected
    assert "No captions available" in items[2]["message"]
    assert store.get_job("j")["status"] == STATUS_COMPLETE


def test_one_exception_does_not_block_siblings(tmp_path):
    store = _job(tmp_path)

    def boom(target):
        if target.video_id == B:
            raise RuntimeError("kaboom")
        return _ok(target, "ok")
    process_job(str(tmp_path / "j.sqlite"), "j", pull=boom)
    items = {i["idx"]: i for i in store.get_items("j")}
    assert items[0]["status"] == STATUS_COMPLETE
    assert items[1]["status"] == STATUS_FAILED          # exception -> failed, not a crash
    assert store.counts("j")["finished"] is True


def test_partial_download_assembles_completed_sections(tmp_path):
    store = _job(tmp_path)
    # Process only the first item by claiming + handling it directly.
    item = store.claim_next_queued("j")
    from transcript_tool.web.worker import _process_item
    _process_item(store, "j", item, _fake_pull)

    items = store.get_items("j")
    sections = [i["md_section"] for i in items if i["status"] == STATUS_COMPLETE and i["md_section"]]
    failures = [(i["url"], i["message"] or "") for i in items if i["status"] == STATUS_FAILED]
    doc = md.assemble("2026-06-18T00:00:00+00:00", ["en"], len(items), sections, failures)
    assert "videos_succeeded: 1" in doc and "videos_requested: 3" in doc
    assert f"Video {A}" in doc
