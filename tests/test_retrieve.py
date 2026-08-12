"""R3 — hybrid retrieval over the one store (§9): BM25 nails exact terms dense
fuzzes, RRF fusion, injectable cross-encoder rerank, prefilter metadata filters."""
import asyncio

import pytest

from transcript_tool.corpus.ingest import corpus_add
from transcript_tool.corpus.records import CorpusVideo
from transcript_tool.corpus.store import CorpusStore
from transcript_tool.policy import Policy
from transcript_tool.retrieval.build import corpus_build
from transcript_tool.retrieval.chunk import ChunkConfig
from transcript_tool.retrieval.index import ChunkIndex
from transcript_tool.retrieval.retrieve import Filters, RetrieveConfig, retrieve
from transcript_tool.schema import (
    Language, Provenance, Result, Segment, TimestampType, VideoRef,
)

from test_index_build import FakeEmbedder, _tokens  # deterministic, no network

VID_A, VID_B = "aaaaaaaaaaa", "bbbbbbbbbbb"
CFG = ChunkConfig(max_tokens=30, min_tokens=8, overlap_ratio=0.1)


def _ref(vid):
    return VideoRef(platform="youtube", id=vid,
                    url=f"https://www.youtube.com/watch?v={vid}", source="url")


def _success(vid, sentences, provenance=Provenance.platform_auto):
    segs = [Segment(start=i * 5.0, end=(i + 1) * 5.0, text=t)
            for i, t in enumerate(sentences)]
    return Result.make_success(
        _ref(vid), provenance=provenance, text=" ".join(sentences), segments=segs,
        language=Language(requested=["en"], selected="en"),
        timestamp_type=TimestampType.caption_cue, raw_text=" ".join(sentences))


SENTS_A = ["the housing market is softening in coastal cities this year.",
           "compute is becoming a tradeable commodity like oil futures.",
           "we discussed the PO-token gating change on youtube clients.",
           "robotaxis are scaling faster than projected last fall."] * 2
SENTS_B = ["fusion startups raised record capital again this quarter.",
           "desalination costs collapse with the new membrane technology.",
           "education needs one on one tutors powered by language models.",
           "quantum error correction crossed the usable threshold."] * 2


@pytest.fixture()
def corpus(tmp_path):
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    results = {VID_A: _success(VID_A, SENTS_A),
               VID_B: _success(VID_B, SENTS_B, provenance=Provenance.local_asr)}

    async def pull(ref, policy, cache):
        return results[ref.id]

    meta = {VID_A: CorpusVideo(id=VID_A, url=f"https://youtu.be/{VID_A}",
                               title="EP A", upload_date="2026-01-10"),
            VID_B: CorpusVideo(id=VID_B, url=f"https://youtu.be/{VID_B}",
                               title="EP B", upload_date="2026-07-01")}
    asyncio.run(corpus_add(store, "pod", [_ref(VID_A), _ref(VID_B)],
                           policy=Policy(), meta=meta, pull=pull))
    corpus_build(store, "pod", embedder=FakeEmbedder(), chunk_config=CFG,
                 token_counter=_tokens)
    return store


def _retrieve(store, query, **kw):
    kw.setdefault("embedder", FakeEmbedder())
    return retrieve(ChunkIndex(store.index_dir("pod")), query, **kw)


def test_hybrid_finds_topical_chunks_with_full_meta(corpus):
    trace = _retrieve(corpus, "compute becoming a tradeable commodity")
    assert trace.hits
    top = trace.hits[0].chunk
    assert top["video_id"] == VID_A and "tradeable commodity" in top["text"]
    assert top["start_s"] < top["end_s"] and top["chunk_id"].startswith(VID_A)
    assert trace.hits[0].fused_rank == 1


def test_bm25_side_nails_exact_jargon(corpus):
    # "PO-token" is exactly the kind of term dense vectors fuzz (§9).
    trace = _retrieve(corpus, "PO-token gating")
    assert any("PO-token" in h.chunk["text"] for h in trace.hits[:3])


def test_filters_prefilter_before_ranking(corpus):
    unfiltered = _retrieve(corpus, "record capital")
    assert any(h.chunk["video_id"] == VID_B for h in unfiltered.hits)

    only_a = _retrieve(corpus, "record capital", filters=Filters(video_id=VID_A))
    assert only_a.hits and all(h.chunk["video_id"] == VID_A for h in only_a.hits)
    assert only_a.where == f"video_id = '{VID_A}'"

    since = _retrieve(corpus, "record capital", filters=Filters(since="2026-06-01"))
    assert since.hits and all(h.chunk["upload_date"] >= "2026-06-01" for h in since.hits)

    prov = _retrieve(corpus, "record capital", filters=Filters(provenance="local_asr"))
    assert prov.hits and all(h.chunk["provenance"] == "local_asr" for h in prov.hits)


def test_rerank_stage_reorders_and_is_recorded(corpus):
    class KeywordReranker:
        name = "fake-rerank"
        revision = "1"

        def score(self, query, texts):
            return [float(sum(w in t for w in query.split())) for t in texts]

    trace = _retrieve(corpus, "desalination membrane", reranker=KeywordReranker(),
                      config=RetrieveConfig(k=4))
    assert trace.reranked and trace.reranker == "fake-rerank@1"
    assert len(trace.hits) <= 4
    assert "desalination" in trace.hits[0].chunk["text"]
    scores = [h.rerank_score for h in trace.hits]
    assert scores == sorted(scores, reverse=True)


def test_k_caps_what_the_answerer_sees(corpus):
    trace = _retrieve(corpus, "the this year quarter", config=RetrieveConfig(k=2))
    assert len(trace.hits) <= 2


def test_empty_index_returns_empty_trace(tmp_path):
    trace = retrieve(ChunkIndex(tmp_path / "index" / "none"), "anything",
                     embedder=FakeEmbedder())
    assert trace.hits == [] and not trace.reranked


def test_unsafe_filter_value_is_refused(corpus):
    with pytest.raises(ValueError):
        _retrieve(corpus, "q", filters=Filters(speaker="x'; DROP TABLE chunks;--"))
