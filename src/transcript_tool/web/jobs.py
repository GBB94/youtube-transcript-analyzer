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
STATUS_CANCELLED = "cancelled"
TERMINAL = (STATUS_COMPLETE, STATUS_FAILED, STATUS_CANCELLED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    languages TEXT NOT NULL,
    status TEXT NOT NULL,
    asr INTEGER DEFAULT 0
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
            self._migrate(c)

    @staticmethod
    def _migrate(c) -> None:
        """Additive migrations for DBs created by an earlier version. CREATE TABLE
        IF NOT EXISTS won't add new columns to a pre-existing table, so backfill them."""
        existing = {row["name"] for row in c.execute("PRAGMA table_info(jobs)")}
        if "asr" not in existing:
            c.execute("ALTER TABLE jobs ADD COLUMN asr INTEGER DEFAULT 0")

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

    def create_job(self, job_id: str, created_at: str, languages: list[str], items,
                   asr: bool = False) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO jobs (id, created_at, languages, status, asr) VALUES (?,?,?,?,?)",
                      (job_id, created_at, ",".join(languages), STATUS_QUEUED, 1 if asr else 0))
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
        done = sum(by.get(s, 0) for s in TERMINAL)
        retryable = self._retryable_count(job_id)
        return {"total": total, "complete": by.get(STATUS_COMPLETE, 0),
                "failed": by.get(STATUS_FAILED, 0), "cancelled": by.get(STATUS_CANCELLED, 0),
                "done": done, "active": total - done, "retryable": retryable,
                "finished": total > 0 and done == total}

    def _retryable_count(self, job_id: str) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) n FROM items WHERE job_id=? AND status=? AND retry=1",
                             (job_id, STATUS_FAILED)).fetchone()["n"]

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
                # Don't overwrite a cancelled job back to complete.
                c.execute("UPDATE jobs SET status=? WHERE id=? AND status != ?",
                          (STATUS_COMPLETE, job_id, STATUS_CANCELLED))
            return True
        return False

    def is_cancelled(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and job["status"] == STATUS_CANCELLED)

    # ---- cancel / retry -----------------------------------------------------

    def request_cancel(self, job_id: str) -> int:
        """Mark the job cancelled and move every not-yet-terminal item to cancelled.
        Returns the number of items cancelled. In-flight compute is stopped separately
        by terminating the worker process."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE items SET status=? WHERE job_id=? AND status IN (?, ?)",
                (STATUS_CANCELLED, job_id, STATUS_QUEUED, STATUS_RUNNING))
            c.execute("UPDATE jobs SET status=? WHERE id=?", (STATUS_CANCELLED, job_id))
            return cur.rowcount

    def requeue_item(self, job_id: str, idx: int) -> bool:
        """Re-queue a single retry-eligible failed item. Returns True if re-queued."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE items SET status=?, message=NULL, badge=NULL, retry=0 "
                "WHERE job_id=? AND idx=? AND status=? AND retry=1",
                (STATUS_QUEUED, job_id, idx, STATUS_FAILED))
            if cur.rowcount:
                c.execute("UPDATE jobs SET status=? WHERE id=?", (STATUS_QUEUED, job_id))
            return cur.rowcount > 0

    def requeue_failed(self, job_id: str) -> int:
        """Re-queue all retry-eligible failed items. Returns how many."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE items SET status=?, message=NULL, badge=NULL, retry=0 "
                "WHERE job_id=? AND status=? AND retry=1",
                (STATUS_QUEUED, job_id, STATUS_FAILED))
            if cur.rowcount:
                c.execute("UPDATE jobs SET status=? WHERE id=?", (STATUS_QUEUED, job_id))
            return cur.rowcount
