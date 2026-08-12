"""CorpusStore — the three-layer on-disk model (RETRIEVAL_DESIGN.md §4).

corpus/<slug>/manifest.json      the ingest ledger (incrementality, staleness)
corpus/<slug>/raw/*.json         CANONICAL — never discarded
corpus/<slug>/markdown/*.md      READABLE — derived convenience
index/<slug>/…                   INDEX — derived, rebuildable (owned by retrieval/)

Writes are atomic (temp file + os.replace) and manifest updates take a per-slug
filesystem lock (reuses locking.py), the same posture as the cache."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from ..locking import FileLockBackend
from .records import CORPUS_SCHEMA_VERSION, CorpusRecord

MANIFEST_VERSION = 1


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class CorpusStore:
    def __init__(self, root: str | Path = "corpus", *, index_root: str | Path | None = None):
        self.root = Path(root).expanduser()
        # index/ is a sibling of corpus/ by default (§4 layout).
        self.index_root = Path(index_root).expanduser() if index_root else self.root.parent / "index"
        self._locks = FileLockBackend(self.root / ".locks")

    # ---- paths --------------------------------------------------------------

    def slug_dir(self, slug: str) -> Path:
        return self.root / slug

    def raw_dir(self, slug: str) -> Path:
        return self.slug_dir(slug) / "raw"

    def markdown_dir(self, slug: str) -> Path:
        return self.slug_dir(slug) / "markdown"

    def manifest_path(self, slug: str) -> Path:
        return self.slug_dir(slug) / "manifest.json"

    def index_dir(self, slug: str) -> Path:
        return self.index_root / slug

    # ---- manifest (the ingest ledger, §4) -----------------------------------

    def load_manifest(self, slug: str) -> dict:
        p = self.manifest_path(slug)
        if not p.exists():
            return {"manifest_version": MANIFEST_VERSION, "source_slug": slug,
                    "corpus_schema_version": CORPUS_SCHEMA_VERSION, "videos": {}}
        return json.loads(p.read_text(encoding="utf-8"))

    def _save_manifest(self, slug: str, manifest: dict) -> None:
        _atomic_write_text(self.manifest_path(slug),
                           json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))

    def save_manifest(self, slug: str, manifest: dict) -> None:
        """For callers already holding `lock(slug)` (the flock is not reentrant —
        re-acquiring from inside would deadlock). Everyone else uses update_entry."""
        self._save_manifest(slug, manifest)

    def lock(self, slug: str):
        """Per-slug mutual exclusion for manifest/index mutation (singleflight posture)."""
        return self._locks.lock(f"corpus-{slug}")

    def video_ids(self, slug: str) -> set[str]:
        return set(self.load_manifest(slug)["videos"].keys())

    def entry(self, slug: str, video_id: str) -> Optional[dict]:
        return self.load_manifest(slug)["videos"].get(video_id)

    def update_entry(self, slug: str, video_id: str, **fields: Any) -> None:
        """Merge fields into one video's ledger entry (build stamps, staleness)."""
        with self.lock(slug):
            manifest = self.load_manifest(slug)
            entry = manifest["videos"].get(video_id)
            if entry is None:
                raise KeyError(f"{video_id} is not in the {slug} manifest")
            entry.update(fields)
            self._save_manifest(slug, manifest)

    # ---- canonical + readable layers ----------------------------------------

    def add_record(self, record: CorpusRecord, *, markdown: Optional[str] = None) -> Path:
        """Write the canonical record (and optional rendered markdown), then update
        the manifest under the slug lock. Returns the raw path."""
        slug = record.source_slug
        raw_path = self.raw_dir(slug) / f"{record.file_stem()}.json"
        _atomic_write_text(raw_path, record.model_dump_json(indent=2))
        markdown_built_at = None
        if markdown is not None:
            _atomic_write_text(self.markdown_dir(slug) / f"{record.file_stem()}.md", markdown)
            markdown_built_at = _utcnow_iso()

        with self.lock(slug):
            manifest = self.load_manifest(slug)
            # A superseded transcript keeps the id but may change dates: drop any
            # older raw/markdown files for this video so the layer stays one-per-video.
            old = manifest["videos"].get(record.video.id)
            if old and old.get("raw_file") and old["raw_file"] != raw_path.name:
                for layer, suffix in ((self.raw_dir(slug), ".json"), (self.markdown_dir(slug), ".md")):
                    stale = layer / (Path(old["raw_file"]).stem + suffix)
                    if stale.exists():
                        stale.unlink()
            manifest["videos"][record.video.id] = {
                "video_id": record.video.id,
                "title": record.video.title,
                "upload_date": record.video.upload_date,
                "url": record.video.url,
                "duration_s": record.video.duration_s,
                "provenance": record.provenance.value,
                "diarized": record.diarized,
                "content_hash": record.content_hash,
                "pulled_at": record.pulled_at.isoformat(timespec="seconds"),
                "raw_file": raw_path.name,
                "markdown_built_at": markdown_built_at,
                # Derivation stamps — None until `corpus build` processes this video.
                "chunker_version": None,
                "context_version": None,
                "embed_model": None,
                "indexed_at": None,
            }
            self._save_manifest(slug, manifest)
        return raw_path

    def load_record(self, slug: str, video_id: str) -> CorpusRecord:
        entry = self.entry(slug, video_id)
        if entry is None or not entry.get("raw_file"):
            raise KeyError(f"{video_id} is not in the {slug} corpus")
        return CorpusRecord.model_validate_json(
            (self.raw_dir(slug) / entry["raw_file"]).read_text(encoding="utf-8"))

    def iter_records(self, slug: str) -> Iterator[CorpusRecord]:
        for video_id in sorted(self.video_ids(slug)):
            yield self.load_record(slug, video_id)

    def remove_record(self, slug: str, video_id: str) -> None:
        """Deletion cascades (§15): removing a canonical record removes its derived
        files and ledger entry. Index-row removal happens on the next build."""
        with self.lock(slug):
            manifest = self.load_manifest(slug)
            entry = manifest["videos"].pop(video_id, None)
            self._save_manifest(slug, manifest)
        if entry and entry.get("raw_file"):
            stem = Path(entry["raw_file"]).stem
            for path in (self.raw_dir(slug) / f"{stem}.json",
                         self.markdown_dir(slug) / f"{stem}.md"):
                if path.exists():
                    path.unlink()
