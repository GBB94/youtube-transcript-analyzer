"""UI-1/2 web flow. Parsing + Markdown are pure units; the app flow runs with an
injected synchronous worker + fake pull so CI never touches the network. Covers the
UI-2 acceptance: durable job, live SSE stream, refresh survives, independent failures,
partial Markdown download."""
import threading
import time

from fastapi.testclient import TestClient

from transcript_tool.schema import (
    Language, Outcome, Provenance, Reason, Result, Retry, Segment, TimestampType, VideoRef,
)
from transcript_tool.web.app import create_app
from transcript_tool.web.markdown import Record, render
from transcript_tool.web.parse import Target, parse_targets
from transcript_tool.web.worker import make_pull, process_job

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
    return _ok(target, f"transcript for {target.video_id}")


def _client(tmp_path, pull=_fake_pull, delay=0.0):
    """App whose worker runs synchronously in-process with a fake pull. `delay` runs the
    worker in a background thread (for exercising live SSE transitions)."""
    db = str(tmp_path / "jobs.sqlite")

    def run_worker(job_id):
        def work():
            if delay:
                time.sleep(delay)
            process_job(db, job_id, pull=pull)
        if delay:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()
    return TestClient(create_app(db_path=db, run_worker=run_worker))


# --- parsing / markdown units ------------------------------------------------

def test_parse_dedupes_preserving_order_and_flags_invalid():
    parsed = parse_targets(f"{A}\n{B}, {A}\nnot-a-link\nhttps://youtu.be/{C}")
    assert [t.video_id for t in parsed.valid] == [A, B, C]
    assert parsed.duplicates == 1
    assert len(parsed.invalid) == 1
    assert "3 valid" in parsed.summary() and "1 duplicate" in parsed.summary()


def test_markdown_render_has_frontmatter_and_failures():
    recs = [
        Record(Target(A, A, f"https://youtu.be/{A}"), _ok(Target(A, A, f"https://youtu.be/{A}"), "first")),
        Record(Target(C, C, f"https://youtu.be/{C}"),
               Result.make_unavailable(VideoRef(source="url", url="u"), Reason.captions_unavailable)),
    ]
    out = render(recs, ["en"], "2026-06-18T00:00:00+00:00")
    assert "videos_succeeded: 1" in out and "Items that could not be transcribed" in out


# --- app flow ----------------------------------------------------------------

def test_validate_endpoint_returns_summary(tmp_path):
    r = _client(tmp_path).post("/validate", data={"links": f"{A}\nbad link\n{A}"})
    assert r.status_code == 200 and "1 valid" in r.text and "1 duplicate" in r.text


def test_transcribe_creates_job_and_renders_rows(tmp_path):
    client = _client(tmp_path)                     # synchronous worker -> done on submit
    r = client.post("/transcribe", data={"links": f"{A}\n{B}\n{C}"})
    assert r.status_code == 200                    # followed the 303 redirect to /jobs/{id}
    assert "2 of 3 complete" in r.text
    assert "Human captions" in r.text
    assert "No captions available" in r.text       # C failed; batch not blocked


def test_refresh_survives_reads_state_from_store(tmp_path):
    client = _client(tmp_path)
    r = client.post("/transcribe", data={"links": f"{A}\n{C}"})
    job_id = str(r.url).rstrip("/").split("/")[-1]
    again = client.get(f"/jobs/{job_id}")          # a fresh GET re-renders current state
    assert again.status_code == 200 and "1 of 2 complete" in again.text


def test_sse_stream_emits_items_and_done(tmp_path):
    client = _client(tmp_path)
    r = client.post("/transcribe", data={"links": f"{A}\n{C}"})
    job_id = str(r.url).rstrip("/").split("/")[-1]
    body = client.get(f"/jobs/{job_id}/events").text
    assert "event: item" in body and "event: done" in body
    assert "Human captions" in body and "captions_unavailable" not in body  # reason mapped to copy
    assert '"complete": 1' in body and '"failed": 1' in body


def test_live_sse_reflects_worker_progress(tmp_path):
    client = _client(tmp_path, delay=0.2)          # worker runs in a thread
    r = client.post("/transcribe", data={"links": f"{A}\n{B}"})
    job_id = str(r.url).rstrip("/").split("/")[-1]
    body = client.get(f"/jobs/{job_id}/events").text   # blocks until job finishes
    assert body.count("event: item") >= 2
    assert "event: done" in body and '"complete": 2' in body


