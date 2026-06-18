"""UI-2 — durable job store. Survives reopen (refresh/restart), atomic claim, counts."""
from transcript_tool.web.jobs import (
    JobStore, STATUS_COMPLETE, STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING,
)
from transcript_tool.web.parse import Target

A, B = "aaaaaaaaaaa", "bbbbbbbbbbb"


def _targets():
    return [Target(A, A, f"https://youtu.be/{A}"), Target(B, B, f"https://youtu.be/{B}")]


def test_create_and_read_persists_across_reopen(tmp_path):
    db = tmp_path / "jobs.sqlite"
    JobStore(db).create_job("j1", "2026-06-18T00:00:00+00:00", ["en"], _targets())
    # A fresh store instance (simulates a server restart / a different process) sees it.
    store2 = JobStore(db)
    items = store2.get_items("j1")
    assert [i["video_id"] for i in items] == [A, B]
    assert all(i["status"] == STATUS_QUEUED for i in items)
    assert store2.get_job("j1")["languages"] == "en"


def test_claim_is_atomic_and_drains(tmp_path):
    store = JobStore(tmp_path / "j.sqlite")
    store.create_job("j", "t", ["en"], _targets())
    first = store.claim_next_queued("j")
    assert first["idx"] == 0 and first["status"] == STATUS_QUEUED  # returns the pre-claim row
    assert store.get_items("j")[0]["status"] == STATUS_RUNNING      # now marked running
    second = store.claim_next_queued("j")
    assert second["idx"] == 1
    assert store.claim_next_queued("j") is None                    # drained


def test_counts_and_finalize(tmp_path):
    store = JobStore(tmp_path / "j.sqlite")
    store.create_job("j", "t", ["en"], _targets())
    store.complete_item("j", 0, badge="Human captions", words=10, md_section="## Video a")
    assert store.finalize_if_done("j") is False                    # one still queued
    store.fail_item("j", 1, message="No captions available.", retry=False)
    c = store.counts("j")
    assert (c["total"], c["complete"], c["failed"], c["done"], c["finished"]) == (2, 1, 1, 2, True)
    assert store.finalize_if_done("j") is True
    assert store.get_job("j")["status"] == STATUS_COMPLETE
