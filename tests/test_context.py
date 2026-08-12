"""R4 — contextual enrichment (§7): prompt-cache-friendly episode doc, verbatim
text preserved separately, context flows into search_text, version-aware
rebuilds when context is toggled."""
import asyncio

import pytest

from transcript_tool.corpus.ingest import corpus_add
from transcript_tool.corpus.records import CorpusVideo
from transcript_tool.corpus.store import CorpusStore
from transcript_tool.policy import Policy
from transcript_tool.retrieval.build import corpus_build
from transcript_tool.retrieval.chunk import ChunkConfig
from transcript_tool.retrieval.context import (
    CONTEXT_VERSION, chunk_prompt, episode_document, generate_contexts,
)
from transcript_tool.retrieval.index import ChunkIndex
from transcript_tool.retrieval.retrieve import retrieve
from transcript_tool.schema import (
    Language, Provenance, Result, Segment, TimestampType, VideoRef,
)

from test_index_build import FakeEmbedder, _tokens

VID = "aaaaaaaaaaa"
CFG = ChunkConfig(max_tokens=30, min_tokens=8, overlap_ratio=0.1)


class FakeContextClient:
    """Records calls; returns a deterministic blurb naming the episode."""
    def __init__(self):
        self.calls = []

    def contextualize(self, episode_doc, chunk_text):
        self.calls.append((episode_doc, chunk_text))
        return "From EP Alpha (2026-07-14); zanzibar discussion. "


def _seed(tmp_path):
    sentences = ["he says the model doubles every eighteen months or so.",
                 "the panel pushed back hard on that projection immediately.",
                 "then they moved on to desalination membrane economics."] * 3
    segs = [Segment(start=i * 5.0, end=(i + 1) * 5.0, text=t)
            for i, t in enumerate(sentences)]
    result = Result.make_success(
        VideoRef(platform="youtube", id=VID, url=f"https://youtu.be/{VID}", source="url"),
        provenance=Provenance.platform_auto, text=" ".join(sentences), segments=segs,
        language=Language(requested=["en"], selected="en"),
        timestamp_type=TimestampType.caption_cue, raw_text=" ".join(sentences))

    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")

    async def pull(ref, policy, cache):
        return result

    meta = {VID: CorpusVideo(id=VID, url=f"https://youtu.be/{VID}", title="EP Alpha",
                             upload_date="2026-07-14", channel_title="Moonshots")}
    asyncio.run(corpus_add(store, "pod", [result.video_ref], policy=Policy(),
                           meta=meta, pull=pull))
    return store


def test_episode_document_is_deterministic_and_shared_across_chunks(tmp_path):
    store = _seed(tmp_path)
    record = store.load_record("pod", VID)
    doc = episode_document(record)
    assert doc.startswith("Episode: EP Alpha\nDate: 2026-07-14\nChannel: Moonshots")
    assert "doubles every eighteen months" in doc

    from transcript_tool.retrieval.chunk import chunk_record
    chunks = chunk_record(record, CFG, token_counter=_tokens)
    client = FakeContextClient()
    blurbs = generate_contexts(record, chunks, client)
    assert len(blurbs) == len(chunks)
    # Identical episode doc every call — the prompt-cache contract (§7).
    assert {c[0] for c in client.calls} == {doc}
    # And the chunk prompt embeds the verbatim chunk text.
    assert chunk_prompt(chunks[0].text).count(chunks[0].text) == 1


def test_context_flows_to_search_text_but_never_into_quoted_text(tmp_path):
    store = _seed(tmp_path)
    client = FakeContextClient()
    from transcript_tool.retrieval.context import default_contextizer
    report = corpus_build(store, "pod", embedder=FakeEmbedder(), chunk_config=CFG,
                          token_counter=_tokens, use_context=True,
                          contextizer=default_contextizer(client))
    assert report["versions"]["context_version"] == CONTEXT_VERSION
    assert store.entry("pod", VID)["context_version"] == CONTEXT_VERSION

    index = ChunkIndex(store.index_dir("pod"))
    rows = index.search_dense(FakeEmbedder().embed(["zanzibar"])[0], n=2)
    top = rows[0]
    assert "zanzibar" in top["search_text"]        # blurb is indexed...
    assert "zanzibar" not in top["text"]           # ...but never in the quotable text
    assert top["context"].startswith("From EP Alpha")

    # The blurb is lexically retrievable too (BM25 over search_text).
    trace = retrieve(index, "zanzibar", embedder=FakeEmbedder())
    assert trace.hits and "zanzibar" in trace.hits[0].chunk["search_text"]


def test_toggling_context_is_a_version_change_that_rebuilds(tmp_path):
    store = _seed(tmp_path)
    corpus_build(store, "pod", embedder=FakeEmbedder(), chunk_config=CFG,
                 token_counter=_tokens)
    assert store.entry("pod", VID)["context_version"] is None

    from transcript_tool.retrieval.context import default_contextizer
    report = corpus_build(store, "pod", embedder=FakeEmbedder(), chunk_config=CFG,
                          token_counter=_tokens, use_context=True,
                          contextizer=default_contextizer(FakeContextClient()))
    assert report["built"] == 1                    # context toggle => stale => rebuild

    report = corpus_build(store, "pod", embedder=FakeEmbedder(), chunk_config=CFG,
                          token_counter=_tokens, use_context=True,
                          contextizer=default_contextizer(FakeContextClient()))
    assert report["built"] == 0 and report["unchanged"] == 1


def test_no_context_is_the_private_path(tmp_path):
    """--no-context: no contextizer runs, nothing needs a key, context stays null."""
    store = _seed(tmp_path)
    report = corpus_build(store, "pod", embedder=FakeEmbedder(), chunk_config=CFG,
                          token_counter=_tokens, use_context=False)
    assert report["versions"]["context_version"] is None
    index = ChunkIndex(store.index_dir("pod"))
    rows = index.search_dense(FakeEmbedder().embed(["desalination"])[0], n=1)
    assert rows[0]["context"] is None
    assert rows[0]["search_text"] == rows[0]["text"]


def test_wrong_blurb_count_fails_closed(tmp_path):
    store = _seed(tmp_path)

    def broken(record, chunks):
        return ["only one"]

    with pytest.raises(ValueError):
        corpus_build(store, "pod", embedder=FakeEmbedder(), chunk_config=CFG,
                     token_counter=_tokens, use_context=True, contextizer=broken)
