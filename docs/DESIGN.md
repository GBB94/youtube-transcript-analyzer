# Project Brief v3 — Video Transcript Tool (Production-Grade)

*Discover videos, acquire the cheapest acceptable transcript, normalize it deterministically, and escalate to ASR when captions are unavailable — built to a production standard, with a personal-use deployment profile today and a clean, auditable path to a commercial release later.*

**Status:** supersedes v2. Incorporates a second senior review. Section 2 records the changes and corrects one factual error from v2 (the YouTube quota model).

---

## 0. Scope & posture

Built for personal use now, engineered so it can become a product. Two consequences:

- **Build to production standards** for correctness, safety, and observability — cheap to do right early, expensive to retrofit.
- **Gate the legally/operationally sensitive parts** behind explicit policy and a deployment profile, so the personal build stays simple and a future release has a clear boundary.

Two deployment profiles run the same core (Section 14): **`local`** (single user/machine) and **`server`** (multi-worker/shared).

---

## 1. Purpose

Video is a rich, under-mined source of signal — competitor webinars, product walkthroughs, founder talks, conference sessions, prospect content — locked in a format you can't search, score, or pipe anywhere. Pulling transcripts *reliably* is the hard part: captions get disabled, auto-captions don't exist, and platforms block automated access. The tool addresses this with **staged, policy-driven escalation** — cheap caption strategies first, audio ASR as the floor — under an honest boundary on what "reliable" can mean (Section 3).

---

## 2. Changes in v3 (second-review dispositions)

**Correction first:** v2 rejected the reviewer's quota claim. v2 was wrong. Verified against Google's primary docs: `search.list` and `videos.insert` now each sit in their **own quota bucket** (default **100 calls/day, 1 unit/call**); all other methods draw on the separate **10,000-unit/day** pool. The v2 figure of "100 units against the shared pool" is corrected throughout (Sections 8, 17). The error came from trusting stale secondary write-ups over the method documentation.

| # | Review point | Disposition | Note |
|---|---|---|---|
| 1 | Quota model (v2 rejection was wrong) | **Accepted (corrected)** | Dual-bucket model; track each bucket independently; use Cloud Console actual limits, not assumed defaults. |
| 2 | Local use is not a ToS exemption | **Accepted** | Capability model revised (Section 4); removed "always allowed"; activation is an explicit policy decision tied to the *use*, not just commercial release. |
| 3 | Preflight = hints, not authoritative truth | **Accepted** | Short-circuit only on authoritative terminals; otherwise allow strategy-specific attempts (Section 5). |
| 4 | Several "terminal" reasons are contextual | **Accepted** | Added `availability_scope` + explicit `retry` object; `retryable` no longer derived from reason alone (Section 6). |
| 5 | Cache key cannot work as written | **Accepted** | Split into request-result + artifact/strategy caches, separate metadata cache, reason-specific negative TTLs, local filesystem lock (Section 10). |
| 6 | Keep a small objective ASR regression set | **Accepted (overrides v2 pushback)** | 5–10 licensed clips per *supported* language, checked-in references, `jiwer` WER/CER, generous thresholds — a change-detection guard, not a research target (Sections 9, 17). |
| 7 | Reconcile resource-limit promises | **Accepted** | Hard/advisory matrix by dimension; `resource_limit_exceeded` with a named dimension replaces overloaded `budget_exceeded` (Section 11). |
| 8 | Strategy names, not L1/L2/L3 | **Accepted** | Numbering dropped throughout. |
| 9 | Managed URL→ASR is a compound strategy | **Accepted** | Modeled as one strategy with no intermediate local media artifact (Section 7). |
| 10 | "ASR only for accuracy" unsupported | **Accepted** | Reframed: ASR-only is for consistency/privacy; human captions may be more accurate. |
| 11 | Preserve raw cues / raw-artifact hash | **Accepted** | Not just `raw_text` (Sections 6, 9). |
| 12 | Source-aware quality gates | **Accepted** | Repetition/CPS **warn** before they hard-reject (protects lyrics/fast speech) (Section 9). |
| 13 | `translated_caption` handling | **Accepted (nuanced)** | **Kept as a provenance label** so translated tracks are *detected and flagged/rejected*, with an explicit rule that translation is never *requested* — rather than removing the label and going blind to it. |
| 14 | Expand `attempts` | **Accepted** | latency, retry count, cost, provider request ID, quality-rejection reasons (Section 6). |
| 15 | `transcript doctor` command | **Accepted** | Verifies yt-dlp, JS runtime, EJS, ffmpeg, PO-token plugin, ASR model, permissions (Section 12). |
| 16 | PO-token plugin = supply-chain dependency | **Accepted** | Pinned and reviewed; flagged as third-party/unaffiliated (Section 13). |

