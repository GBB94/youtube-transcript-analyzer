"""R5 — grounded answering (§10, §19). The answer client is injected, so tests
drive every outcome deterministically: grounded with validated citations,
insufficient evidence citing nothing, fabricated / expansion-only citations
rejected, expansion widening sight but never citations, provenance preserved."""
import asyncio

import pytest

from transcript_tool.corpus.ingest import corpus_add
from transcript_tool.corpus.records import CorpusVideo
from transcript_tool.corpus.store import CorpusStore
from transcript_tool.policy import Policy
from transcript_tool.retrieval.answer import Answer, ask_sync
from transcript_tool.retrieval.build import corpus_build
from transcript_tool.retrieval.chunk import ChunkConfig
from transcript_tool.retrieval.retrieve import Filters, RetrieveConfig
from transcript_tool.schema import (
    Language, Provenance, Result, Segment, TimestampType, VideoRef,
)

from test_index_build import FakeEmbedder, _tokens

VID = "aaaaaaaaaaa"
CFG = ChunkConfig(max_tokens=30, min_tokens=8, overlap_ratio=0.1)

SENTS = ["the housing market is softening in coastal cities this year.",
         "compute is becoming a tradeable commodity like oil futures.",
         "we discussed longevity escape velocity and clinical trials.",
         "robotaxis are scaling faster than anyone projected last fall.",
         "the guest argued education needs one on one ai tutors now.",
         "fusion startups raised record capital again this quarter."] * 2


class ScriptedClient:
    """Returns a preset verdict; records the evidence it saw."""
    def __init__(self, verdict):
        self.verdict = verdict
        self.seen = None

    def answer(self, query, evidence):
        self.seen = (query, evidence)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


@pytest.fixture()
def corpus(tmp_path):
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    segs = [Segment(start=i * 5.0, end=(i + 1) * 5.0, text=t)
            for i, t in enumerate(SENTS)]
    result = Result.make_success(
        VideoRef(platform="youtube", id=VID,
                 url=f"https://www.youtube.com/watch?v={VID}", source="url"),
        provenance=Provenance.platform_auto, text=" ".join(SENTS), segments=segs,
        language=Language(requested=["en"], selected="en"),
        timestamp_type=TimestampType.caption_cue, raw_text=" ".join(SENTS))

    async def pull(ref, policy, cache):
        return result

    meta = {VID: CorpusVideo(id=VID, url=f"https://www.youtube.com/watch?v={VID}",
                             title="EP 278", upload_date="2026-07-14")}
    asyncio.run(corpus_add(store, "pod", [result.video_ref], policy=Policy(),
                           meta=meta, pull=pull))
    corpus_build(store, "pod", embedder=FakeEmbedder(), chunk_config=CFG,
                 token_counter=_tokens)
    return store


def _ask(store, client, query="compute tradeable commodity", **kw):
    return ask_sync(query, store=store, slug="pod", embedder=FakeEmbedder(),
                    client=client, **kw)


def _first_retrieved(store, client_verdict=None, query="compute tradeable commodity"):
    probe = ScriptedClient({"insufficient_evidence": True})
    _ask(store, probe, query=query)
    return probe


def test_grounded_answer_carries_validated_deep_link_citations(corpus):
    probe = ScriptedClient({"insufficient_evidence": True})
    _ask(corpus, probe)
    retrieved_id = None
    # Recover a real retrieved id from the evidence the client saw.
    for line in probe.seen[1].splitlines():
        if line.startswith("[chunk_id="):
            retrieved_id = line.split("chunk_id=")[1].split(" ")[0]
            break
    assert retrieved_id

    client = ScriptedClient({"answer": "They argue compute becomes tradeable like oil.",
                             "citations": [retrieved_id]})
    answer = _ask(corpus, client)
    assert answer.answer_outcome == "grounded"
    c = answer.citations[0]
    assert c.chunk_id == retrieved_id and c.video_id == VID
    assert c.url_with_timestamp.endswith(f"&t={int(c.start_s)}s")
    assert c.provenance == "platform_auto"
    # The quote is the verbatim chunk text (never the context blurb).
    assert c.quote and "compute" in c.quote
    assert retrieved_id in answer.retrieval["retrieved_chunk_ids"]


