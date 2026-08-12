"""Contextual enrichment (§7, Anthropic Contextual Retrieval).

Before embedding, each chunk gets a short LLM-generated context line situating
it in its episode, so floating references ("he thinks it 10x's by 2027") become
searchable. The chunk is embedded and lexically indexed as context + text; the
verbatim `text` is preserved separately and is what gets quoted — we never cite
the synthesized context as if it were spoken.

Contracts: deterministic prompt, pinned model, CONTEXT_VERSION bumped on any
prompt/model change (a rebuild input, §14). This step is EGRESS — it sends
transcript text to a model provider. It runs against the operator's own key, is
disclosed, and is skippable (`corpus build --no-context`)."""
from __future__ import annotations

from typing import Optional, Protocol

from ..corpus.records import CorpusRecord
from .chunk import Chunk

CONTEXT_VERSION = "1.0.0"
# Cheap, pinned model (§7): blurbs are ~50-100 tokens; the episode body is
# prompt-cached so the transcript is read once per episode, not once per chunk.
CONTEXT_MODEL = "claude-haiku-4-5-20251001"
CONTEXT_MAX_TOKENS = 200
# Episode bodies beyond this are truncated (rare: ~4h of speech) so the cached
# system block stays inside the model context window.
MAX_EPISODE_CHARS = 300_000

_SYSTEM_PREFIX = (
    "You generate retrieval context for transcript chunks. "
    "The full episode transcript follows.\n\n<document>\n"
)
_SYSTEM_SUFFIX = "\n</document>"

_CHUNK_PROMPT = (
    "Here is the chunk we want to situate within the episode above:\n"
    "<chunk>\n{chunk}\n</chunk>\n"
    "Give a short succinct context (50-100 tokens) situating this chunk within "
    "the overall episode for the purposes of improving search retrieval of the "
    "chunk. Name the episode, the date, who or what is being discussed, and "
    "resolve pronouns where the episode makes them clear. Answer only with the "
    "succinct context and nothing else."
)


class ContextClient(Protocol):
    """One call per chunk; `episode_doc` is identical across an episode's chunks
    so the provider's prompt cache absorbs the transcript re-reads."""

    def contextualize(self, episode_doc: str, chunk_text: str) -> str: ...


def episode_document(record: CorpusRecord) -> str:
    """Deterministic episode body: metadata header + the full transcript."""
    v = record.video
    header = (f"Episode: {v.title or v.id}\n"
              f"Date: {v.upload_date or 'unknown'}\n"
              f"Channel: {v.channel_title or 'unknown'}\n\n")
    transcript = " ".join(s.text for s in record.segments)
    return (header + transcript)[:MAX_EPISODE_CHARS]


def chunk_prompt(chunk_text: str) -> str:
    return _CHUNK_PROMPT.format(chunk=chunk_text)


def generate_contexts(record: CorpusRecord, chunks: list[Chunk],
                      client: ContextClient) -> list[Optional[str]]:
    """One blurb per chunk, in order. A blurb failure raises rather than silently
    indexing an un-contextualized chunk under a context_version that claims
    otherwise."""
    doc = episode_document(record)
    return [client.contextualize(doc, c.text).strip() for c in chunks]


class AnthropicContextClient:
    """Default client (lazy `anthropic` import; ANTHROPIC_API_KEY). The episode
    document is sent as a cache_control system block: read once per episode."""

    def __init__(self, model: str = CONTEXT_MODEL):
        self.model = model
        self._client = None

    def _load(self):
        if self._client is None:
            try:
                import anthropic  # lazy — optional extra
            except ImportError as e:
                raise RuntimeError(
                    "contextual enrichment needs the 'answer' extra "
                    "(pip install '.[answer]') or pass --no-context") from e
            self._client = anthropic.Anthropic()
        return self._client

    def contextualize(self, episode_doc: str, chunk_text: str) -> str:
        client = self._load()
        resp = client.messages.create(
            model=self.model,
            max_tokens=CONTEXT_MAX_TOKENS,
            system=[{
                "type": "text",
                "text": _SYSTEM_PREFIX + episode_doc + _SYSTEM_SUFFIX,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": chunk_prompt(chunk_text)}],
        )
        return "".join(block.text for block in resp.content
                       if getattr(block, "type", "") == "text")


def default_contextizer(client: Optional[ContextClient] = None):
    """What `corpus build` uses when context is on (the default posture once a
    key is configured). Returns a (record, chunks) -> blurbs callable."""
    resolved = client or AnthropicContextClient()

    def contextize(record: CorpusRecord, chunks: list[Chunk]) -> list[Optional[str]]:
        return generate_contexts(record, chunks, resolved)

    return contextize