---

## 3. Goals, non-goals & the honest reliability boundary

**Goals:** reliability through staged escalation (not false-independent fallbacks); structured, provenance-rich output; find + pull as first-class; async-canonical library + CLI; deterministic, versioned normalization that never silently rewrites spoken words; cost/latency/privacy-aware policy.

**Non-goals (v1):** real-time/live transcription; diarization; translation (capture source language only); hosted UI.

**Honest boundary:** *"If usable audio can be acquired, ASR provides a caption-independent transcription path."* **Not** "any video with speech will succeed." Terminal/`unavailable` outcomes (private, removed, DRM, members-only, age-restricted, geoblocked, live, bot-gated, PO-token-gated, unsupported, no speech) are expected, not bugs — though several are *contextual* and may change with time or credentials (Section 6).

---

## 4. Compliance & rights posture

Scope-independent, because it governs what you're *permitted* to do. **A deployment profile is not a Terms exemption** — "local" does not make unauthorized downloading/scraping/circumvention acceptable.

**Tiered capability model:**
1. **Uploaded / licensed media** — supported **when the operator asserts sufficient rights**. The tool cannot verify licensing; it records the operator's assertion. The safest input.
2. **Owned content via authorized APIs** — captions through authorized API paths; media only through an authorized acquisition path.
3. **Arbitrary public URLs** — **buildable behind a flag, but activation requires an explicit policy decision appropriate to the use** (not merely "commercial release"). Off by default.

**No ToS-circumvention defaults.** Residential proxies, geo-bypass, and TLS-verification disabling are removed from the recommended design. Cookies are **explicit opt-in only**, with dedicated (non-primary) credentials, restrictive permissions, redacted logs, and a documented account-risk warning.

**Data retention.** Per YouTube developer policies, non-authorized API metadata must be refreshed or deleted within ~30 days; cache TTLs enforce this (Section 10). *Confirm current terms before any release.*

**Pre-release policy set:** retention, deletion, attribution, privacy (PII in transcripts), copyrighted-content handling. This brief specifies a capability; it is not legal advice on permitted use.

---

## 5. Architecture — three stages

```
   find ─► Discovery (Data API) ─► [video refs]
                                       │
   ┌───────────────────────────────────▼──────────────────────────────┐
   │ STAGE 1 — Preflight (produces HINTS, not truth)                   │
   │   resolve id/handle, collect metadata + access evidence.          │
   │   short-circuit ONLY on authoritative terminals (malformed input, │
   │   confirmed removal). otherwise → allow strategy attempts.        │
   └───────────────────────────────────┬──────────────────────────────┘
                                        ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │ STAGE 2 — Acquisition (policy-ordered strategies)                 │
   │   caption strategies:  api_captions ▸ ytdlp_subs ▸ managed_native │
   │   media acquisition:   ytdlp_audio / uploaded_file                │
   │   compound:            managed_url_to_asr (no local media artifact)│
   └───────────────────────────────────┬──────────────────────────────┘
                                        ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │ STAGE 3 — Transcription (only if no caption obtained)             │
   │   local_whisper ▸ managed_asr                                     │
   └───────────────────────────────────┬──────────────────────────────┘
                                        ▼
        normalize (versioned) → quality-gate → cache → emit
```

