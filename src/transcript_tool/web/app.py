"""FastAPI app (UI-2). Server-rendered (Jinja + HTMX). Paste -> validate -> a durable
job whose items are transcribed by a SEPARATE worker process, with live per-row updates
streamed over SSE. A refresh re-renders current state from SQLite; one failed item never
blocks the rest; the Markdown download assembles whatever has completed so far.

`run_worker` is injectable so tests drive processing deterministically (in-process with a
fake pull) instead of spawning; the default spawns a child worker process."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import markdown as md
from .jobs import JobStore, STATUS_COMPLETE, STATUS_FAILED
from .parse import parse_targets

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MAX_BATCH = 50
DEFAULT_DB = str(Path("~/.cache/transcript-tool/web-jobs.sqlite").expanduser())

RunWorker = Callable[[str], None]       # (job_id) -> launch processing
CancelWorker = Callable[[str], None]    # (job_id) -> stop in-flight compute


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_payload(item: dict) -> dict:
    ok = item["status"] == STATUS_COMPLETE
    return {"idx": item["idx"], "status": item["status"], "ok": ok,
            "badge": item["badge"], "message": item["message"] or "",
            "retry": bool(item["retry"]), "words": item["words"] or 0, "url": item["url"]}


def create_app(db_path: Optional[str] = None, run_worker: Optional[RunWorker] = None,
               cancel_worker: Optional[CancelWorker] = None) -> FastAPI:
    app = FastAPI(title="Batch Transcripts")
    db_path = db_path or DEFAULT_DB
    Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    store = JobStore(db_path)
    app.state.store = store
    app.state.workers = {}        # job_id -> spawned worker Process (default impl)

    def _spawn(job_id: str) -> None:
        import multiprocessing as mp
        from .worker import process_job
        p = mp.get_context("spawn").Process(target=process_job, args=(db_path, job_id), daemon=True)
        p.start()
        app.state.workers[job_id] = p

    def _terminate(job_id: str) -> None:
        # Real cancel: kill the worker process so in-flight transcription compute stops.
        p = app.state.workers.pop(job_id, None)
        if p is not None and p.is_alive():
            p.terminate()
            p.join(3)
            if p.is_alive():
                p.kill()

    app.state.run_worker = run_worker or _spawn
    app.state.cancel_worker = cancel_worker or _terminate

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return _TEMPLATES.TemplateResponse(request, "index.html", {})

    @app.post("/validate", response_class=HTMLResponse)
    def validate(request: Request, links: str = Form("")):
        return _TEMPLATES.TemplateResponse(request, "_validation.html",
                                           {"parsed": parse_targets(links)})

    def _err(request, message):
        return _TEMPLATES.TemplateResponse(
            request, "job.html", {"error": message, "rows": [], "job_id": None})

    @app.post("/transcribe")
    def transcribe(request: Request, links: str = Form(""), asr: str = Form("")):
        parsed = parse_targets(links)
        if not parsed.valid:
            return _err(request, "No valid YouTube links to transcribe.")
        if len(parsed.valid) > MAX_BATCH:
            return _err(request, f"Keep it to {MAX_BATCH} links per batch.")
        job_id = uuid.uuid4().hex
        store.create_job(job_id, _now_iso(), ["en"], parsed.valid, asr=bool(asr))
        app.state.run_worker(job_id)        # separate worker process picks it up
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    def _job_view(request, job_id):
        items = store.get_items(job_id)         # current state -> refresh survives
        counts = store.counts(job_id)
        job = store.get_job(job_id)
        return _TEMPLATES.TemplateResponse(
            request, "job.html",
            {"error": None, "job_id": job_id, "rows": [_row_payload(i) for i in items],
             "n_ok": counts["complete"], "n_total": counts["total"],
             "finished": counts["finished"], "active": counts["active"],
             "retryable": counts["retryable"], "asr": bool(job and job["asr"])})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        if store.get_job(job_id) is None:
            return _err(request, "Unknown job.")
        return _job_view(request, job_id)

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        if store.get_job(job_id) is not None:
            app.state.cancel_worker(job_id)     # stop in-flight compute
            store.request_cancel(job_id)        # reconcile queued/running -> cancelled
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/items/{idx}/retry")
    def retry_item(job_id: str, idx: int):
        if store.requeue_item(job_id, idx):
            app.state.run_worker(job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/retry-failed")
    def retry_failed(job_id: str):
        if store.requeue_failed(job_id):
            app.state.run_worker(job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}/events")
    def job_events(job_id: str):
        async def gen():
            last: dict[int, tuple] = {}
            while True:
                items = store.get_items(job_id)
                for it in items:
                    sig = (it["status"], it["badge"], it["message"])
                    if last.get(it["idx"]) != sig:
                        last[it["idx"]] = sig
                        yield f"event: item\ndata: {json.dumps(_row_payload(it))}\n\n"
                counts = store.counts(job_id)
                yield f"event: counts\ndata: {json.dumps(counts)}\n\n"
                if counts["finished"]:
                    yield "event: done\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.3)
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/jobs/{job_id}/transcripts.md")
    def download(job_id: str):
        job = store.get_job(job_id)
        if job is None:
            return PlainTextResponse("Unknown job.", status_code=404)
        items = store.get_items(job_id)
        sections = [it["md_section"] for it in items
                    if it["status"] == STATUS_COMPLETE and it["md_section"]]
        failures = [(it["url"], it["message"] or "Did not complete.")
                    for it in items if it["status"] == STATUS_FAILED]
        document = md.assemble(job["created_at"], job["languages"].split(","),
                               len(items), sections, failures)
        return PlainTextResponse(
            document, media_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="transcripts.md"'})

    return app


app = create_app()        # for `uvicorn transcript_tool.web.app:app`
