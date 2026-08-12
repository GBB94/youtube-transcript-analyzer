# Review punch list — 2026-08-12 code & spec review

Findings from the full-tree review (specs, docs, packaging, source, tests).
Spec-level items were folded directly into `RETRIEVAL_DESIGN.md` (§20.4–20.6) and
are not repeated here — this file is the **code** punch list for the built
acquisition half. Work an item, check it off, note the commit.

Severity: **H** = fix before building R0 on top · **M** = fix soon · **L** = opportunistic.

## Correctness

- [x] **H — Web job durability is not what it claims.** *(fixed: `JobStore.recover_orphans` + startup relaunch in `create_app`)* If the worker process dies
  mid-item, the item stays `running` forever and nothing on startup requeues
  `queued` items of unfinished jobs (`web/worker.py:60-75`, `web/jobs.py`). The
  README's "survives a refresh or restart" holds for the render only. Fix: on
  `create_app`/worker start, requeue orphaned `running` items and resume `queued`
  items of unfinished jobs.
- [x] **H — SSE endpoint loops forever on unknown job ids.** *(fixed: 404 on unknown job; SQLite via `asyncio.to_thread`)* `job_events`
  (`web/app.py:133-150`) never checks the job exists; total=0 means `finished` is
  never true, so each bad connection polls SQLite every 0.3 s indefinitely. Also
  calls synchronous SQLite (30 s busy timeout) inside the async generator,
  blocking the event loop under contention.
- [ ] **M — `use_process=True` is never set.** The killable-child ASR path
  (`local_whisper.py:91`) is documented as "the batch-UI worker sets this," but
  the worker goes through the default registry (`orchestrator.py:34`) where it is
  `False` — so the batch UI's per-item ASR timeout does not actually stop compute.
- [ ] **M — `cancellable.py` waits out the full timeout on child death.** If the
  child dies without posting a result (native-code segfault is realistic),
  `q.get(timeout=…)` blocks up to 1800 s instead of watching `p.sentinel`.
- [ ] **M — Temp artifacts leak.** `ytdlp_subs.fetch` (`:92`) and
  `media.acquire_audio` (`:27-40`) never remove their `mkdtemp` dirs; ASR pulls
  leak the downloaded audio (GBs). Already flagged in `SECURITY_REVIEW.md`; still open.
- [ ] **M — Job-store races.** `complete_item`/`fail_item` (`web/jobs.py:140-148`)
  lack an `AND status='running'` guard, so a worker outliving a cancel flips a
  `cancelled` item back to `complete`; `claim_next_queued` (`:134`) unconditionally
  sets the job back to `running`. `app.state.workers` (`web/app.py:51,58`) also
  accumulates finished `Process` objects that are never joined.
- [ ] **L — Misclassifications.** "This video is private" maps to
  `Reason.members_only` instead of `Reason.private` (`ytdlp_subs.py:56-59`; the
  test asserts the wrong value). Provenance decided by `"auto" in vtt.name`
  (`:130,150`) — an 11-char id containing "auto" misclassifies.
- [ ] **L — Cache hit assigns a raw ISO string into `cached_at:
  Optional[datetime]`** (`cache.py:167`); enable `validate_assignment` or parse it.

## Security

- [ ] **M — `EgressPolicy.allowed_hosts` is never populated** (`policy.py:43`), so
  `assert_safe_url` only checks the scheme — the CLI hands any host to yt-dlp with
  `--enable-public-url`, contradicting `DESIGN.md §12`'s allowlist promise.
  (The web path is safe: it canonicalizes to a strict watch URL, `web/parse.py:71`.)
- [ ] **L — Document `MANAGED_API_BASE_URL`** in `.env.example` (read at
  `managed.py:228`).
- [ ] **L — Sanitize lock keys** before path interpolation (`locking.py:55`) —
  defensive only; current callers pass hex hashes.

## Tests

- [ ] **M — The production spawn path has zero coverage.** Every web test injects
  a synchronous `run_worker`; `_spawn`/`_terminate` (`web/app.py:53-67`) — real
  multiprocessing, SIGTERM, and the terminate-vs-DB-write race — are never
  exercised. One end-to-end test with a trivial pull would cover it.
- [ ] **M — No tests for `security.py`** (redact, `build_subprocess_args`) or
  `cli.py` (`_classify_target`, exit codes); no SSE test for an unknown job (would
  have caught the infinite loop).
- [ ] **L — `media.acquire_audio` untested** even at the injectable seam.

## Tooling & packaging

- [x] **H — No linter/formatter/type-checker.** *(added: ruff + mypy in dev extra, `[tool.ruff]`/`[tool.mypy]` configured, tree ruff-clean)* Add `ruff` + `mypy` to the `dev`
  extra and configure `[tool.ruff]` / `[tool.mypy]` in `pyproject.toml` before
  building R0–R6, so the retrieval code is born under enforcement. The tree is
  ~90 % annotated already; several findings above would have been caught
  mechanically.
- [ ] **L — Project metadata:** add `readme`, `license`, classifiers, and a
  `__version__` in `transcript_tool/__init__.py`. (`uv.lock` already covers
  reproducibility — no action there.)
- [ ] **L — Deduplicate strategy helpers:** the `_attempt`/`_fail`/`_unavail` trio
  is copy-pasted four times; `AUDIO_SUFFIXES` three times; `WATCH_URL` twice; the
  markdown front-matter block twice. A small mixin removes ~80 lines.

## Repo hygiene

- [x] **H — Commit the spec work.** *(committed in 8bc4cf9)* `docs/RETRIEVAL_DESIGN.md` is untracked and
  `CLAUDE.md` / `README.md` / `docs/DESIGN.md` / `.gitignore` carry uncommitted
  edits — the locked decisions exist only in the working tree.
- [ ] **L — Root clutter:** `CONVERSATION_LOG.txt` is a *reconstruction*, not a
  transcript (label or drop); `RETRIEVAL_DESIGN.txt` is a duplicate export of the
  `.md` (regenerate on spec change or drop); `moonshot_EP278_transcript.md`
  belongs under `corpus/` once R0 exists.
