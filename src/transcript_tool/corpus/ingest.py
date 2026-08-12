"""corpus add — ingest through the EXISTING pipeline (RETRIEVAL_DESIGN.md §2, §12).

Discovery hands us VideoRefs; each is pulled with the same staged `get_transcript`
the CLI uses, so the EgressPolicy gate and compliance posture are inherited
unchanged. Incremental by default: a video already in the manifest is skipped, and
a forced re-pull whose content_hash is unchanged is a no-op. A changed transcript
supersedes the old record and resets that video's derivation stamps only."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..cache import Cache
from ..orchestrator import get_transcript
from ..policy import Policy
from ..schema import Outcome, Result, VideoRef
from ..web import markdown as md
from ..web.parse import Target
from .records import CorpusVideo, content_hash, record_from_result
from .store import CorpusStore

PullFn = Callable[[VideoRef, Policy, Optional[Cache]], Awaitable[Result]]


@dataclass
class IngestReport:
    """What one `corpus add` run did — every id lands in exactly one bucket."""
    pulled: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)   # {target, outcome, reason}

    def as_dict(self) -> dict:
        return {"pulled": self.pulled, "superseded": self.superseded,
                "skipped_existing": self.skipped_existing, "unchanged": self.unchanged,
                "failed": self.failed}


def _render_markdown(ref: VideoRef, result: Result) -> str:
    """Readable layer via the existing renderer — reuse, not re-implementation."""
    target = Target(raw=ref.url or ref.id or "", video_id=ref.id or "", url=ref.url or "")
    return md.video_section(target, result)


async def corpus_add(store: CorpusStore, slug: str, refs: list[VideoRef], *,
                     policy: Policy, cache: Optional[Cache] = None,
                     meta: Optional[dict[str, CorpusVideo]] = None,
                     force: bool = False,
                     pull: Optional[PullFn] = None,
                     log: Callable[[str], None] = lambda _msg: None) -> IngestReport:
    """Pull only-new refs through the existing pipeline and bank the canonical
    records. `meta` carries discovery enrichment (title/upload_date/chapters) keyed
    by video id. `pull` is injectable so tests never touch the network."""
    pull = pull or get_transcript
    meta = meta or {}
    report = IngestReport()
    known = store.video_ids(slug)

    for ref in refs:
        video_id = ref.id or ""
        if not video_id:
            report.failed.append({"target": ref.url or str(ref.path), "outcome": "failed",
                                  "reason": "invalid_input"})
            continue
        if video_id in known and not force:
            report.skipped_existing.append(video_id)
            continue

        log(f"corpus add: pulling {video_id}")
        result = await pull(ref, policy, cache)
        if result.outcome is not Outcome.success:
            report.failed.append({"target": video_id, "outcome": result.outcome.value,
                                  "reason": result.reason.value if result.reason else None})
            continue

        existing = store.entry(slug, video_id)
        if existing and existing.get("content_hash") == content_hash(result.segments):
            report.unchanged.append(video_id)         # §12 content-hash guard
            continue

        video = meta.get(video_id) or CorpusVideo(
            id=video_id, url=ref.url or f"https://www.youtube.com/watch?v={video_id}")
        record = record_from_result(result, source_slug=slug, video=video)
        store.add_record(record, markdown=_render_markdown(ref, result))
        (report.superseded if existing else report.pulled).append(video_id)

    return report
