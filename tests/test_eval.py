"""R6 — the evaluation harness (§13): recall@k / MRR against a labelled golden
set, optional LLM-as-judge faithfulness via injection, and the --compare
regression gate."""
import asyncio
import json

import pytest

from transcript_tool.corpus.ingest import corpus_add
from transcript_tool.corpus.records import CorpusVideo
from transcript_tool.corpus.store import CorpusStore
from transcript_tool.policy import Policy
from transcript_tool.retrieval.answer import ask_sync
from transcript_tool.retrieval.build import corpus_build
from transcript_tool.retrieval.chunk import ChunkConfig
from transcript_tool.retrieval.evalharness import (
    GoldenQuestion, chunk_answers, compare, format_compare_table, load_golden, run_eval,
)
from transcript_tool.schema import (
    Language, Provenance, Result, Segment, TimestampType, VideoRef,
)

from test_index_build import FakeEmbedder, _tokens

VID_A, VID_B = "aaaaaaaaaaa", "bbbbbbbbbbb"
CFG = ChunkConfig(max_tokens=30, min_tokens=8, overlap_ratio=0.1)

SENTS_A = ["the housing market is softening in coastal cities this year.",
           "compute is becoming a tradeable commodity like oil futures.",
           "we discussed longevity escape velocity and clinical trials.",
           "robotaxis are scaling faster than anyone projected last fall."] * 2
SENTS_B = ["fusion startups raised record capital again this quarter.",
           "desalination costs collapse with the new membrane technology.",
           "education needs one on one tutors powered by language models.",
           "quantum error correction crossed the usable threshold."] * 2


@pytest.fixture()
def corpus(tmp_path):
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    results = {VID_A: _make(VID_A, SENTS_A), VID_B: _make(VID_B, SENTS_B)}

    async def pull(ref, policy, cache):
        return results[ref.id]

    meta = {v: CorpusVideo(id=v, url=f"https://www.youtube.com/watch?v={v}",
                           title=f"EP {v}", upload_date="2026-07-14")
            for v in results}
    refs = [results[v].video_ref for v in (VID_A, VID_B)]
    asyncio.run(corpus_add(store, "pod", refs, policy=Policy(), meta=meta, pull=pull))
    corpus_build(store, "pod", embedder=FakeEmbedder(), chunk_config=CFG,
                 token_counter=_tokens)
    return store


def _make(vid, sentences):
    segs = [Segment(start=i * 5.0, end=(i + 1) * 5.0, text=t)
            for i, t in enumerate(sentences)]
    return Result.make_success(
        VideoRef(platform="youtube", id=vid,
                 url=f"https://www.youtube.com/watch?v={vid}", source="url"),
        provenance=Provenance.platform_auto, text=" ".join(sentences), segments=segs,
        language=Language(requested=["en"], selected="en"),
        timestamp_type=TimestampType.caption_cue, raw_text=" ".join(sentences))


GOLDEN = [
    # "compute ... tradeable commodity" is sentence 2 of A: ~5-10s.
    GoldenQuestion(id="q1", question="compute becoming a tradeable commodity",
                   answers=[{"video_id": VID_A, "timestamp_s": 7.0}]),
    # desalination is sentence 2 of B: ~5-10s.
    GoldenQuestion(id="q2", question="desalination membrane cost collapse",
                   answers=[{"video_id": VID_B, "timestamp_s": 7.0}]),
    # Unanswerable: nothing about gardening in the corpus.
    GoldenQuestion(id="q3", question="heirloom tomato gardening advice",
                   answers=[{"video_id": VID_A, "timestamp_s": 9990.0}]),
]


def test_chunk_answers_tolerance_window():
    chunk = {"video_id": VID_A, "start_s": 100.0, "end_s": 130.0}
    assert chunk_answers(chunk, [{"video_id": VID_A, "timestamp_s": 125.0}])
    assert chunk_answers(chunk, [{"video_id": VID_A, "timestamp_s": 75.0}])   # within 30s
    assert not chunk_answers(chunk, [{"video_id": VID_A, "timestamp_s": 40.0}])
    assert not chunk_answers(chunk, [{"video_id": VID_B, "timestamp_s": 110.0}])


def test_retrieval_metrics_are_deterministic(corpus):
    report = run_eval(corpus, "pod", GOLDEN, embedder=FakeEmbedder(), k=4)
    assert report.n_questions == 3
    # q1 and q2 hit; the gardening question misses.
    by_id = {row["id"]: row for row in report.per_question}
    assert by_id["q1"]["hit"] and by_id["q2"]["hit"] and not by_id["q3"]["hit"]
    assert report.recall_at_k == pytest.approx(2 / 3)
    assert report.mrr == pytest.approx(
        (1 / by_id["q1"]["first_rank"] + 1 / by_id["q2"]["first_rank"]) / 3)
    assert report.versions.get("embed_model") == "fake-bow@1#32"

    again = run_eval(corpus, "pod", GOLDEN, embedder=FakeEmbedder(), k=4)
    assert again.as_dict() == report.as_dict()


def test_golden_set_roundtrips_from_json(tmp_path):
    path = tmp_path / "golden.json"
    path.write_text(json.dumps({"questions": [
        {"id": "q1", "question": "x", "answers": [{"video_id": VID_A, "timestamp_s": 1.0}],
         "time_sensitive": True}]}))
    loaded = load_golden(path)
    assert loaded[0].time_sensitive and loaded[0].answers[0]["video_id"] == VID_A
    path.write_text(json.dumps({"questions": []}))
    with pytest.raises(ValueError):
        load_golden(path)


def test_faithfulness_judge_runs_only_on_grounded_answers(corpus):
    class Judge:
        def __init__(self):
            self.calls = []

        def judge(self, question, passages, answer, cited_quotes):
            self.calls.append(question)
            return {"supported": True, "citations_correct": True}

    class GroundingClient:
        """Grounds q1/q2 by citing the first retrieved chunk; punts on q3."""
        def answer(self, query, evidence):
            if "tomato" in query:
                return {"insufficient_evidence": True}
            cid = evidence.split("chunk_id=")[1].split(" ")[0]
            return {"answer": "grounded answer", "citations": [cid]}

    judge = Judge()

    def answer_fn(q):
        return ask_sync(q.question, store=corpus, slug="pod",
                        embedder=FakeEmbedder(), client=GroundingClient())

    report = run_eval(corpus, "pod", GOLDEN, embedder=FakeEmbedder(), k=4,
                      answer_fn=answer_fn, judge=judge)
    assert report.faithfulness["n_grounded"] == 2
    assert report.faithfulness["supported_rate"] == 1.0
    assert len(judge.calls) == 2                   # never judged the insufficient one


def test_compare_gate_flags_regressions_past_threshold():
    baseline = {"recall_at_k": 0.9, "mrr": 0.7}
    ok = compare(baseline, {"recall_at_k": 0.88, "mrr": 0.75}, threshold=0.05)
    assert ok["ok"] and ok["regressions"] == []

    bad = compare(baseline, {"recall_at_k": 0.7, "mrr": 0.69}, threshold=0.05)
    assert not bad["ok"] and bad["regressions"] == ["recall_at_k"]
    table = format_compare_table(bad)
    assert "REGRESSION" in table and "recall_at_k" in table
