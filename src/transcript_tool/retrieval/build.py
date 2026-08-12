"""corpus build — (re)derive chunks -> context -> embeddings -> index (§11, §12, §14).

Offline and idempotent: a pure function of the canonical layer. Version-aware:
each video's stored derivation stamps are compared against current values and
only the stale ones rebuild. A version bump rebuilds derived layers; it NEVER
re-pulls the canonical layer. Runs under the per-slug lock so two concurrent
builds can't corrupt the index."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from ..corpus.store import CorpusStore
from .chunk import ChunkConfig, TokenCounter, chunk_record, CHUNKER_VERSION, token_counter_name
from .embed import Embedder, embedder_id, get_embedder
from .index import ChunkIndex, INDEX_SCHEMA_VERSION, search_text_of

# A contextizer maps (record, chunks) -> per-chunk blurbs (None entries allowed).
# R4 provides the Anthropic-backed one; None means context off for this build.
Contextizer = Callable[..., list[Optional[str]]]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _context_version(contextizer: Optional[Contextizer]) -> Optional[str]:
    if contextizer is None:
        return None
    from .context import CONTEXT_VERSION
    return CONTEXT_VERSION


def corpus_build(store: CorpusStore, slug: str, *, rebuild: bool = False,
                 embedder: Optional[Embedder] = None, embedder_kind: str = "local",
                 use_context: bool = False, contextizer: Optional[Contextizer] = None,
                 chunk_config: ChunkConfig = ChunkConfig(),
                 token_counter: Optional[TokenCounter] = None,
                 log: Callable[[str], None] = lambda _msg: None) -> dict:
    """Returns {built, unchanged, removed, chunks, versions}. `embedder` and
    `contextizer` are injectable; the defaults resolve from `embedder_kind` /
    `use_context`. The CLI passes use_context=True unless --no-context (§20:
    enrichment is on by default and skippable)."""
    embedder = embedder or get_embedder(embedder_kind)
    if use_context and contextizer is None:
        from .context import default_contextizer
        contextizer = default_contextizer()
    emb_id = embedder_id(embedder)
    ctx_version = _context_version(contextizer)

    index = ChunkIndex(store.index_dir(slug))
    report: dict = {"built": 0, "unchanged": 0, "removed": 0, "chunks": 0}

    with store.lock(slug):
        manifest = store.load_manifest(slug)
        videos = manifest["videos"]

        # A different embedding space or index schema invalidates the whole table —
        # per-video incrementality only holds inside one build tuple.
        build = index.read_build()
        table_invalid = False
        if build is not None and (build.get("embed_model") != emb_id
                                  or build.get("index_schema_version") != INDEX_SCHEMA_VERSION):
            table_invalid = True
            log(f"corpus build: build tuple changed ({build.get('embed_model')} -> "
                f"{emb_id}); full rebuild")
        if rebuild or table_invalid:
            index.drop()

        # Deletion cascades: index rows for videos gone from the manifest.
        for gone in sorted(index.video_ids() - set(videos.keys())):
            index.delete_video(gone)
            report["removed"] += 1

        indexed_ids = index.video_ids()
        for video_id in sorted(videos.keys()):
            entry = videos[video_id]
            current = (entry.get("chunker_version") == CHUNKER_VERSION
                       and entry.get("context_version") == ctx_version
                       and entry.get("embed_model") == emb_id
                       and entry.get("indexed_at") is not None
                       and video_id in indexed_ids)
            if current:
                report["unchanged"] += 1
                continue

            record = store.load_record(slug, video_id)
            chunks = chunk_record(record, chunk_config, token_counter)
            if contextizer is not None and chunks:
                blurbs = contextizer(record, chunks)
                if len(blurbs) != len(chunks):
                    raise ValueError("contextizer must return one blurb per chunk")
                for chunk, blurb in zip(chunks, blurbs, strict=True):
                    chunk.context = blurb
                    chunk.context_version = ctx_version if blurb is not None else None
            vectors = embedder.embed([search_text_of(c) for c in chunks]) if chunks else []
            index.replace_video(video_id, chunks, vectors)
            log(f"corpus build: {video_id} -> {len(chunks)} chunks")

            entry.update({"chunker_version": CHUNKER_VERSION,
                          "context_version": ctx_version,
                          "embed_model": emb_id,
                          "indexed_at": _utcnow_iso()})
            report["built"] += 1
            report["chunks"] += len(chunks)

        store.save_manifest(slug, manifest)

        if report["built"] or report["removed"]:
            index.refresh_fts()
        versions = {
            "corpus_schema_version": manifest.get("corpus_schema_version"),
            "chunker_version": CHUNKER_VERSION,
            "context_version": ctx_version,
            "embed_model": emb_id,
            "token_counter": token_counter_name() if token_counter is None else "injected",
            "chunk_config": {"max_tokens": chunk_config.max_tokens,
                             "min_tokens": chunk_config.min_tokens,
                             "overlap_ratio": chunk_config.overlap_ratio},
        }
        index.write_build(versions)
        report["versions"] = versions

    return report
