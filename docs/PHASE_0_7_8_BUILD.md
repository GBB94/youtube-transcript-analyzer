# Phases 0, 7, 8 — the remaining phases (release track)

> The productization tail. `CLAUDE.md` holds the durable rules; `docs/DESIGN.md` is
> the spec (§3 reliability boundary, §4 compliance, §10 cache, §11 limits, §14
> profiles, §16 roadmap, §17 SLOs). These phases turn the working single-operator
> tool into something releasable.
>
> **Ordering is non-linear.** Phase 0 is numbered first because it *gates release*,
> but it runs in parallel with engineering and only has to finish **before any public
> or commercial release**. Phases 7 → 8 are roughly sequential, though Phase 8's test
> harnesses can be built alongside Phase 7.
>
> **None of this is needed for personal file/URL use** (Phases 1–4 cover that).
> Build these only if you're taking it toward a shared or commercial product.

---

## Phase 0 — Rights/compliance spike + reliability bakeoff + SLO thresholds

This is the **release gate**. Part of it is engineering Claude Code can do; part is
human decisions (legal, policy, vendor) that code can't make. Both must land before
the public-URL capability is enabled for anyone but you.

### Workstream A — Rights & compliance (human-led; Claude Code drafts artifacts)
- **Decide the acquisition + retention model** for the public-URL capability and get
  it approved (counsel for any commercial use). Recall: a deployment profile is **not**
  a Terms exemption.
- **Capability posture:** uploaded/licensed media (operator asserts rights), owned
  content via authorized APIs, arbitrary public URLs **off by default** behind an
  explicit policy decision tied to the use.
- **Confirm no ToS-circumvention defaults** ship: no residential proxies, no
  geo-bypass, no TLS-verification disabling; cookies opt-in only with dedicated creds.
- **Write the policy set:** retention, deletion, attribution, privacy (PII in
  transcripts), copyrighted-content handling. Enforce the ~30-day metadata refresh in
  the metadata cache.
- **Supply-chain review:** pin and review the PO-token provider plugin and any managed
  provider SDKs (third-party, unaffiliated).
- **Deliverable:** a short, written, approved acquisition/retention/policy document,
  plus the explicit sign-off that flips `EgressPolicy.allow_public_url` for release.

### Workstream B — Reliability bakeoff (Claude Code builds the harness; run on a real machine)
- Assemble a **representative corpus** spanning the real distribution: human-captioned,
  auto-captioned, captions-disabled, language-mismatch, blocked/bot-gated,
  members-only, age-restricted, geoblocked, live, no-speech, Shorts, and ≥2 languages.
  Use only content you have the right to test against.
- Build a **bakeoff runner** that executes the real pipeline (on a real machine — this
  sandbox is IP-blocked) and records, **by acquisition path**: success/unavailable/
  failed counts + reasons, latency (p50/p95), and cost.
- **Deliverable:** an empirical report of what actually succeeds and how fast/expensive.

### Workstream C — SLO thresholds (turn measurements into acceptance gates)
- Convert the bakeoff numbers into **numeric targets** Phase 8 will assert against, e.g.:
  - caption-path success p95 latency < **X** s
  - local ASR real-time factor < **Y** on **named** reference hardware
  - public-eligible-corpus success rate ≥ **Z** %
  - max expected cost per processed hour ≤ **$N**
- **Deliverable:** a checked-in `docs/SLO.md` with the numbers and the reference
  hardware. ("Measure latency" is telemetry; these are the *gates*.)

### Exit criteria (Phase 0)
- Approved compliance/retention/policy doc on file; `allow_public_url` release sign-off.
- Bakeoff report exists and was run on real hardware.
- `docs/SLO.md` exists with concrete thresholds + reference hardware.

---

## Phase 7 — Server / multi-worker profile

Same core, the `server` deployment profile (`DESIGN.md §14`). Build behind the profile
switch so the `local` path stays simple.

### Components
- **Distributed singleflight + locking:** replace the local filesystem lock with a
  shared lock (Redis/DB). Take the per-request-key lock, **re-check the cache after
  acquiring it** (the contract from `cache.py` — preserve it across processes).
- **Shared cache store:** swap the disk store for SQLite/Postgres/Redis behind the
  existing cache interface. Implement **eviction + size controls** and the
  **metadata-refresh jobs** (≤30-day refresh/delete). Keep the two-layer split
  (request-result vs artifact) and reason-specific negative TTLs.
- **Hard resource enforcement:** memory becomes **hard** via container/cgroup limits
  (advisory-only locally); disk quotas + reserved workspace; per-job duration/bytes/
  cost/timeout enforced. Exceeding any ⇒ `resource_limit_exceeded` with the named
  `resource_dimension`.
