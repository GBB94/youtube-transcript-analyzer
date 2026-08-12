"""ONE LanceDB store (§9, §20.4): dense vectors, the native BM25 full-text
index, and the full ChunkMeta live in the same table, so the two retrieval
sides cannot drift and a hit needs no join.

`build.json` records the version tuple the index was built at (§14) — the
policy_hash analogue. Anything outside that tuple changing is a rebuild."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .chunk import Chunk

INDEX_SCHEMA_VERSION = "1.0.0"
TABLE_NAME = "chunks"

_ID_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


def _sql_str(value: str) -> str:
    """Values interpolated into LanceDB `where` expressions are restricted to the
    id alphabet — never quote-escaped free text."""
    if not _ID_SAFE.match(value):
        raise ValueError(f"unsafe value for a filter expression: {value!r}")
    return f"'{value}'"


def search_text_of(chunk: Chunk) -> str:
    """What gets embedded and lexically indexed: context + verbatim text (§7).
    The verbatim `text` column stays separate — it is what gets quoted."""
    return f"{chunk.context}\n{chunk.text}" if chunk.context else chunk.text


def _arrow_schema(dim: int):
    """Explicit schema so nullable string columns (context, speaker, title) never
    get inferred as Null from an all-None first batch."""
    import pyarrow as pa
    return pa.schema([
        ("chunk_id", pa.string()),
        ("video_id", pa.string()),
        ("source_slug", pa.string()),
        ("url", pa.string()),
        ("title", pa.string()),
        ("upload_date", pa.string()),
        ("start_s", pa.float64()),
        ("end_s", pa.float64()),
        ("provenance", pa.string()),
        ("speaker", pa.string()),
        ("text", pa.string()),
        ("context", pa.string()),
        ("chunker_version", pa.string()),
        ("context_version", pa.string()),
        ("search_text", pa.string()),
        ("vector", pa.list_(pa.float32(), dim)),
    ])


def _row(chunk: Chunk, vector: list[float]) -> dict:
    row = chunk.model_dump()
    row["search_text"] = search_text_of(chunk)
    row["vector"] = vector
    return row


class ChunkIndex:
    """The index layer for one source slug. Derived and rebuildable: deleting the
    directory and rebuilding from raw/ is routine, offline, and idempotent."""

    def __init__(self, index_dir: Path):
        self.index_dir = Path(index_dir)
        self._db = None
        self._table = None

    # ---- connection ---------------------------------------------------------

    def _connect(self):
        if self._db is None:
            import lancedb  # lazy — optional `retrieval` extra
            self.index_dir.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.index_dir / "lancedb"))
        return self._db

    def _names(self) -> list[str]:
        listing = self._connect().list_tables()
        return list(getattr(listing, "tables", listing))

    def table(self):
        if self._table is None:
            if TABLE_NAME not in self._names():
                return None
            self._table = self._connect().open_table(TABLE_NAME)
        return self._table

    def exists(self) -> bool:
        return self.table() is not None

    def drop(self) -> None:
        if TABLE_NAME in self._names():
            self._connect().drop_table(TABLE_NAME)
        self._table = None
        if self.build_path.exists():
            self.build_path.unlink()

    # ---- writes -------------------------------------------------------------

    def replace_video(self, video_id: str, chunks: list[Chunk],
                      vectors: list[list[float]]) -> None:
        """Delete-then-add for one video: per-video incrementality (§12)."""
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must parallel one another")
        rows = [_row(c, v) for c, v in zip(chunks, vectors, strict=True)]
        tbl = self.table()
        if tbl is None:
            if not rows:
                return
            self._table = self._connect().create_table(
                TABLE_NAME, data=rows, schema=_arrow_schema(len(vectors[0])))
            return
        tbl.delete(f"video_id = {_sql_str(video_id)}")
        if rows:
            tbl.add(rows)

    def delete_video(self, video_id: str) -> None:
        expr = f"video_id = {_sql_str(video_id)}"     # validate even when empty
        tbl = self.table()
        if tbl is not None:
            tbl.delete(expr)

    def refresh_fts(self) -> None:
        """(Re)build the native BM25 full-text index over search_text (§20.4)."""
        tbl = self.table()
        if tbl is not None:
            from lancedb.index import FTS  # lazy with the rest of lancedb
            tbl.create_index("search_text", config=FTS(), replace=True)

    # ---- reads --------------------------------------------------------------

    def count(self) -> int:
        tbl = self.table()
        return tbl.count_rows() if tbl is not None else 0

    def video_ids(self) -> set[str]:
        tbl = self.table()
        if tbl is None:
            return set()
        import pyarrow.compute as pc  # noqa: F401  (arrow ships with lancedb)
        data = tbl.to_arrow().column("video_id")
        return set(data.to_pylist())

    def get_chunks(self, chunk_ids: list[str]) -> dict[str, dict]:
        """Fetch stored rows by chunk id (parent-context expansion, §10). ids are
        `video_id:ordinal`, so ':' joins two id-alphabet halves."""
        if not chunk_ids:
            return {}
        quoted = []
        for cid in chunk_ids:
            vid, _, ordinal = cid.rpartition(":")
            quoted.append(f"'{_sql_str(vid)[1:-1]}:{_sql_str(ordinal)[1:-1]}'")
        tbl = self.table()
        if tbl is None:
            return {}
        rows = (tbl.search(None).where(f"chunk_id IN ({', '.join(quoted)})")
                .limit(len(chunk_ids)).to_list())
        return {r["chunk_id"]: r for r in rows}

    def search_dense(self, vector: list[float], n: int,
                     where: Optional[str] = None) -> list[dict]:
        tbl = self.table()
        if tbl is None:
            return []
        q = tbl.search(vector, vector_column_name="vector").limit(n)
        if where:
            q = q.where(where, prefilter=True)
        return q.to_list()

    def search_fts(self, query: str, n: int, where: Optional[str] = None) -> list[dict]:
        tbl = self.table()
        if tbl is None:
            return []
        q = tbl.search(query, query_type="fts", fts_columns="search_text").limit(n)
        if where:
            q = q.where(where, prefilter=True)
        return q.to_list()

    # ---- build manifest (§14) -----------------------------------------------

    @property
    def build_path(self) -> Path:
        return self.index_dir / "build.json"

    def read_build(self) -> Optional[dict]:
        if not self.build_path.exists():
            return None
        return json.loads(self.build_path.read_text(encoding="utf-8"))

    def write_build(self, versions: dict) -> None:
        payload = dict(versions)
        payload["built_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload["index_schema_version"] = INDEX_SCHEMA_VERSION
        self.build_path.parent.mkdir(parents=True, exist_ok=True)
        self.build_path.write_text(json.dumps(payload, indent=2, sort_keys=True),
                                   encoding="utf-8")