def test_insufficient_evidence_cites_nothing(corpus):
    client = ScriptedClient({"insufficient_evidence": True})
    answer = _ask(corpus, client)
    assert answer.answer_outcome == "insufficient_evidence"
    assert answer.citations == [] and answer.answer is None
    # Retrieval trace still present for audit.
    assert answer.retrieval["retrieved_chunk_ids"]


def test_no_hits_is_insufficient_without_calling_the_model(corpus):
    client = ScriptedClient({"answer": "should never be seen", "citations": []})
    answer = _ask(corpus, client, filters=Filters(video_id="zzzzzzzzzzz"))
    assert answer.answer_outcome == "insufficient_evidence"
    assert client.seen is None                 # the model was never consulted


def test_fabricated_citation_is_rejected_not_shipped(corpus):
    client = ScriptedClient({"answer": "made up",
                             "citations": [f"{VID}:9999"]})
    answer = _ask(corpus, client)
    assert answer.answer_outcome == "failed"
    assert "citation_rejected" in answer.reason and answer.citations == []


def test_expansion_widens_sight_but_never_citations(corpus):
    # k=2 on a bigger corpus guarantees some neighbours are expansion-only.
    small = RetrieveConfig(k=2)
    probe = ScriptedClient({"insufficient_evidence": True})
    _ask(corpus, probe, retrieve_config=small)
    evidence = probe.seen[1]
    # The answerer sees neighbour context...
    assert "(context, not citable)" in evidence
    assert "<citable-span>" in evidence

    # ...but citing an expansion-only neighbour fails validation (§19).
    answer_obj = _ask(corpus, probe, retrieve_config=small)
    expansion_only = None
    for hit in answer_obj.retrieval["hits"]:
        for nid in hit["expanded_with"]:
            if nid not in answer_obj.retrieval["retrieved_chunk_ids"]:
                expansion_only = nid
                break
        if expansion_only:
            break
    assert expansion_only is not None
    client = ScriptedClient({"answer": "cites a neighbour",
                             "citations": [expansion_only]})
    answer = _ask(corpus, client, retrieve_config=small)
    assert answer.answer_outcome == "failed" and "citation_rejected" in answer.reason


def test_grounded_without_citations_fails_validation(corpus):
    client = ScriptedClient({"answer": "uncited claim", "citations": []})
    answer = _ask(corpus, client)
    assert answer.answer_outcome == "failed"


def test_provider_error_is_an_operational_failure(corpus):
    client = ScriptedClient(RuntimeError("boom"))
    answer = _ask(corpus, client)
    assert answer.answer_outcome == "failed" and "provider_error" in answer.reason


def test_missing_index_is_failed_not_a_guess(tmp_path):
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    answer = ask_sync("anything", store=store, slug="none", embedder=FakeEmbedder(),
                      client=ScriptedClient({}))
    assert answer.answer_outcome == "failed" and answer.reason == "index_missing"


def test_answer_model_enforces_invariants_directly():
    with pytest.raises(ValueError):
        Answer(answer_outcome="grounded", answer="x", citations=[])
    with pytest.raises(ValueError):
        Answer(answer_outcome="failed")            # failed requires a reason
    with pytest.raises(ValueError):
        Answer(answer_outcome="insufficient_evidence",
               citations=[{"chunk_id": "v:0000", "video_id": "v", "start_s": 1.0,
                           "url_with_timestamp": "u", "provenance": "local_asr",
                           "quote": "q"}])


def test_ask_sync_guards_against_running_loop(corpus):
    async def call():
        return ask_sync("q", store=corpus, slug="pod", embedder=FakeEmbedder(),
                        client=ScriptedClient({}))
    with pytest.raises(RuntimeError):
        asyncio.run(call())


def test_versions_ride_along_in_the_trace(corpus):
    client = ScriptedClient({"insufficient_evidence": True})
    answer = _ask(corpus, client)
    versions = answer.retrieval["versions"]
    assert versions["chunker_version"] and versions["embed_model"] == "fake-bow@1#32"