**Preflight produces hints, not authoritative truth.** A local probe may report "blocked" while a managed strategy would succeed; never let a non-authoritative probe terminate the request. Only authoritative evidence (malformed input, confirmed removal) short-circuits.

**Policy, not "first success wins."** A configurable policy orders strategies by cost/latency/quality/privacy and may skip stages (captions-only for cost; ASR-only for consistency/privacy). Each strategy returns a typed outcome; the orchestrator advances on failure or quality-gate rejection.

**Correlated-failure note (document explicitly):** the caption strategies share YouTube's caption infrastructure; `ytdlp_subs` and `ytdlp_audio` share yt-dlp media access (now subject to JS-runtime and per-video PO-token requirements). If media access is blocked, ASR cannot run — these are not independent.

---

## 6. Outcome model (discriminated)

```jsonc
"outcome": "success | unavailable | failed",

"reason":
    // → unavailable
      "private" | "removed" | "drm" | "unsupported" | "no_speech"          // typically permanent
    | "members_only" | "age_restricted" | "geoblocked" | "bot_gated" | "live" // typically contextual/transient
    // → failed (operational)
    | "rate_limited" | "timeout" | "audio_download_failed" | "provider_error"
    // → failed (configuration)
    | "missing_js_runtime" | "missing_po_token_provider" | "missing_dependency"
    // → failed (control / limits)
    | "cancelled" | "resource_limit_exceeded",

"availability_scope": "permanent | contextual | transient | null",   // null on success
"retry": { "eligible": false, "not_before": "ISO-8601|null", "max_attempts": 0 },
"resource_dimension": "cost | duration | bytes | disk | memory | runtime | null"  // only with resource_limit_exceeded
```

`retry` is set **explicitly per reason + context**, not derived: `rate_limited` honors `Retry-After`; `live` may become processable later (`transient`, retry-eligible); `missing_po_token_provider` is a config failure (not retryable until fixed); `cancelled`/`resource_limit_exceeded` are not auto-retryable. `audio_download_failed` MUST NOT collapse into `no_speech`.

Success payload (abridged):

```jsonc
{
  "outcome": "success",
  "video_ref": { "platform": "youtube", "id": "…", "url": "…" },
  "provenance": "human_caption | platform_auto | local_asr | managed_asr | translated_caption",
  "language": { "requested": "en", "source": "en-US", "detected": "en", "detection_confidence": 0.98 }, // BCP-47
  "track_id": "…|null",
  "model": { "name": "faster-whisper", "size": "small", "revision": "…", "compute_type": "int8" }, // ASR only
  "normalizer_version": "1.0.0", "schema_version": "3.0.0",
  "timestamp_type": "caption_cue | asr_segment",
  "segments": [ { "start": 0.0, "end": 4.2, "text": "…" } ],
  "raw_cues_ref": "sha256:…",   // hash/handle to original cues+timing (not just raw_text)
  "raw_text": "…",
  "text": "…",                  // deterministic normalized output
  "word_count": 0, "duration_seconds": 0,
  "quality": { "gates": [ { "name": "cps", "result": "warn", "value": 21.4 } ] },
  "attempts": [
    { "strategy": "api_captions", "ok": false, "reason": "rate_limited",
      "latency_ms": 320, "retry_count": 2, "cost": 0, "provider_request_id": "…",
      "quality_rejections": [] }
  ],
  "fetched_at": "ISO-8601"
}
```

