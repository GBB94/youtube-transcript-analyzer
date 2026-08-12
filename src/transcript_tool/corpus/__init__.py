"""Corpus subsystem (RETRIEVAL_DESIGN.md §4-§5, R0) — the canonical store.

Canonical vs. derived is sacred: `raw/*.json` (one CorpusRecord per video) is the
only expensive, never-discarded layer. Markdown, chunks, embeddings, and indexes
are pure functions of it and rebuild offline. Ingest reuses the existing
`find` + `pull` pipeline — no new YouTube access path.
"""
from .records import CORPUS_SCHEMA_VERSION, Chapter, CorpusRecord, CorpusVideo, content_hash
from .store import CorpusStore
from .ingest import IngestReport, corpus_add

__all__ = [
    "CORPUS_SCHEMA_VERSION", "Chapter", "CorpusRecord", "CorpusVideo", "content_hash",
    "CorpusStore", "IngestReport", "corpus_add",
]
