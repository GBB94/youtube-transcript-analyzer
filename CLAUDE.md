# CLAUDE.md — working agreement for this repo

Read this before writing code. The authoritative designs are `docs/DESIGN.md` (the
v3 acquisition brief) and `docs/RETRIEVAL_DESIGN.md` (the corpus & query brief);
this file is the short, durable version Claude Code should follow on every run.
When in doubt, the design docs win; if you change a contract, update both the doc
and this file.

## What this is
Two halves of one system:
1. **Acquisition (the pull half) — built.** A staged, policy-driven tool that
   produces clean, provenance-rich transcripts from video. Caption strategies
   first, audio ASR as the floor. Reliability comes from honest staging + good
   contracts, not from pretending every video succeeds.
2. **Corpus & query (the ask half) — built, R0–R7** (`docs/RETRIEVAL_DESIGN.md`).
   Turn pulled transcripts into a queryable knowledge base: grounded
   answers with episode+timestamp citations. Canonical transcripts are pulled once
   and are the source of truth; everything downstream (chunks, embeddings, indexes)
   is derived and rebuildable **with no network access**.

## Architecture (three stages — never call them "layers" or "L1/L2/L3")
1. **Preflight** — produces *hints*, not truth. Short-circuit ONLY on authoritative
   terminals (invalid input, confirmed removal). Inconclusive ⇒ keep going.
2. **Acquisition** — policy-ordered strategies by *name*: `uploaded_caption`,
   `api_captions`, `ytdlp_subs`, `managed_native`, plus media acquisition
   (`ytdlp_audio`, `uploaded_file`) and the compound `managed_url_to_asr`.
3. **Transcription** — `local_whisper`, `managed_asr` (only if no caption obtained).
Then: normalize (versioned) → quality-gate → cache → emit.

## Golden rules (these are how the review punch-list is enforced — don't regress them)
- **Outcome model is sacred.** `Result` is discriminated: `success | unavailable |
  failed`. A success never carries a `reason`; a non-success never carries
  transcript fields. The validator in `schema.py` enforces this — don't bypass it.
- **`retry` is set explicitly**, never derived from `reason` alone. `availability_scope`
  (permanent/contextual/transient) is separate: `live`, `geoblocked`, `age_restricted`
  etc. are not permanently terminal.
- **Reasons are complete.** Use the right one: `captions_unavailable`,
  `language_unavailable`, `no_acceptable_transcript`, `invalid_input`,
  `access_challenge`, `po_token_rejected`. Config failures
  (`missing_js_runtime`/`missing_po_token_provider`/`missing_dependency`) are
  distinct from content failures and are **never persistently negative-cached**.
- **Cache contract** (`cache.py`): take the per-request lock, then **re-check** the
  cache. A cache hit is **labelled** (`CacheProvenance`) and its `attempts` are
  cleared — never replay old attempts as fresh. A result whose `raw_cues_ref`
  artifact was evicted is a **miss**, not a stale hit. `policy_hash` covers enabled
  strategies + language prefs + quality config + egress policy.
- **Cost is structured**: `{amount, unit, currency, estimated}`. Units, provider
  credits, and dollars are not interchangeable.
- **Language is a preference list** (BCP-47): `requested: [...]`, plus `selected`,
  `spoken_detected`, `track_language`. A **translated** track is only claimed with
  **adapter evidence** (`detection_method`) — never inferred from text.
- **Resource limits**: exceeding any → `failed` with `reason=resource_limit_exceeded`
  and a named `resource_dimension`. Duration/bytes/cost/timeout are hard in both
  profiles; memory is advisory locally (hard via cgroup on server).
- **Model provisioning** (Phase 4): pre-provision + checksum out of band; load
  **lazily** on first ASR use (local) or warm at startup (server). **Never download
  a model mid-request** — a caption-first run must not load a multi-GB model.

