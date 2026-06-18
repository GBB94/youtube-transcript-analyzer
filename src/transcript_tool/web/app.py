"""FastAPI app (UI-1). Server-rendered (Jinja + HTMX). Paste -> validate (client+server)
-> synchronous small-batch pull (captions-first) -> one Markdown file.

The transcription call is injected (`pull`) so tests run with a fake and never touch the
network; the default pulls captions-first via the engine. The SQLite job model, worker
process, and SSE arrive in UI-2+. ASR opt-in is UI-4 — UI-1 is captions-only."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from ..schema import Outcome, Reason, Result
from .copy import badge_for, message_for, retry_allowed
from .markdown import Record, render
from .parse import Target, parse_targets

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MAX_BATCH = 25                      # UI-1 is synchronous; bigger batches wait for UI-2's worker

PullFn = Callable[[Target], Result]


def _default_pull(target: Target) -> Result:
    """Captions-first pull via the engine. Public-URL egress is enabled because running
    the local app and pasting links IS the operator's acknowledgment (UI-4 turns this
    into a revocable one-time setting). No ASR in UI-1."""
    from ..cache import Cache
    from ..orchestrator import get_transcript_sync
    from ..policy import EgressPolicy, Policy
    app_cache = Cache(Path("~/.cache/transcript-tool").expanduser())
    policy = Policy(
        enabled_strategies=("api_captions", "ytdlp_subs"),
        egress=EgressPolicy(allow_network=True, allow_public_url=True),
    )
    return get_transcript_sync(target.ref(), policy, app_cache)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_app(pull: Optional[PullFn] = None, max_batch: int = MAX_BATCH) -> FastAPI:
    app = FastAPI(title="Batch Transcripts")
    app.state.pull = pull or _default_pull
    app.state.results = {}            # token -> {"markdown": str, "rows": [...]}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return _TEMPLATES.TemplateResponse(request, "index.html", {})

    @app.post("/validate", response_class=HTMLResponse)
    def validate(request: Request, links: str = Form("")):
        parsed = parse_targets(links)
        return _TEMPLATES.TemplateResponse(request, "_validation.html", {"parsed": parsed})

    @app.post("/transcribe", response_class=HTMLResponse)
    def transcribe(request: Request, links: str = Form("")):
        parsed = parse_targets(links)
        if not parsed.valid:
            return _TEMPLATES.TemplateResponse(
                request, "results.html",
                {"error": "No valid YouTube links to transcribe.",
                 "rows": [], "token": None, "parsed": parsed})
        if len(parsed.valid) > max_batch:
            return _TEMPLATES.TemplateResponse(
                request, "results.html",
                {"rows": [], "token": None, "parsed": parsed,
                 "error": f"UI-1 runs synchronously — keep it to {max_batch} links per batch."})

        pull_fn: PullFn = app.state.pull
        records: list[Record] = []
        rows = []
        for t in parsed.valid:
            try:
                result = pull_fn(t)
            except Exception:  # never let one link break the batch
                result = Result.make_failed(t.ref(), Reason.provider_error)
            records.append(Record(target=t, result=result))
            ok = result.outcome is Outcome.success
            rows.append({
                "url": t.url, "video_id": t.video_id, "ok": ok,
                "badge": badge_for(result) if ok else None,
                "message": "" if ok else message_for(result),
                "retry": (not ok) and retry_allowed(result),
                "words": result.word_count if ok else 0,
            })

        token = uuid.uuid4().hex
        markdown = render(records, language_preferences=["en"], generated_at=_now_iso())
        app.state.results[token] = {"markdown": markdown, "rows": rows}
        n_ok = sum(1 for r in rows if r["ok"])
        return _TEMPLATES.TemplateResponse(
            request, "results.html",
            {"rows": rows, "token": token,
             "n_ok": n_ok, "n_total": len(rows), "error": None})

    @app.get("/download/{token}.md")
    def download(token: str):
        entry = app.state.results.get(token)
        if not entry:
            return PlainTextResponse("Unknown or expired download.", status_code=404)
        return PlainTextResponse(
            entry["markdown"], media_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="transcripts.md"'})

    return app


app = create_app()        # for `uvicorn transcript_tool.web.app:app`
