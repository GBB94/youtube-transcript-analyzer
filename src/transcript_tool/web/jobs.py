"""Durable job model (docs/UI_SCOPE.md §6). SQLite holds a Job and its per-VideoItem
status so a batch survives a refresh or restart. Written by the worker process, read by
the web process (SSE) — WAL mode + a busy timeout make that safe across processes.

Per-item rendered Markdown sections are stored as they complete, so
`/jobs/{id}/transcripts.md` can assemble a partial document at any time without holding
full Result objects."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# Item lifecycle. The engine's get_transcript is a single call, so the UI shows coarse
# states (queued -> running -> complete/failed); finer stages would need engine progress
# callbacks (a later refinement). "failed" never blocks sibling items.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    languages TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    job_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    url TEXT NOT NULL,
    raw TEXT NOT NULL,
    status TEXT NOT NULL,
    badge TEXT,
    message TEXT,
    words INTEGER DEFAULT 0,
    retry INTEGER DEFAULT 0,
    md_section TEXT,
    PRIMARY KEY (job_id, idx)
);
"""


class JobStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- create / read ------------------------------------------------------

    def create_job(self, job_id: str, created_at: str, languages: list[str], items) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO jobs (id, created_at, languages, status) VALUES (?,?,?,?)",
                      (job_id, created_at, ",".join(languages), STATUS_QUEUED))
            c.executemany(
                "INSERT INTO items (job_id, idx, video_id, url, raw, status) VALUES (?,?,?,?,?,?)",
                [(job_id, i, t.video_id, t.url, t.raw, STATUS_QUEUED) for i, t in enumerate(items)])

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def get_items(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM items WHERE job_id=? ORDER BY idx", (job_id,)).fetchall()]

    def counts(self, job_id: str) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM items WHERE job_id=? GROUP BY status",
                             (job_id,)).fetchall()
        by = {r["status"]: r["n"] for r in rows}
        total = sum(by.values())
        done = by.get(STATUS_COMPLETE, 0) + by.get(STATUS_FAILED, 0)
        return {"total": total, "complete": by.get(STATUS_COMPLETE, 0),
                "failed": by.get(STATUS_FAILED, 0), "done": done,
                "finished": total > 0 and done == total}

    # ---- worker transitions -------------------------------------------------

    def claim_next_queued(self, job_id: str) -> Optional[dict]:
        """Atomically take the next queued item (BEGIN IMMEDIATE so two workers can't
        grab the same one). Returns the claimed item or None when the queue is drained."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM items WHERE job_id=? AND status=? ORDER BY idx LIMIT 1",
                (job_id, STATUS_QUEUED)).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute("UPDATE items SET status=? WHERE job_id=? AND idx=?",
                         (STATUS_RUNNING, job_id, row["idx"]))
            conn.execute("UPDATE jobs SET status=? WHERE id=?", (STATUS_RUNNING, job_id))
            conn.execute("COMMIT")
            return dict(row)
        finally:
            conn.close()

    def complete_item(self, job_id: str, idx: int, *, badge: str, words: int, md_section: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE items SET status=?, badge=?, words=?, md_section=? WHERE job_id=? AND idx=?",
                      (STATUS_COMPLETE, badge, words, md_section, job_id, idx))

    def fail_item(self, job_id: str, idx: int, *, message: str, retry: bool) -> None:
        with self._conn() as c:
            c.execute("UPDATE items SET status=?, message=?, retry=? WHERE job_id=? AND idx=?",
                      (STATUS_FAILED, message, 1 if retry else 0, job_id, idx))

    def finalize_if_done(self, job_id: str) -> bool:
        if self.counts(job_id)["finished"]:
            with self._conn() as c:
                c.execute("UPDATE jobs SET status=? WHERE id=?", (STATUS_COMPLETE, job_id))
            return True
        return False