## Golden rules — retrieval half (see `docs/RETRIEVAL_DESIGN.md`; built, keep enforced)
- **Canonical vs. derived is sacred.** `corpus/<slug>/raw/*.json` is the only
  expensive, never-discarded layer. Chunks, contextual blurbs, embeddings, and
  indexes are pure functions of it and must rebuild **offline**. A version bump
  rebuilds derived layers; it **never** re-pulls. No new YouTube access path —
  ingest is `find`+`pull` under the existing `EgressPolicy` gate.
- **Never flatten timestamps.** Every chunk carries `{video_id, start_s, end_s}`
  so every answer is a working `…&t=<s>s` deep link.
- **Grounded-answer invariant (the outcome-model analogue).** `answer_outcome` is
  `grounded | insufficient_evidence | failed`. A `grounded` answer carries only
  citations drawn from the retrieved set — **no fabricated timestamps**;
  `insufficient_evidence` cites nothing and does **not** fall back to parametric
  memory. Provenance (`platform_auto` vs `local_asr` …) is preserved to the answer.
  **Parent-context expansion never widens citations**: the answerer may read ±1
  neighbouring chunks, but a citation naming an expansion-only neighbour fails
  validation.
- **One index store.** Dense vectors and BM25 live in the same LanceDB table
  (native FTS + built-in RRF hybrid) — never a second index directory that could
  drift. No HyDE / keyword query expansion in v1 (measured to hurt; see
  `RETRIEVAL_DESIGN.md §20.6`).
- **Derivation is versioned.** `CHUNKER_VERSION` / `CONTEXT_VERSION` / embedder
  revision / `INDEX_SCHEMA_VERSION` are the index cache key (the `policy_hash`
  analogue). No silent behavioural change without a bump.
- **Egress is explicit.** Contextual enrichment and API embeddings send text off
  machine; both are disclosed and disableable (`--no-context`, `--embedder local`).

## Security (apply from the start, even before subprocesses exist)
- Subprocesses use **argument arrays, never a shell**, and put **`--` before
  untrusted positionals** (see `security.py`). Never derive filenames from remote
  titles. Allowlist URL schemes/hosts.
- **Cookies are opt-in only** (dedicated creds, restrictive perms, redacted logs);
  never auto-read browser cookies. **Never** use `--no-check-certificates` — install
  the enterprise CA instead.
- The **PO-token provider plugin is a third-party supply-chain dependency**: pin and
  review it.

## Compliance (don't quietly weaken this)
A deployment profile is **not** a Terms exemption. `uploaded_file` is
operator-supplied **subject to a rights assertion + file validation** (we can't
verify licensing). Arbitrary public-URL extraction is **off by default**, gated
behind an explicit policy decision (see `policy.EgressPolicy`, `DESIGN.md §4`).
No ToS-circumvention defaults (no residential proxies / geo-bypass).

## I/O & API conventions
- **Async is canonical** (`get_transcript`); `get_transcript_sync` is a wrapper that
  **raises** if called inside a running event loop.
- CLI: machine output to **stdout**, all logs/progress to **stderr**.
- Bump `NORMALIZER_VERSION` / `SCHEMA_VERSION` in `schema.py` on any behavioural
  contract change — they are cache-key inputs.