def test_partial_and_full_markdown_download(tmp_path):
    client = _client(tmp_path)
    r = client.post("/transcribe", data={"links": f"{A}\n{B}\n{C}"})
    job_id = str(r.url).rstrip("/").split("/")[-1]
    md = client.get(f"/jobs/{job_id}/transcripts.md")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    body = md.text
    assert body.index(f"Video {A}") < body.index(f"Video {B}")   # pasted order
    assert "videos_succeeded: 2" in body and "videos_failed: 1" in body


def test_no_valid_links_is_handled(tmp_path):
    r = _client(tmp_path).post("/transcribe", data={"links": "nonsense, also-bad"})
    assert r.status_code == 200 and "No valid YouTube links" in r.text


def test_unknown_job_is_404_for_download(tmp_path):
    assert _client(tmp_path).get("/jobs/deadbeef/transcripts.md").status_code == 404


# --- UI-4: retry / cancel / ASR ----------------------------------------------

def _job_id(resp):
    return str(resp.url).rstrip("/").split("/")[-1]


def test_retry_failed_requeues_and_succeeds_second_time(tmp_path):
    """A retry-eligible failure (rate_limited) can be retried; second attempt succeeds."""
    calls = {}

    def stateful(target):
        calls[target.video_id] = calls.get(target.video_id, 0) + 1
        if target.video_id == C and calls[C] == 1:
            return Result.make_failed(target.ref(), Reason.rate_limited,
                                      retry=Retry(eligible=True, max_attempts=3))
        return _ok(target, f"t {target.video_id}")

    client = _client(tmp_path, pull=stateful)
    r = client.post("/transcribe", data={"links": f"{A}\n{C}"})
    job_id = _job_id(r)
    assert "Retry all failed" in r.text and "Temporary problem" in r.text   # C failed, retryable

    again = client.post(f"/jobs/{job_id}/retry-failed")                      # reprocess
    assert "2 of 2 complete" in again.text                                  # C now succeeded


def test_retry_single_item(tmp_path):
    calls = {}

    def stateful(target):
        calls[target.video_id] = calls.get(target.video_id, 0) + 1
        if target.video_id == C and calls[C] == 1:
            return Result.make_failed(target.ref(), Reason.timeout, retry=Retry(eligible=True))
        return _ok(target, "ok")

    client = _client(tmp_path, pull=stateful)
    job_id = _job_id(client.post("/transcribe", data={"links": f"{A}\n{C}"}))
    # the failed item exposes a retry endpoint at its idx (C is idx 1)
    again = client.post(f"/jobs/{job_id}/items/1/retry")
    assert "2 of 2 complete" in again.text


def test_non_retryable_failure_offers_no_retry(tmp_path):
    # captions_unavailable is not retry-eligible -> no Retry control, retryable count 0
    r = _client(tmp_path).post("/transcribe", data={"links": f"{A}\n{C}"})
    assert "Retry all failed" not in r.text


def test_cancel_reconciles_queued_items(tmp_path):
    cancelled_calls = []
    db = str(tmp_path / "jobs.sqlite")
    # run_worker is a no-op so items stay queued; cancel must reconcile them to cancelled.
    client = TestClient(create_app(db_path=db, run_worker=lambda jid: None,
                                   cancel_worker=lambda jid: cancelled_calls.append(jid)))
    job_id = _job_id(client.post("/transcribe", data={"links": f"{A}\n{B}"}))
    r = client.post(f"/jobs/{job_id}/cancel")
    assert cancelled_calls == [job_id]              # in-flight kill hook invoked
    assert "Cancelled" in r.text
    store = client.app.state.store
    assert store.get_job(job_id)["status"] == "cancelled"
    assert all(i["status"] == "cancelled" for i in store.get_items(job_id))


def test_asr_flag_is_persisted_on_job(tmp_path):
    client = _client(tmp_path)
    with_asr = _job_id(client.post("/transcribe", data={"links": A, "asr": "on"}))
    without = _job_id(client.post("/transcribe", data={"links": B}))
    store = client.app.state.store
    assert store.get_job(with_asr)["asr"] == 1
    assert store.get_job(without)["asr"] == 0


def test_make_pull_appends_local_whisper_only_when_asr(monkeypatch):
    seen = {}

    def fake_get(ref, policy, cache):
        seen["strategies"] = policy.enabled_strategies
        return _ok(Target("x", "x", "u"), "body")

    import transcript_tool.orchestrator as orch
    monkeypatch.setattr(orch, "get_transcript_sync", fake_get)

    make_pull(asr=False)(Target("x", "x", "https://youtu.be/x"))
    assert "local_whisper" not in seen["strategies"]
    make_pull(asr=True)(Target("x", "x", "https://youtu.be/x"))
    assert "local_whisper" in seen["strategies"]
