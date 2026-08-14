"""Hybrid two-stage retrieval (§9): retrieve wide and cheap, rerank narrow and
precise.

Stage 1 is LanceDB's built-in hybrid query over the ONE store — dense (we supply
the query vector, so the embedder stays injectable) + native FTS BM25, fused
with the built-in RRF reranker (§20.4). Stage 2 is a cross-encoder scoring the
fused candidates; keep top k. Metadata filters are first-class and apply BEFORE
ranking (prefilter), shrinking the search space rather than trimming results.

Defaults are config, set and moved by the eval harness (§13) — never hardcoded
folklore. No HyDE, no keyword expansion: the query is embedded as asked (§20.6)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from .embed import Embedder
from .index import ChunkIndex, _sql_str

DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-base"


@dataclass(frozen=True)
class RetrieveConfig:
    n_candidates: int = 50        # hybrid fan-out (per §9, N≈50)
    k: int = 8                    # what the answerer sees
    rerank: bool = True


class Reranker(Protocol):
    name: str
    revision: str

    def score(self, query: str, texts: list[str]) -> list[float]: ...


class LocalCrossEncoder:
    """Cross-encoder reranker (BGE-reranker class), lazily imported. Provisioned
    out of band like every other model — never downloaded mid-query."""
    def __init__(self, model_name: str = DEFAULT_RERANKER_MODEL, revision: str = "main"):
        self.name = model_name
        self.revision = revision
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder  # lazy
            self._model = CrossEncoder(self.name, revision=self.revision)
        return self._model

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self._load().predict([(query, t) for t in texts])]


@dataclass
class Filters:
    """Structured metadata constraints (§9). Date bounds compare ISO strings;
    every other value is restricted to the id alphabet before interpolation."""
    source_slug: Optional[str] = None
    video_id: Optional[str] = None
    since: Optional[str] = None            # upload_date >= since (YYYY-MM-DD)
    until: Optional[str] = None
    speaker: Optional[str] = None
    provenance: Optional[str] = None

    def to_where(self) -> Optional[str]:
        clauses = []
        for column, value in (("source_slug", self.source_slug),
                              ("video_id", self.video_id),
                              ("speaker", self.speaker),
                              ("provenance", self.provenance)):
            if value is not None:
                clauses.append(f"{column} = {_sql_str(value)}")
        if self.since is not None:
            clauses.append(f"upload_date >= {_sql_str(self.since)}")
        if self.until is not None:
            clauses.append(f"upload_date <= {_sql_str(self.until)}")
        return " AND ".join(clauses) or None


@dataclass
class Retrieved:
    """One candidate that survived the funnel. `chunk` is the full stored row
    (ChunkMeta + text + context) — a hit needs no join."""
    chunk: dict
    fused_rank: int                        # 1-based rank out of stage 1
    fused_score: float
    rerank_score: Optional[float] = None


@dataclass
class RetrievalTrace:
    """What the funnel did — auditable, and the input to citation validation."""
    query: str
    where: Optional[str]
    n_candidates: int
    reranked: bool
    reranker: Optional[str] = None
    hits: list[Retrieved] = field(default_factory=list)

    def chunk_ids(self) -> set[str]:
        return {h.chunk["chunk_id"] for h in self.hits}


def merge_traces(traces: list[RetrievalTrace], k: int) -> RetrievalTrace:
    """Merge per-source traces into one ranked list (multi-corpus queries).

    Sound because every hit keeps its source attribution and the merge key is
    comparable across indexes: when every trace was reranked, the cross-encoder
    score is a query-passage judgment independent of which index produced the
    candidate; without rerank, hits interleave by per-index fused rank (RRF
    ranks are per-index and scores are NOT cross-index comparable, so rank is
    the honest key). Never merges what it cannot order: a mix of reranked and
    un-reranked traces is refused."""
    live = [t for t in traces if t is not None]
    if not live:
        raise ValueError("nothing to merge")
    rerank_states = {t.reranked for t in live}
    if len(rerank_states) > 1:
        raise ValueError("cannot merge reranked and un-reranked traces")
    reranked = rerank_states.pop()

    hits = [h for t in live for h in t.hits]
    if reranked:
        hits.sort(key=lambda h: (-(h.rerank_score or 0.0), h.fused_rank))
    else:
        hits.sort(key=lambda h: (h.fused_rank, -h.fused_score))

    merged = RetrievalTrace(
        query=live[0].query, where=live[0].where,
        n_candidates=sum(t.n_candidates for t in live),
        reranked=reranked, reranker=live[0].reranker)
    merged.hits = hits[:k]
    return merged


def retrieve(index: ChunkIndex, query: str, *, embedder: Embedder,
             reranker: Optional[Reranker] = None,
             filters: Optional[Filters] = None,
             config: RetrieveConfig = RetrieveConfig()) -> RetrievalTrace:
    where = filters.to_where() if filters else None
    tbl = index.table()
    trace = RetrievalTrace(query=query, where=where, n_candidates=config.n_candidates,
                           reranked=False)
    if tbl is None:
        return trace

    from lancedb.rerankers import RRFReranker  # lazy with the rest of lancedb

    vector = embedder.embed([query])[0]
    q = (tbl.search(query_type="hybrid", vector_column_name="vector")
            .vector(vector).text(query)
            .limit(config.n_candidates).rerank(RRFReranker()))
    if where:
        q = q.where(where, prefilter=True)
    fused = q.to_list()

    hits = [Retrieved(chunk={k: v for k, v in row.items()
                             if k not in ("vector", "_relevance_score")},
                      fused_rank=i + 1,
                      fused_score=float(row.get("_relevance_score", 0.0)))
            for i, row in enumerate(fused)]

    if config.rerank and reranker is not None and hits:
        scores = reranker.score(query, [h.chunk["search_text"] for h in hits])
        for h, s in zip(hits, scores, strict=True):
            h.rerank_score = s
        hits.sort(key=lambda h: (-(h.rerank_score or 0.0), h.fused_rank))
        trace.reranked = True
        trace.reranker = f"{reranker.name}@{reranker.revision}"

    trace.hits = hits[:config.k]
    return trace