## Repo layout
```
src/transcript_tool/
  schema.py        # the contract (outcome model, enums) — change carefully
  policy.py        # Policy + policy_hash; QualityConfig; EgressPolicy
  preflight.py     # hints only
  normalize.py     # versioned normalization, VTT parse + rolling-overlap dedup
  quality.py       # source-aware gates (warn before reject)
  cache.py         # two-layer + metadata cache, lifecycle contract (local profile)
  orchestrator.py  # staged pipeline + singleflight; get_transcript[/_sync]
  provisioning.py  # ASR model provisioning + warm-at-startup (P4/P7)
  security.py      # subprocess/url/redaction helpers
  profiles.py      # local vs server profile + ResourceLimits + enforce_limit (P7)
  locking.py       # pluggable lock backend: flock (local, death-safe) / shared stub (P7)
  cancellable.py   # killable out-of-process execution for real ASR cancel (UI-0)
  bakeoff.py       # reliability bakeoff harness (P0)
  cli.py           # pull / find / doctor / bakeoff / serve / corpus add|build|status / ask / eval
  strategies/
    base.py            # Strategy protocol
    uploaded_caption.py  # P1   api_captions.py  # P2
    ytdlp_subs.py        # P3   local_whisper.py # P4
    managed.py           # managed_native/asr/url_to_asr (P5)
    _stubs.py            # _Unbuilt base (no stubs left as of P5)
  asr_eval.py        # jiwer regression harness (P4)
  media.py           # yt-dlp audio acquisition for URL->ASR (live-only)
  discover.py        # YouTube Data API discovery + dual-bucket quota (P6)
  corpus/            # R0+R7 — canonical store + ingest
    records.py         # CorpusRecord, content_hash, date__id naming
    store.py           # raw/+markdown/ layers, manifest ledger, atomic writes, slug lock
    ingest.py          # corpus add (reuses find+pull; only-new; hash guard)
    status.py          # counts, versions, staleness
    diarize.py         # R7 per-episode opt-in: audio->ASR->speakers (Diarizer injectable)
  retrieval/         # R1-R6 — all derived, versioned, offline-rebuildable
    chunk.py           # CHUNKER_VERSION; chapter/utterance/speaker-aware, overlap
    embed.py           # Embedder protocol; local default, Voyage API optional
    index.py           # ONE LanceDB store (dense+FTS+ChunkMeta); build.json tuple
    build.py           # corpus build: per-video staleness, cascades, manifest stamps
    retrieve.py        # hybrid RRF + cross-encoder rerank + prefilter Filters
    context.py         # CONTEXT_VERSION; prompt-cached blurbs (egress, gated)
    answer.py          # ask/ask_sync; grounded|insufficient_evidence|failed
    evalharness.py     # golden set, recall@k/MRR, judge, --compare gate
tests/             # pytest; golden VTT fixtures govern dedup
docs/DESIGN.md          # the authoritative v3 acquisition spec
docs/RETRIEVAL_DESIGN.md # the corpus & query (ask half) spec
docs/PHASE_1_BUILD.md   # historical phase task docs (P1, P5/6, P0/7/8)
```

## Build / run
```
pip install -e ".[dev]"      # or: pip install pydantic pytest
pytest -q                    # all green is the bar
ruff check src tests         # must stay clean; corpus/ + retrieval/ are also mypy-clean
transcript pull tests/fixtures/rolling_autocaption.vtt
transcript doctor            # per-strategy + retrieval-component readiness

# retrieval half (needs the 'retrieval' extra; models/answerer per doctor hints)
transcript corpus add <slug> --channel @handle --no-shorts --enable-public-url
transcript corpus build <slug>          # offline; --no-context stays fully local
transcript ask "question" --json
transcript eval <slug> --compare baseline.json
```

## What's built vs stubbed
- **Built (Phases 1–6):** schema/outcome model, policy + policy_hash, preflight,
  normalization + dedup (fixture-tested), source-aware quality gates, two-layer
  cache + a separate metadata store (≤30-day TTL), orchestrator + singleflight,
  sync guard, CLI (`pull` handles caption/audio files, gated URLs, bare YouTube ids,
  and `--file -` batch; `find`; `doctor`).
  - `uploaded_caption` (P1), `api_captions` (P2, youtube-transcript-api),
    `ytdlp_subs` (P3, yt-dlp), `local_whisper` (P4, faster-whisper) + the `jiwer`
    regression harness (`asr_eval.py`) + model provisioning contract.
  - `managed_native` / `managed_asr` / `managed_url_to_asr` (P5) — generic provider
    adapter over an injectable HTTP client; key-gated, egress-gated, structured Cost.
  - Discovery (P6) — `discover.py`: channel/playlist traversal, query search, handle
    resolution, batched enrichment, **dual-bucket quota** (search vs general pool).
  - All network/model strategies are unit-tested via **dependency injection** (fake
    client / runner / transcriber); the live YouTube and real-model paths (P2–P4)
    were verified on a real machine, not in CI.
