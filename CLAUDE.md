# CLAUDE.md — working agreement for this repo

Read this before writing code. The authoritative design is `docs/DESIGN.md` (the
v3 brief); this file is the short, durable version Claude Code should follow on
every run. When in doubt, the design doc wins; if you change a contract, update
both.

## What this is
A staged, policy-driven tool that produces clean, provenance-rich transcripts
from video. Caption strategies first, audio ASR as the floor. Reliability comes
from honest staging + good contracts, not from pretending every video succeeds.

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
  provisioning.py  # ASR model provisioning contract (Phase 4 stub)
  security.py      # subprocess/url/redaction helpers
  cli.py           # pull / find / doctor
  strategies/
    base.py            # Strategy protocol
    uploaded_caption.py  # P1   api_captions.py  # P2
    ytdlp_subs.py        # P3   local_whisper.py # P4
    managed.py           # managed_native/asr/url_to_asr (P5)
    _stubs.py            # _Unbuilt base (no stubs left as of P5)
  asr_eval.py        # jiwer regression harness (P4)
  media.py           # yt-dlp audio acquisition for URL->ASR (live-only)
  discover.py        # YouTube Data API discovery + dual-bucket quota (P6)
tests/             # pytest; golden VTT fixtures govern dedup
docs/DESIGN.md     # the authoritative v3 spec
docs/PHASE_1_BUILD.md  # the current task
```

## Build / run
```
pip install -e ".[dev]"      # or: pip install pydantic pytest
pytest -q                    # all green is the bar
transcript pull tests/fixtures/rolling_autocaption.vtt
transcript doctor
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
- **Stubbed (with contract docstrings):** the `server` deployment profile (P7) and
  the failure-injection / canary / security hardening suites (P8).

> Public-URL strategies (`api_captions`, `ytdlp_subs`, and `local_whisper` from a
> URL) are gated by `EgressPolicy.allow_public_url` and the CLI's
> `--enable-public-url` flag. Do not enable by default; honor `DESIGN.md §4`.

## Supported platforms (v1)
Tested target: **macOS ARM (CPU)**. Linux x86-64 should work; process-tree kill,
filesystem locking, GPU, and memory enforcement are OS-specific — treat untested
platforms as best-effort and gate platform-specific code.

## Don't
- Don't build multiple phases at once. One vertical slice, green tests, then next.
- Don't add a public-URL/network path without honoring `EgressPolicy` and `DESIGN.md §4`.
- Don't weaken the outcome-model validator or the cache lifecycle rules to make a
  test pass — fix the cause.
