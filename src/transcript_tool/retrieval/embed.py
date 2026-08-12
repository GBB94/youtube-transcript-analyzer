"""Embeddings — pluggable behind one interface, pinned by name+revision+dim (§8).

Default is a LOCAL sentence-embedding model: free, offline, private — no
transcript text leaves the machine. The API embedder is a config swap, not a
code change, because the index is rebuildable (§14); it is EGRESS and says so.
Mirrors the ASR model-pinning discipline: the embedder identity is part of the
index build tuple, so changing it is a rebuild, never a silent drift."""
from __future__ import annotations

from typing import Protocol

EMBED_BATCH = 64

# Pinned local default (BGE-small class, §8/§17). `revision` is recorded into
# build.json — pin a specific model revision here (or via the constructor) when
# reproducibility across machines matters.
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_LOCAL_REVISION = "main"


class Embedder(Protocol):
    """`name`, `revision`, and `dim` identify the embedding space; two embedders
    with different identity never share a table."""
    name: str
    revision: str

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def embedder_id(e: Embedder) -> str:
    return f"{e.name}@{e.revision}#{e.dim}"


class LocalSentenceEmbedder:
    """sentence-transformers, lazily imported (optional `retrieval-local` extra).
    The model must be provisioned out of band — never downloaded mid-build
    (the provisioning rule from the acquisition half applies here too)."""

    def __init__(self, model_name: str = DEFAULT_LOCAL_MODEL,
                 revision: str = DEFAULT_LOCAL_REVISION):
        self.name = model_name
        self.revision = revision
        self._model = None
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = self._load().get_sentence_embedding_dimension()
        return self._dim

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # lazy
            self._model = SentenceTransformer(self.name, revision=self.revision)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            vecs = model.encode(texts[i:i + EMBED_BATCH], normalize_embeddings=True,
                                show_progress_bar=False)
            out.extend(v.tolist() for v in vecs)
        return out


class VoyageEmbedder:
    """API embedder (optional upgrade, §8). EGRESS: sends chunk text to the
    provider. Only reach for it if the eval harness says recall is embedder-bound."""

    def __init__(self, model: str = "voyage-3.5-lite", dim: int = 1024):
        self.name = model
        self.revision = "api"
        self.dim = dim
        self._client = None

    def _load(self):
        if self._client is None:
            import voyageai  # lazy — optional extra
            self._client = voyageai.Client()
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        client = self._load()
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            resp = client.embed(texts[i:i + EMBED_BATCH], model=self.name,
                                output_dimension=self.dim)
            out.extend(resp.embeddings)
        return out


def get_embedder(kind: str) -> Embedder:
    if kind == "local":
        return LocalSentenceEmbedder()
    if kind == "api":
        return VoyageEmbedder()
    raise ValueError(f"unknown embedder kind: {kind!r} (expected 'local' or 'api')")