**`translated_caption` is retained as a provenance value on purpose** — so a provider-supplied auto-translated track is *detected and flagged/rejected*, not silently mislabeled as source language. Translation is never *requested* (it's a non-goal); the label exists for detection.

---

## 7. Strategies

**Caption strategies (Stage 2)**
- **`api_captions`** (`youtube-transcript-api`) — fastest, free; existing tracks. Falls through on disabled/missing/blocked.
- **`ytdlp_subs`** — robust extractor; writes `.vtt`, parsed + deduped (Section 9). Requires an external JS runtime (Deno/Node) for full YouTube support and may require a PO-token provider (Section 15). Cookies opt-in only.
- **`managed_native`** — managed provider in captions-only/native mode, so it does **not** silently bill paid ASR. *Additional capacity*, subject to outages and inaccessible-video limits — not "blocking-proof." Skipped if unconfigured.

**Media acquisition (feeds Stage 3)** — `ytdlp_audio` (same access constraints/risks as `ytdlp_subs`) or `uploaded_file` (compliance-safe, always available).

**Transcription (Stage 3)**
- **`local_whisper`** (`faster-whisper`) — decodes via PyAV (no system ffmpeg needed) on CTranslate2. Pin model name + revision + `compute_type`; **preload** models; enable **VAD**; capture **language probability** + **no-speech**. Default `small` (CPU), `large-v3` (GPU). No forced 16 kHz pre-conversion (the library resamples). External chunking, if used, needs overlap + deterministic boundary reconciliation.
- **`managed_asr`** — alternative when local compute isn't available.
- **`managed_url_to_asr`** — a **compound** strategy: provider takes a URL and returns a transcript with no intermediate local media artifact. Modeled distinctly from media-acquisition + local ASR.

ASR-only is selected for **consistency or privacy**, not accuracy — human captions may be more accurate.

---

## 8. Discovery (find) — YouTube Data API v3

- **Endpoints:** `search.list` (query), `channels.list` + `playlistItems.list` (channel uploads), `videos.list` (batched enrichment).
- **Quota (corrected, verified):** as of June 1, 2026 the API uses **granular per-method buckets**. `search.list` has its own **Search Queries** bucket — default **100 calls/day at 1 unit/call**. `videos.insert` likewise has its own 100/day bucket. **All other methods** draw on the separate **10,000-unit/day** pool. **Track each bucket independently**, and read the project's **actual Cloud Console limits** rather than assuming defaults. The API gives no authoritative remaining-quota readout — maintain local budget estimates. Practical effect: search is a hard ~100/day in its own bucket (spend it where nothing else works); the 10k pool is abundant for cheap channel/playlist/`videos.list` enrichment.
- **Resolution:** define how `@handles`/custom URLs resolve to channel IDs; cache the mapping.
- **Channel-traversal semantics (specify):** whether "whole channel" includes Shorts, past livestreams, premieres, and how deleted/private placeholders and reordered uploads are handled.
- **Result stability:** persist `regionCode`, `relevanceLanguage`, `safeSearch`, `order`, and the query — `search.list` results aren't stable.
- **Enrichment:** batch `videos.list` (≤50 IDs/call); missing IDs → inaccessible/deleted.
- **Caption download:** Data API `captions.download` requires permission to edit the video — owned content only.

---

## 9. Normalization & quality gates

- **Preserve original timing.** Keep a `raw_cues_ref` (hash/handle to original cues + timestamps) alongside `raw_text` and normalized `text` — normalization bugs usually need the original timing to debug.
- Normalization is **deterministic and versioned** and never silently rewrites spoken wording.
- **VTT dedup** removes rolling auto-caption repetition but is governed by **golden fixtures** so it cannot destroy intentional repetition (lyrics, deliberate repeats). Fixture regressions = build failures.
- **Source-aware quality gates.** Timestamp validity/monotonicity and duration bounds are hard. Repetition ratio, characters-per-second, and language mismatch **warn first** rather than hard-reject — fast speech and lyrics are legitimate. ASR no-speech confidence informs escalation. No naive minimum word count (rejects valid Shorts).

---

## 10. Caching & concurrency

Two cache layers plus separate metadata, because provider/track/model aren't known at initial lookup:

- **Request-result cache** — keyed by `canonical_source + language_policy + policy_hash + normalizer_version + schema_version`. Answers "have we already produced a transcript for this request shape?"
- **Artifact/strategy cache** — keyed by `provider + track_id + media_identity + model_revision + decoding_settings`. Reuses expensive intermediate results across requests.
- **Metadata cache** — separate, with its own refresh policy (≤30-day refresh/delete per Section 4).
- **Negative caching** — **reason-specific TTLs**, not a uniform "short": `removed` can cache long; `rate_limited`/`timeout` very short; `missing_po_token_provider` until config changes.
- **Concurrency** — in-process singleflight always; a **cheap filesystem lock locally too** (a single user can launch two CLI processes; atomic writes prevent corruption but not duplicate downloads). Cross-process locking in the `server` profile.

Reframe v1's "never pulled twice" → **"deduplicated within a run and reused until stale."**

---

## 11. Timeouts, resource & cost controls

- **Global + per-attempt timeouts;** on timeout/cancel, **kill the whole subprocess tree** (process group).
- **Separate CPU and GPU semaphores** — concurrency must not launch N large ASR jobs blindly.
- **Limit enforcement matrix:**

| Dimension | `local` | `server` |
|---|---|---|
| Duration, bytes, cost, timeout | **hard** | **hard** |
| Memory | **advisory** (unless ASR runs in a supervised child process) | **hard** via container/cgroup |
| Disk | preflight check + reserved workspace (race-prone, acknowledged) | same + quota |

Exceeding any limit → `failed: resource_limit_exceeded` with the named `resource_dimension` (not a catch-all `budget_exceeded`).

---

## 12. Interfaces

**Library (async canonical + sync wrapper):**
```python
result = await get_transcript(ref, policy=Policy(...))   # canonical
result = get_transcript_sync(ref, policy=Policy(...))    # wrapper
refs   = await find_videos(channel_id=..., include_shorts=False, max_results=50)
```

**CLI:**
```
transcript pull <url|id|file> [--policy captions-only|prefer-captions|asr-only]
                              [--lang en] [--json] [--force]
transcript pull --file urls.txt --concurrency 4 --out results.jsonl
transcript find --channel <id> --max 50 --format ids     # pipeable into pull --file -
transcript find --query "…" --max 25 --format jsonl
transcript doctor                                        # environment self-check
```
- **Machine output on stdout; progress/logs on stderr.**
- **`find --format ids`** fixes the v1/v2 pipe incompatibility.
- **`transcript doctor`** verifies yt-dlp, JS runtime, EJS components, ffmpeg, PO-token provider plugin, ASR model availability, and file permissions — and prints actionable fixes.
- **Restrict accepted URL schemes/hosts** — an untrusted URL must not silently activate every yt-dlp extractor.

---

## 13. Security

- Subprocesses invoked with **argument arrays, never a shell**.
- **Never derive filenames from remote titles**; content-addressed temp names.
- **Never auto-read browser cookies.** Explicit opt-in, dedicated credentials, restrictive permissions, redacted logs, documented account-risk.
- **No `--no-check-certificates`.** Install the enterprise CA if behind a TLS-intercepting proxy.
- **PO-token provider plugin is a third-party supply-chain dependency** (not affiliated with yt-dlp): pin a specific version, review it, and update deliberately.
- Allowlist schemes/hosts; treat all remote metadata as untrusted input.

---

## 14. Deployment profiles

| Concern | `local` | `server` |
|---|---|---|
| Concurrency | in-process, small | worker pool + CPU/GPU semaphores |
| Singleflight | in-process + filesystem lock | cross-process locks |
| Cache | disk, two-layer, TTLs | shared store, eviction, refresh jobs |
| Memory limits | advisory (or supervised child) | hard via cgroup |
| Proxies | none | optional, policy-gated (post-compliance) |
| Provider canaries | off | scheduled |
| Public-URL capability | flag + explicit policy decision | **release-gated by policy review** |

---

## 15. Tech stack

- **Python 3.11+**, async-first (`anyio`/`asyncio`).
- **Captions/media:** `youtube-transcript-api`, `yt-dlp` **+ external JS runtime (Deno/Node) + `yt-dlp-ejs`** (bundled in official binaries; pip installs set up the runtime and, if needed, a **PO-token provider plugin** such as `bgutil-ytdlp-pot-provider` — pinned/reviewed). `ffmpeg` recommended **for yt-dlp**, not for faster-whisper.
- **ASR:** `faster-whisper` (PyAV + CTranslate2); pinned model **revision** + **compute_type**.
- **ASR testing:** `jiwer` (WER/CER regression).
- **Discovery:** `google-api-python-client` or `httpx`.
- **Plumbing:** `pydantic` (schema/outcome), `tenacity` (retry only on retry-eligible reasons), `typer`+`rich` (CLI), `python-dotenv`, `platformdirs`.

---

## 16. Build phases (profile-aware)

Production hardening, packaging, locking, provider contracts, security, and compliance are **not** half-day tasks. Estimates assume one experienced builder.

| Phase | Deliverable | Profile | Rough |
|---|---|---|---|
| **0** | **Rights/compliance spike** (public-URL capability) **+ real-world reliability bakeoff**; runs in parallel with Phase 1 on uploaded media; gates the public-URL path. | both | spike |
| 1 | Core: schema/outcome model (+`availability_scope`/`retry`), policy engine, preflight (hints), normalization + fixtures, two-layer cache skeleton, CLI/library, **`uploaded_file`** end to end | local | ~2 days |
| 2 | `api_captions` + source-aware quality gates + request-result cache + singleflight + filesystem lock | local | ~1 day |
| 3 | `ytdlp_subs` (JS runtime + PO-token + EJS setup, cookies opt-in) + robust VTT parse/dedup + `transcript doctor` | local | ~1.5–2 days |
| 4 | `local_whisper` (preload, VAD, pinned revision, semaphores, timeouts/tree-kill) **+ `jiwer` regression set** | local | ~1.5–2 days |
| 5 | `managed_native` + `managed_asr` + `managed_url_to_asr` (key-gated) | local | ~1 day |
| 6 | Discovery (resolution, traversal semantics, **dual-bucket** budgets, batched enrichment, persisted params) | local | ~1 day |
| 7 | `server` profile: cross-process locking, eviction/refresh, hard limits, canaries, proxy hooks (post-compliance), packaging | server | several days |
| 8 | Failure-injection + provider-canary suites; security review | both | several days |

Usable personal tool = **Phases 1–4** (+ Phase 0 gating the public-URL path).

---

## 17. Acceptance criteria / SLOs

- **Scoped SLO:** success rate + p95 latency **by acquisition path** over *publicly accessible, supported, non-live, speech-bearing, permitted* media — not "every video with speech."
- **Dual-bucket budgeting** verified: search and the general pool tracked independently against Cloud Console limits.
- Duplicate concurrent requests cause **one** underlying acquisition (in-process + filesystem-lock test).
- Caption normalization passes **fixed VTT fixtures**; dedup preserves intentional repetition.
- **ASR regression gate:** `jiwer` WER/CER on 5–10 licensed clips per supported language stays within **generous** thresholds; the gate fires on model/compute-type/VAD/decoding changes. (A research-grade multilingual benchmark remains out of scope.)
- **Failure-injection matrix passes:** 429, 5xx, timeout, corrupt cache, full disk, missing dependency (no JS runtime / no ffmpeg / no PO-token provider), subprocess hang, cancellation, provider outage.
- **Resource ceilings enforced** per the Section 11 matrix; `resource_limit_exceeded` reports the named dimension.
- `server` profile runs **scheduled provider canaries** outside unit tests.

---

## 18. Future extensions
- Speaker diarization (segment schema accommodates it).
- Cross-ID **audio-fingerprint** ASR reuse (deferred from core).
- Downstream pipeline push: run extraction (feature mentions, competitor names, intent signals) on each transcript → existing store, turning video into the same structured signal as call transcripts.
- Non-YouTube sources (Vimeo, Loom, podcasts) — Stage 3 generalizes; add source-specific acquirers.
- Incremental channel monitoring (only new uploads since last run).
