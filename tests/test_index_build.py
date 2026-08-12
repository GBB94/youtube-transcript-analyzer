"""R2 — embeddings + the ONE LanceDB store + offline corpus build (§8, §9, §12, §14).

The embedder is a deterministic bag-of-words fake, so tests exercise the real
LanceDB table (vectors + metadata in one place) with zero network and zero model
downloads. Dense retrieval runs end to end."""
import asyncio
import hashlib

from transcript_tool.corpus.ingest import corpus_add
from transcript_tool.corpus.records import CorpusVideo
from transcript_tool.corpus.store import CorpusStore
from transcript_tool.policy import Policy
from transcript_tool.retrieval.build import corpus_build
from transcript_tool.retrieval.chunk import CHUNKER_VERSION, ChunkConfig
from transcript_tool.retrieval.index import ChunkIndex, INDEX_SCHEMA_VERSION
from transcript_tool.schema import (
    Language, Provenance, Result, Segment, TimestampType, VideoRef,
)

VID_A, VID_B = "aaaaaaaaaaa", "bbbbbbbbbbb"
CFG = ChunkConfig(max_tokens=40, min_tokens=10, overlap_ratio=0.2)


def _tokens(text):
    return len(text.split())


class FakeEmbedder:
    """Deterministic bag-of-words hashing: shared vocabulary => nearby vectors,
    so dense search is meaningful without a model."""
    name = "fake-bow"
    revision = "1"
    dim = 32

    def embed(self, texts):
        out = []
        for text in texts:
            v = [0.0] * self.dim
            for word in text.lower().split():
                h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


def _ref(vid):
    return VideoRef(platform="youtube", id=vid,
                    url=f"https://www.youtube.com/watch?v={vid}", source="url")


def _success(vid, sentences):
    segs = []
    for i, text in enumerate(sentences):
        segs.append(Segment(start=i * 5.0, end=(i + 1) * 5.0, text=text))
    return Result.make_success(
        _ref(vid), provenance=Provenance.platform_auto,
        text=" ".join(sentences), segments=segs,
        language=Language(requested=["en"], selected="en"),
        timestamp_type=TimestampType.caption_cue, raw_text=" ".join(sentences),
        duration_seconds=len(sentences) * 5.0)


SENTS_A = ["the housing market is softening in coastal cities this year.",
           "compute is becoming a tradeable commodity like oil futures.",
           "we talked about longevity escape velocity and clinical trials.",
           "the panel disagreed about open source model release cadence.",
           "robotaxis are scaling faster than anyone projected last fall."] * 3
SENTS_B = ["fusion startups raised record capital this quarter again.",
           "desalination costs are collapsing with new membrane tech.",
           "the guest argued education needs one on one ai tutors now.",
           "seasteading came up briefly and nobody defended it.",
           "quantum error correction finally crossed the threshold."] * 3


def _seed(tmp_path):
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    results = {VID_A: _success(VID_A, SENTS_A), VID_B: _success(VID_B, SENTS_B)}

    async def pull(ref, policy, cache):
        return results[ref.id]

    meta = {v: CorpusVideo(id=v, url=f"https://www.youtube.com/watch?v={v}",
                           title=f"EP {v}", upload_date="2026-07-14")
            for v in results}
    asyncio.run(corpus_add(store, "pod", [_ref(VID_A), _ref(VID_B)],
                           policy=Policy(), meta=meta, pull=pull))
    return store


def _build(store, **kw):
    kw.setdefault("embedder", FakeEmbedder())
    kw.setdefault("chunk_config", CFG)
    kw.setdefault("token_counter", _tokens)
    return corpus_build(store, "pod", **kw)


def test_build_populates_one_store_with_meta_and_vectors(tmp_path):
    store = _seed(tmp_path)
    report = _build(store)
    assert report["built"] == 2 and report["unchanged"] == 0
    assert report["chunks"] > 0

    index = ChunkIndex(store.index_dir("pod"))
    assert index.count() == report["chunks"]
    rows = index.search_dense(FakeEmbedder().embed(["compute tradeable commodity oil"])[0], n=3)
    top = rows[0]
    # The hit carries full ChunkMeta — no join needed (§9).
    assert top["video_id"] == VID_A and "compute" in top["text"]
    assert top["start_s"] < top["end_s"] and top["provenance"] == "platform_auto"
    assert top["chunk_id"].startswith(f"{VID_A}:")

    build = index.read_build()
    assert build["chunker_version"] == CHUNKER_VERSION
    assert build["embed_model"] == "fake-bow@1#32"
    assert build["context_version"] is None
    assert build["index_schema_version"] == INDEX_SCHEMA_VERSION

    entry = store.entry("pod", VID_A)
    assert entry["embed_model"] == "fake-bow@1#32" and entry["indexed_at"]


def test_rebuild_is_idempotent_and_incremental(tmp_path):
    store = _seed(tmp_path)
    _build(store)
    report = _build(store)                       # nothing changed -> no work
    assert report["built"] == 0 and report["unchanged"] == 2

    # One video goes stale (supersede resets stamps) -> only it rebuilds.
    store.update_entry("pod", VID_A, indexed_at=None, chunker_version=None)
    report = _build(store)
    assert report["built"] == 1 and report["unchanged"] == 1


def test_embedder_swap_invalidates_the_whole_table(tmp_path):
    store = _seed(tmp_path)
    _build(store)

    class OtherEmbedder(FakeEmbedder):
        name = "fake-bow-2"
    report = _build(store, embedder=OtherEmbedder())
    assert report["built"] == 2                  # full rebuild, new space
    assert ChunkIndex(store.index_dir("pod")).read_build()["embed_model"] == "fake-bow-2@1#32"


def test_removed_video_cascades_out_of_the_index(tmp_path):
    store = _seed(tmp_path)
    _build(store)
    store.remove_record("pod", VID_B)
    report = _build(store)
    assert report["removed"] == 1
    index = ChunkIndex(store.index_dir("pod"))
    assert index.video_ids() == {VID_A}


def test_index_is_rebuildable_offline_from_raw(tmp_path):
    """Acceptance §19: delete index/, rebuild from raw/ with no network, same rows."""
    import shutil
    store = _seed(tmp_path)
    first = _build(store)
    index = ChunkIndex(store.index_dir("pod"))
    before = sorted((r["chunk_id"], r["text"]) for r in
                    index.search_dense(FakeEmbedder().embed(["fusion capital"])[0], n=5))

    shutil.rmtree(store.index_dir("pod"))
    for vid in (VID_A, VID_B):
        store.update_entry("pod", vid, indexed_at=None)
    second = _build(store)
    assert second["built"] == 2 and second["chunks"] == first["chunks"]
    index = ChunkIndex(store.index_dir("pod"))
    after = sorted((r["chunk_id"], r["text"]) for r in
                   index.search_dense(FakeEmbedder().embed(["fusion capital"])[0], n=5))
    assert before == after


def test_unsafe_filter_values_are_refused(tmp_path):
    import pytest
    index = ChunkIndex(tmp_path / "index" / "pod")
    with pytest.raises(ValueError):
        index.delete_video("x' OR '1'='1")


def test_status_is_current_after_a_no_context_build(tmp_path):
    """Regression: a fresh --no-context build must read as current — context off
    is a recorded choice in build.json, not staleness against the module
    constant. Caught on the first real corpus (status said stale=162)."""
    from transcript_tool.corpus.status import corpus_status
    store = _seed(tmp_path)
    _build(store)
    status = corpus_status(store, "pod")
    assert status["indexed"] == 2 and status["stale"] == 0
    assert status["stale_video_ids"] == [] and status["index_outdated"] is False