- **Seams + partial (P0/P7/P8):**
  - P0 — `bakeoff.py` + `transcript bakeoff` (harness; real-corpus run needs real
    hardware), `docs/COMPLIANCE.md` + `docs/SLO.md` (DRAFT — legal sign-off and the
    real bakeoff numbers are human/hardware-gated).
  - P7 — `profiles.py` (local/server, `ResourceLimits`, `enforce_limit`), `locking.py`
    (pluggable backend: `FileLockBackend` local; `SharedLockBackend` is a documented
    stub — real Redis/DB/container/cgroup is deploy-time), `provisioning.warm()`.
    Singleflight contract is preserved across the backend (tested).
  - P8 — `tests/test_failure_injection.py` (the fault matrix is green) +
    `docs/SECURITY_REVIEW.md`. Live canaries and SLO-conformance vs. real numbers
    remain infra/hardware-gated.

> Public-URL strategies (`api_captions`, `ytdlp_subs`, and `local_whisper` from a
> URL) are gated by `EgressPolicy.allow_public_url` and the CLI's
> `--enable-public-url` flag. Do not enable by default; honor `DESIGN.md §4`.

- **Built — retrieval half (R0–R7, `docs/RETRIEVAL_DESIGN.md`):** all phases
  implemented and unit-tested via injected adapters (fake embedder / context
  client / answer client / diarizer — CI touches no network and downloads no
  model). R0 canonical corpus store (`corpus add`, reusing `find`+`pull` under
  the same egress gate); R1 versioned chunker (chapter/utterance/speaker-aware,
  overlap, timestamped `ChunkMeta`); R2 embeddings + ONE LanceDB store +
  offline version-aware `corpus build`; R3 hybrid (native FTS BM25 + built-in
  RRF + cross-encoder rerank + prefilter metadata filters); R4 contextual
  enrichment (prompt-cached system block, `CONTEXT_VERSION`, `--no-context`);
  R5 `transcript ask` (grounded answering, server-built citations, ±1-chunk
  expansion that never widens citations, `--json`, exit codes 0/1/2); R6 eval
  harness (recall@k / MRR / faithfulness judge, `eval --compare` regression
  gate, exit 3); R7 on-demand diarization (`corpus add --diarize`, default off,
  per-episode, mixed corpus, `ask --speaker`). CLI verbs: `corpus add|build|status`,
  `ask`, `eval`; `doctor` reports the retrieval components + per-corpus
  staleness. Live-model paths (sentence-transformers, cross-encoder, Anthropic,
  pyannote) are lazy optional extras verified outside CI, mirroring P2–P4.
  **Decisions locked and implemented:** contextual enrichment on by default
  (switchable), local embeddings by default, captions-default with
  diarization strictly opt-in per episode (never automatic on every pull), one
  LanceDB store for dense+BM25, parent-context expansion at answer time (never
  widening citations), no HyDE/keyword query expansion in v1
  (`RETRIEVAL_DESIGN.md §20.4–20.6`).

## Supported platforms (v1)
Tested target: **macOS ARM (CPU)**. Linux x86-64 should work; process-tree kill,
filesystem locking, GPU, and memory enforcement are OS-specific — treat untested
platforms as best-effort and gate platform-specific code.

## Don't
- Don't build multiple phases at once. One vertical slice, green tests, then next.
- Don't add a public-URL/network path without honoring `EgressPolicy` and `DESIGN.md §4`.
- Don't weaken the outcome-model validator or the cache lifecycle rules to make a
  test pass — fix the cause.
