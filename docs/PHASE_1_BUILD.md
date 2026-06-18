# Phase 1 — task prompt for Claude Code

> Hand this file to Claude Code as the current task. `CLAUDE.md` holds the durable
> rules; `docs/DESIGN.md` is the full spec. This phase is **scaffolded and passing**
> — your job is to verify it, harden the edges listed below, then proceed to the
> Phase 2 task at the bottom. Do **not** start Phase 2 work until Phase 1's
> acceptance criteria are green.

## Scope (the compliance-safe vertical slice)
Operator supplies a caption/subtitle file (`.vtt`/`.srt`); the pipeline returns a
normalized, validated `Result`. **No network, no platform access, no ASR.** ASR over
operator-supplied *audio* is Phase 4 (`local_whisper`). This slice exists to prove
the whole skeleton — schema, policy, preflight, normalization, quality gates, cache,
orchestrator, CLI — end to end on the safest possible input.

## Definition of done (acceptance criteria)
1. `pytest -q` is fully green, including:
   - VTT rolling-overlap dedup matches the golden fixture **and** intentional
     repetition (chorus fixture) is preserved.
   - Outcome-model invariants hold (success⇒text/provenance & no reason;
     non-success⇒reason & no transcript; `resource_limit_exceeded`⇒dimension).
   - End-to-end `uploaded_caption` success on a fixture.
   - A cache hit is **labelled** and **not replayed** (`attempts == []`,
     `served_from_cache is True`).
   - Missing file ⇒ `failed/invalid_input`.
2. `transcript pull <caption-file>` prints the `Result` JSON to **stdout** and logs to
   **stderr**; exit code 0 on success, 1 on non-success, 2 on usage error.
3. `transcript doctor` reports environment readiness without throwing.
4. No rule in `CLAUDE.md` is violated (run a quick self-audit against the Golden Rules).

## Harden before moving on (small, in-scope)
- Add `.srt`-specific fixtures (the parser is tolerant, but prove it).
- Add a fixture where **all candidates fail a hard gate** (e.g. inverted
  timestamps) and assert the result is `unavailable/no_acceptable_transcript` with
  the gate captured in `quality`.
- Confirm `policy_hash` changes when languages / enabled strategies / quality config
  / egress policy change (add a test).
- Confirm corrupt-cache recovery: write garbage into a results file and assert
  `get()` treats it as a miss and removes it.

## Explicitly NOT in this phase
- Any network call, YouTube access, yt-dlp, or ASR.
- The `server` deployment profile (cross-process locks, eviction, canaries).
- Discovery (`find`) beyond the existing skeleton.

---

## Next task — Phase 2 prompt (`api_captions`)
> Start only after Phase 1 acceptance is green.

Implement the `api_captions` strategy (`strategies/api_captions.py`) using
`youtube-transcript-api`:
- It is a **caption strategy** that returns existing tracks for a `url` ref when
  `EgressPolicy.allow_public_url` is enabled (still gated — see `DESIGN.md §4`).
- Honor the language **preference list**; if no requested language is available and
  the policy doesn't allow fallback, return `unavailable/language_unavailable`. If no
  captions at all, `captions_unavailable`. Map an IP block / bot wall to
  `failed/access_challenge` (contextual), **not** a permanent reason.
- Populate `Language` correctly: `selected`, `track_language`, and `is_generated` →
  `provenance` (`human_caption` vs `platform_auto`). Only set `translated_caption`
  with provider-disclosed evidence (`detection_method`), never inference.
- Run output through the same `normalize` + `quality` path; record an `Attempt` with
  latency and a `Cost` (unit `none` for this provider).
- Add tests with **recorded/fixture** API responses (no live network in CI).
- Keep the public-URL path gated; do not enable it by default.

Acceptance: unit tests for the happy path, language-unavailable, captions-unavailable,
and access-challenge mapping; the strategy slots into the orchestrator behind
`uploaded_caption` per policy order; `transcript pull <youtube-url>` works when the
capability is explicitly enabled.
