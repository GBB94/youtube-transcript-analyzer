"""Job worker (docs/UI_SCOPE.md §6). Runs in a SEPARATE process from the web server
(heavy ASR must not be an in-process task) and drains a job's queued items, writing
status to the shared SQLite store. The web process streams those changes over SSE.

`process_job(db_path, job_id)` is the spawn entry point (picklable args; the default
engine pull is used in the child). Tests call `process_job(..., pull=fake)` directly for
determinism without spawning."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..schema import Outcome, Reason, Result
from .copy import badge_for, message_for, retry_allowed
from .jobs import JobStore
from .markdown import video_section
from .parse import Target

PullFn = Callable[[Target], Result]


def default_pull(target: Target) -> Result:
    """Captions-first pull via the engine (no ASR in UI-1/2). Public-URL egress is on:
    running the local app + pasting links is the operator's acknowledgment."""
    from ..cache import Cache
    from ..orchestrator import get_transcript_sync
    from ..policy import EgressPolicy, Policy
    policy = Policy(
        enabled_strategies=("api_captions", "ytdlp_subs"),
        egress=EgressPolicy(allow_network=True, allow_public_url=True),
    )
    cache = Cache(Path("~/.cache/transcript-tool").expanduser())
    return get_transcript_sync(target.ref(), policy, cache)


def _process_item(store: JobStore, job_id: str, item: dict, pull: PullFn) -> None:
    target = Target(raw=item["raw"], video_id=item["video_id"], url=item["url"])
    try:
        result = pull(target)
    except Exception:                       # one failed item never blocks the batch
        result = Result.make_failed(target.ref(), Reason.provider_error)
    if result.outcome is Outcome.success:
        store.complete_item(job_id, item["idx"], badge=badge_for(result),
                            words=result.word_count, md_section=video_section(target, result))
    else:
        store.fail_item(job_id, item["idx"],
                        message=message_for(result), retry=retry_allowed(result))


def process_job(db_path: str, job_id: str, pull: Optional[PullFn] = None) -> None:
    """Drain every queued item in the job, then finalize. Safe to run as the spawn
    target in a child process."""
    store = JobStore(db_path)
    pull = pull or default_pull
    while True:
        item = store.claim_next_queued(job_id)
        if item is None:
            break
        _process_item(store, job_id, item, pull)
    store.finalize_if_done(job_id)
