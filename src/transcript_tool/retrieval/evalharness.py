"""Evaluation harness (§13) — measure retrieval, don't vibe-check it.

The golden set is hand-authored questions labelled with the video_id + rough
timestamp that answers them. Retrieval metrics (recall@k, MRR) are cheap,
deterministic, and LLM-free — the primary gate, because a generated answer can
only be as good as what retrieval surfaced. Answer faithfulness is an optional
LLM-as-judge pass over generated answers. `--compare` diffs two runs per metric
and fails the build on regression past a threshold — the jiwer-gate analogue."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

from ..corpus.store import CorpusStore
from .embed import Embedder
from .index import ChunkIndex
from .retrieve import Filters, Reranker, RetrieveConfig, retrieve

# A labelled timestamp within this many seconds of a chunk's span counts as
# that chunk answering the question ("rough timestamp", §13).
TIMESTAMP_TOLERANCE_S = 30.0
DEFAULT_REGRESSION_THRESHOLD = 0.05


@dataclass
class GoldenQuestion:
    id: str
    question: str
    answers: list[dict]                   # [{"video_id": ..., "timestamp_s": ...}]
    time_sensitive: bool = False
    filters: Optional[dict] = None        # optional structured constraints


def load_golden(path: Path) -> list[GoldenQuestion]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    questions = []
    for q in payload["questions"]:
        questions.append(GoldenQuestion(
            id=q["id"], question=q["question"], answers=q["answers"],
            time_sensitive=q.get("time_sensitive", False),
            filters=q.get("filters")))
    if not questions:
        raise ValueError(f"golden set at {path} has no questions")
    return questions


def chunk_answers(chunk: dict, answers: list[dict],
                  tolerance_s: float = TIMESTAMP_TOLERANCE_S) -> bool:
    for a in answers:
        if (chunk["video_id"] == a["video_id"]
                and chunk["start_s"] - tolerance_s <= a["timestamp_s"]
                and a["timestamp_s"] <= chunk["end_s"] + tolerance_s):
            return True
    return False


class JudgeClient(Protocol):
    """LLM-as-judge for answer faithfulness: given question, passages, and the
    generated answer, say whether the answer is supported and its citations
    correct."""

    def judge(self, question: str, passages: list[str], answer: str,
              cited_quotes: list[str]) -> dict: ...


@dataclass
class EvalReport:
    slug: str
    k: int
    n_questions: int
    recall_at_k: float
    mrr: float
    per_question: list[dict] = field(default_factory=list)
    versions: dict = field(default_factory=dict)
    faithfulness: Optional[dict] = None

    def as_dict(self) -> dict:
        out = {"slug": self.slug, "k": self.k, "n_questions": self.n_questions,
               "recall_at_k": round(self.recall_at_k, 4), "mrr": round(self.mrr, 4),
               "per_question": self.per_question, "versions": self.versions}
        if self.faithfulness is not None:
            out["faithfulness"] = self.faithfulness
        return out


def run_eval(store: CorpusStore, slug: str, golden: list[GoldenQuestion], *,
             embedder: Embedder, reranker: Optional[Reranker] = None, k: int = 8,
             answer_fn: Optional[Callable[[GoldenQuestion], "object"]] = None,
             judge: Optional[JudgeClient] = None,
             log: Callable[[str], None] = lambda _msg: None) -> EvalReport:
    """Retrieval metrics always; faithfulness only when both an answer_fn (a
    question -> Answer callable, usually wrapping ask()) and a judge are given."""
    index = ChunkIndex(store.index_dir(slug))
    config = RetrieveConfig(k=k)

    hits_at_k = 0
    reciprocal_ranks: list[float] = []
    per_question: list[dict] = []
    judged: list[dict] = []

    for q in golden:
        filters = Filters(**q.filters) if q.filters else None
        trace = retrieve(index, q.question, embedder=embedder, reranker=reranker,
                         filters=filters, config=config)
        first_rank = None
        for rank, hit in enumerate(trace.hits, start=1):
            if chunk_answers(hit.chunk, q.answers):
                first_rank = rank
                break
        hit = first_rank is not None
        hits_at_k += int(hit)
        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
        row = {"id": q.id, "hit": hit, "first_rank": first_rank,
               "time_sensitive": q.time_sensitive}
        log(f"eval: {q.id} -> {'hit@' + str(first_rank) if hit else 'MISS'}")

        if answer_fn is not None and judge is not None:
            answer = answer_fn(q)
            verdict = {"outcome": answer.answer_outcome}
            if answer.answer_outcome == "grounded":
                verdict.update(judge.judge(
                    q.question,
                    [h.chunk["text"] for h in trace.hits],
                    answer.answer,
                    [c.quote for c in answer.citations]))
            judged.append(verdict)
            row["faithfulness"] = verdict
        per_question.append(row)

    n = len(golden)
    faithfulness = None
    if judged:
        graded = [j for j in judged if j["outcome"] == "grounded"]
        faithfulness = {
            "n_grounded": len(graded),
            "supported_rate": (sum(1 for j in graded if j.get("supported"))
                               / len(graded)) if graded else None,
            "citations_correct_rate": (sum(1 for j in graded if j.get("citations_correct"))
                                       / len(graded)) if graded else None,
        }

    return EvalReport(slug=slug, k=k, n_questions=n,
                      recall_at_k=hits_at_k / n,
                      mrr=sum(reciprocal_ranks) / n,
                      per_question=per_question,
                      versions=(index.read_build() or {}),
                      faithfulness=faithfulness)


def compare(baseline: dict, candidate: dict, *,
            threshold: float = DEFAULT_REGRESSION_THRESHOLD) -> dict:
    """Per-metric diff. A metric that drops by more than `threshold` is a
    regression and fails the run (§13). Returns {rows, regressions, ok}."""
    rows = []
    regressions = []
    for metric in ("recall_at_k", "mrr"):
        base, cand = baseline.get(metric), candidate.get(metric)
        delta = None if base is None or cand is None else round(cand - base, 4)
        regressed = delta is not None and delta < -threshold
        rows.append({"metric": metric, "baseline": base, "candidate": cand,
                     "delta": delta, "regressed": regressed})
        if regressed:
            regressions.append(metric)
    return {"rows": rows, "regressions": regressions, "ok": not regressions,
            "threshold": threshold}


def format_compare_table(result: dict) -> str:
    lines = [f"{'metric':<12} {'baseline':>9} {'candidate':>9} {'delta':>8}"]
    for row in result["rows"]:
        flag = "  << REGRESSION" if row["regressed"] else ""
        lines.append(f"{row['metric']:<12} {row['baseline']!s:>9} "
                     f"{row['candidate']!s:>9} {row['delta']!s:>8}{flag}")
    return "\n".join(lines)
