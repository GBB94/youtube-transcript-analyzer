# Phases 5–6 — task prompt for Claude Code

> Hand this to Claude Code as the next task, after Phases 1–4 are green. `CLAUDE.md`
> holds the durable rules; `docs/DESIGN.md` is the full spec (§5 architecture, §6
> outcome model, §7 strategies, §8 discovery, §14 profiles). Build **one strategy /
> one slice at a time**, keep `pytest -q` green, and do not enable any gated
> capability by default.
>
> Phase 5 (managed providers) and Phase 6 (discovery) are **independent** — do them
> in either order. Neither makes live network calls in CI; everything is tested via
> injected clients against **recorded/fixture** responses.

These phases are the **scale / commercialization track**. They are not required for
single-operator file/URL use (Phases 1–4 already cover that). Build them when you
want blocking-resistant capacity (Phase 5) and the "find" half so you can batch
(Phase 6).

---

## Phase 5 — Managed providers

### Why
Managed transcript providers (e.g. Supadata, youtube-transcript.io, AssemblyAI,
Deepgram-style) handle the IP/bot-blocking and PO-token churn on their own
infrastructure. They are **additional capacity**, **not** "blocking-proof": they
have outages, rate limits, and cannot reach private/members-only/age-restricted
videos. Treat them accordingly.

### The three strategies (slot into `strategies/`, replace the `_stubs` entries)

**`managed_native`** — a *caption* strategy. Fetches **existing** captions via the
provider's native/captions-only mode.
- MUST use the provider's captions-only path (e.g. `mode=native`). Do **not** use an
  `auto` mode that silently performs paid AI transcription — that duplicates
  `managed_asr` and bills the operator unexpectedly.
