"""Batch transcript web UI (docs/UI_SCOPE.md). A thin local FastAPI app over the
engine: paste links -> review -> one button -> one Markdown file.

UI-1: paste -> validate (client+server) -> synchronous small-batch pull -> .md download.
Later phases add the SQLite job model, worker process, and SSE (UI-2+)."""
