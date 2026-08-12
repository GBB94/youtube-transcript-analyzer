"""corpus status — counts, versions, staleness (RETRIEVAL_DESIGN.md §11, §12, §14).

Answers "what do we have, and is any of it stale relative to the current
derivation versions?" from the manifest alone, without scanning raw files."""
from __future__ import annotations

from collections import Counter

from .store import CorpusStore


def _current_versions() -> dict:
    """The derivation-version tuple (§14). Retrieval modules are optional extras,
    so absence reads as 'not installed', never a crash."""
    versions: dict = {}
    try:
        from ..retrieval.chunk import CHUNKER_VERSION
        versions["chunker_version"] = CHUNKER_VERSION
    except ImportError:
        versions["chunker_version"] = None
    try:
        from ..retrieval.context import CONTEXT_VERSION
        versions["context_version"] = CONTEXT_VERSION
    except ImportError:
        versions["context_version"] = None
    return versions


def corpus_status(store: CorpusStore, slug: str) -> dict:
    manifest = store.load_manifest(slug)
    videos = manifest["videos"]
    current = _current_versions()

    stale = []
    for vid, entry in videos.items():
        if entry.get("indexed_at") is None:
            stale.append(vid)
            continue
        for key in ("chunker_version", "context_version"):
            if current.get(key) is not None and entry.get(key) != current[key]:
                stale.append(vid)
                break

    provenance = Counter(e.get("provenance") for e in videos.values())
    build = None
    build_path = store.index_dir(slug) / "build.json"
    if build_path.exists():
        import json
        build = json.loads(build_path.read_text(encoding="utf-8"))

    return {
        "source_slug": slug,
        "videos": len(videos),
        "diarized": sum(1 for e in videos.values() if e.get("diarized")),
        "indexed": sum(1 for e in videos.values() if e.get("indexed_at")),
        "stale": len(stale),
        "stale_video_ids": sorted(stale),
        "provenance": dict(provenance),
        "current_versions": current,
        "index_build": build,
    }