- Maps to `human_caption` / `platform_auto` provenance from provider metadata when
  available; otherwise leave provenance honest (don't guess).

**`managed_asr`** — a *transcription* strategy over media the pipeline already
acquired (uploaded file or `media.acquire_audio`). `provenance = managed_asr`.

**`managed_url_to_asr`** — a **compound** strategy: hand the provider a URL, get a
transcript back, with **no intermediate local media artifact**. Model it distinctly
from `media-acquisition + managed_asr`; it does not produce a local `MediaArtifact`
and its cost/latency profile is different.

### Shared contracts (apply to all three)
- **Key-gated.** Read the key from config (`MANAGED_API_KEY`); if absent, the
  strategy is **not applicable** and is skipped — never a hard failure. A configured
  key that the provider rejects ⇒ `failed/provider_error` (or `access_challenge` for
  an auth/bot wall), retry-eligible per `Retry-After`.
- **Injectable HTTP client.** Constructor takes a `client` (default: a thin `httpx`
  wrapper). Tests inject a fake returning **recorded fixtures**; no live calls in CI.
- **Cost is real here.** Populate `Cost{amount, unit, currency, estimated}` —
  `unit="provider_credits"` or `"usd"`, `estimated=True` unless the provider returns
  an exact charge. Units, credits, and dollars are not interchangeable.
- **Error mapping** to the outcome model: provider 4xx for missing content →
  `unavailable` (`captions_unavailable` / `language_unavailable` / `removed` /
  `members_only` / `age_restricted` per the provider's signal); 429 → `rate_limited`
  with `Retry-After` → `retry.not_before`; 5xx/transport → `provider_error`; auth
  failure → `access_challenge`.
- **Translation:** only set `translated_caption` provenance when the provider
  **discloses** translation (set `Language.detection_method` to the provider flag).
  Never infer translation from text.
- **Security/observability:** redact provider request IDs and keys in logs
  (`security.redact`); record `Attempt.provider_request_id`, `latency_ms`, `cost`.
- **Supply chain:** pin the provider SDK/version; document it as a third-party
  dependency. Honor `EgressPolicy` (these are network strategies; gated like the
  other public-URL work).
- **Caching:** provider failures get short, reason-specific negative TTLs (already in
  `cache.py`); a missing key is config, not negative-cached.

### Acceptance (Phase 5)
- Unit tests per strategy with recorded fixtures: success (with cost populated),
  `language_unavailable`, a 429 producing `rate_limited` + `not_before`, an outage
  producing `provider_error`, and "no key ⇒ skipped/not applicable."
- `managed_native` proven to use the captions-only path (assert the request shape).
- Strategies slot into the orchestrator behind the free strategies per policy order
  (managed providers are fallbacks, not first choice).
- No live network in CI; redaction verified on a log line containing a fake key.

### Not in this phase
- Provider **canaries** (scheduled live health checks) — that's Phase 8, `server`
  profile.
- Choosing/contracting a specific vendor — leave the adapter interface generic with
  one reference adapter.

---

## Phase 6 — Discovery ("find")

### Why
The "find" half: turn a channel, playlist, or query into a list of `VideoRef`s to
feed the existing `pull` pipeline, so you can batch instead of pasting URLs.

### Module: `discover.py`
- **Endpoints (YouTube Data API v3):** `search.list` (query), `channels.list` +
  `playlistItems.list` (channel uploads), `videos.list` (batched enrichment).
- **Output:** `list[VideoRef]` (+ lightweight metadata) ready for the orchestrator.
- Injectable API client; tests use recorded fixtures (no live quota spend in CI).

### Dual-bucket quota (this is the corrected, verified model — do not regress it)
- `search.list` lives in its **own "Search Queries" bucket**: default **100 calls/day
  at 1 unit/call**. `videos.insert` likewise has its own bucket. **All other methods**
  (channels.list, playlistItems.list, videos.list, …) draw on the separate
  **10,000-unit/day** pool.
- **Track each bucket independently.** Read the project's **actual Cloud Console
  limits** rather than assuming defaults — quotas can be raised/lowered per project.
- The API gives **no authoritative remaining-quota readout** — maintain a **local
  budget estimate** and treat it as an estimate, not truth.
- Practical guidance to bake into the planner: search is the scarce resource (~100/day
  in its own bucket); the 10k pool is abundant for cheap channel/playlist/`videos.list`
  enrichment (1 unit each). **Prefer channel/playlist traversal over search.**

### Contracts
- **Resolution:** define how `@handles` / custom URLs resolve to channel IDs
  (`channels.list?forHandle=` / search fallback); **cache the mapping**.
- **Channel-traversal semantics (must be explicit):** does "whole channel" include
  Shorts, past livestreams, premieres? How are deleted/private placeholders and
  reordered uploads handled? Make these flags on the discovery call, with sane
  defaults documented.
- **Result stability:** `search.list` results are **not stable** — persist
  `regionCode`, `relevanceLanguage`, `safeSearch`, `order`, and the query alongside
  results so a re-run is interpretable.
- **Enrichment:** batch `videos.list` (≤50 IDs/call); treat missing IDs as
  inaccessible/deleted, not errors.
- **`captions.download` is owner-only** — never use it as a third-party transcript
  source (transcripts come from the pull strategies).
- **Compliance / retention:** discovery uses the **authorized** API (lower-risk than
  scraping), but YouTube's developer policies require non-authorized API metadata to
  be **refreshed or deleted within ~30 days**. Cache discovery metadata in the
  **separate metadata cache** with its own refresh policy (see `cache.py` /
  `DESIGN.md §4, §10`). Discovery output still feeds the **gated** public-URL pull
  path — discovery does not bypass the `EgressPolicy` gate.

### CLI wiring (`cmd_find` is currently a skeleton)
- `transcript find --channel <id|@handle> --max N --format ids` → one video id per
  line on **stdout** (logs to stderr), pipeable into `transcript pull --file -`.
- `transcript find --query "<q>" --max N --format jsonl` → one `VideoRef`-ish JSON
  object per line.
- Surface the **estimated** remaining budget per bucket on stderr; fail clearly on
  `quotaExceeded` (per bucket).

### Acceptance (Phase 6)
- Unit tests with recorded API fixtures: channel-uploads traversal (with/without
  Shorts), query search, handle→channel-id resolution (+ cache hit), batched
  `videos.list` enrichment with a missing ID handled gracefully.
- **Dual-bucket budgeting** tested: search calls decrement the search bucket; reads
  decrement the general pool; the two are tracked separately.
- `find --format ids | pull --file -` round-trips end to end against fixtures.
- Discovery metadata is stored in the metadata cache with a ≤30-day refresh and is
  never written to the request-result cache.

### Not in this phase
- **Incremental channel monitoring** (only new uploads since last run) — future
  extension; leave a hook (store last-seen upload per channel) but don't build the
  scheduler.
- The `server` profile's shared quota accounting / canaries — Phase 7–8.

---

## Suggested build order & effort (one experienced builder)

| Step | Deliverable | Rough |
|---|---|---|
| 6a | `discover.py`: client + channel/playlist traversal + `videos.list` enrichment | ~1 day |
| 6b | Resolution (@handle→id, cached), result-stability params, dual-bucket budget tracker | ~0.5–1 day |
| 6c | Wire `cmd_find` (`--format ids/jsonl`), pipe into `pull`, metadata-cache + retention | ~0.5 day |
| 5a | Generic managed-provider adapter interface + one reference adapter (`managed_native`) | ~1 day |
| 5b | `managed_asr` + `managed_url_to_asr` (compound) + cost/redaction/error mapping | ~1 day |
| 5c | Negative-cache + Retry-After handling + key-gating tests | ~0.5 day |

Either phase can ship first. After 5–6, the remaining roadmap is the `server` profile
(Phase 7) and the failure-injection + canary + security hardening (Phase 8) — the
productization tail, gated by the Phase 0 compliance/SLO work before any release.