- **Worker pool + semaphores:** CPU/GPU semaphores sized for the box; **warm ASR
  workers at startup** (provision + load the pinned model once — never download
  mid-request).
- **Shared quota accounting:** the discovery **dual-bucket** budget (search vs general
  pool) tracked across workers, not per-process.
- **Proxy hooks:** optional, **policy-gated, post-compliance** (Phase 0). No defaults.
- **Untrusted-media decode** in a supervised child/container (defense-in-depth).
- **Packaging/deploy:** container image, config schema, healthcheck/readiness probe
  (reuse `transcript doctor`), structured logging with redaction, graceful shutdown
  that kills subprocess trees.

### Acceptance (Phase 7)
- Two concurrent processes hitting the same key cause **one** underlying acquisition
  (distributed singleflight test).
- Memory/disk ceilings demonstrably enforced (container limits) ⇒ correct
  `resource_limit_exceeded` dimension.
- Cache eviction + ≤30-day metadata refresh run as jobs; integrity preserved (no
  dangling `raw_cues_ref`).
- ASR workers warm at startup; a request never triggers a model download.
- Shared dual-bucket quota tracked correctly across workers.
- `transcript doctor` works as a readiness probe.

### Not in this phase
- Choosing a specific datastore/orchestrator is a deployment decision; keep the
  interfaces swappable.

---

## Phase 8 — Hardening: failure-injection, canaries, security review

### Failure-injection suite (automated)
Cover every failure mode with deterministic tests (fault injection, not luck): 429,
5xx, request timeout, corrupt cache entry, full disk, missing dependency (no JS
runtime / no ffmpeg / no PO-token provider / unprovisioned model), subprocess hang,
cancellation, managed-provider outage, and PO-token rejection. Each must produce the
**correct reason** and leave the system in a clean state (no orphan processes, no
half-written cache).

### Provider canaries (scheduled, live)
- Small, rate-limited live health checks **outside** the unit suite, for the paths most
  prone to silent upstream drift: `api_captions`, `ytdlp_subs` (JS-runtime/PO-token),
  and each managed provider.
- Alert on success-rate or latency drift vs. the Phase 0 SLOs. These catch YouTube/
  provider changes before users do.

### SLO conformance tests
- Assert against `docs/SLO.md`: success rate + p95 latency **by acquisition path**, the
  **`jiwer` WER/CER regression** on the licensed clip corpus (the change-detection
  guard — generous thresholds), and the cost-per-hour ceiling.

### Security review (checklist audit)
- Subprocesses: argument arrays only, `--` before untrusted positionals, no shell.
- URL scheme/host allowlist; remote metadata treated as untrusted; no remote-title
  filenames.
- Cookies: opt-in, dedicated creds, restrictive perms, redacted logs, account-risk
  documented. **No `--no-check-certificates`** (enterprise CA instead).
- Secrets: keys never logged; request IDs redacted. Temp-file cleanup; secure temp
  dirs. Supply-chain pins (PO-token plugin, provider SDKs) reviewed.
- Untrusted-media decode sandboxed (server profile).
- **Supported-platform matrix** verified: the tested targets pass; untested platforms
  are explicitly best-effort and platform-specific code is gated.

### Acceptance (Phase 8)
- Full failure-injection matrix green and each fault maps to the right reason with no
  resource leaks.
- Canaries running on a schedule with alerting wired.
- SLO conformance suite green against `docs/SLO.md`.
- Security checklist signed off; platform matrix verified.

---

## Definition of "production-ready v1" (release gate)
Releasable when **all** hold:
1. **Phase 0** compliance/retention/policy approved and `allow_public_url` signed off;
   `docs/SLO.md` populated from a real bakeoff.
2. **Phase 7** server profile passing its acceptance criteria on the target deploy.
3. **Phase 8** failure-injection + SLO-conformance green, canaries live, security
   checklist + platform matrix signed off.

### Effort (one experienced builder)
| Phase | Rough |
|---|---|
| 0 — compliance spike + bakeoff harness + SLO doc | spike (legal-dependent) + ~2–3 days harness |
| 7 — server profile | several days–1.5 weeks (datastore + limits + packaging) |
| 8 — hardening | several days (suites + canaries + security audit) |

### The honest caveat (carry it past release)
This category is never set-and-forget. YouTube actively changes the rules (PO tokens,
JS-runtime requirements, SABR) and providers change upstream access. The waterfall
architecture exists to **absorb** that churn so one change doesn't take the tool down —
but budget for ongoing dependency bumps and canary-driven fixes even after Phase 8.
"Done" here means low-maintenance, not zero-maintenance.
