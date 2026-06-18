# transcript-tool

Staged, policy-driven video transcript acquisition. Caption strategies first,
audio ASR as the floor.

- **Start here:** `CLAUDE.md` (working agreement) and `docs/DESIGN.md` (full spec).
- **Phase 1 task notes:** `docs/PHASE_1_BUILD.md`.

## Batch web UI (local)
```
pip install -e ".[web]"
transcript serve            # http://127.0.0.1:8000
```
Paste YouTube links → review → download one Markdown file. Captions-first, local, and
private. Scope + build phases: `docs/UI_SCOPE.md`. (UI-1: synchronous paste→Markdown;
the SQLite job model + worker + live SSE updates land in UI-2+.)

## Quickstart
```
pip install -e ".[dev]"        # or: pip install pydantic pytest
pytest -q
transcript pull tests/fixtures/rolling_autocaption.vtt
transcript doctor
```

## Status
**Phases 1–4 implemented, unit-tested via injected adapters, and live-verified on
macOS ARM (CPU).**

- **Phase 1 (`uploaded_caption`)** — complete and green, including the hardening
  suite (`.srt` fixtures, all-gates-fail, `policy_hash` invalidation, corrupt-cache
  recovery).
- **Phase 2 (`api_captions`, youtube-transcript-api)** and **Phase 3 (`ytdlp_subs`,
  yt-dlp)** — unit-tested via dependency injection (fake client / runner) **and**
  smoke-verified against a real public YouTube video.
- **Phase 4 (`local_whisper`, faster-whisper)** — unit-tested with a fake
  transcriber **and** verified end-to-end against the real `small` model on real
  audio (lazy local-only load, no mid-request download).
- **Phase 5 (managed providers: `managed_native` / `managed_asr` / `managed_url_to_asr`)**
  and **Phase 6 (discovery / `find`)** — implemented and unit-tested via injected
  clients against recorded fixtures (no live network / quota in CI). Live verification
  against a real managed provider and the YouTube Data API is **pending** (needs
  `MANAGED_API_KEY` / `YOUTUBE_API_KEY`). See `docs/PHASE_5_6_BUILD.md`.
- **Phase 0 (release gate)** — bakeoff harness (`transcript bakeoff`) + drafted
  `docs/COMPLIANCE.md` and `docs/SLO.md`. Legal sign-off and the real-hardware bakeoff
  numbers are human/infra-gated (this environment is IP-blocked).
- **Phase 7 (server profile)** — seams in place: `profiles.py` (limits + enforcement),
  `locking.py` (pluggable lock backend; distributed-singleflight contract tested),
  `provisioning.warm()`. The real datastore/container/cgroup wiring is deploy-time.
- **Phase 8 (hardening)** — failure-injection suite is green; `docs/SECURITY_REVIEW.md`
  audit done. Live canaries + SLO-conformance vs. real numbers remain infra-gated.

Verification is environment-specific: the unit suite is the portable guarantee
(`pytest -q`); the live/real-model paths depend on local runtime deps. Run
`transcript doctor` to see per-strategy readiness on a given machine. Dependencies
are pinned in `uv.lock` for reproducible installs.

```
# captions from a file
transcript pull subtitles.vtt
# transcribe an audio file (needs a provisioned faster-whisper model)
transcript pull talk.m4a
# YouTube URL (gated capability — explicit opt-in)
transcript pull "https://youtu.be/VIDEO_ID" --enable-public-url
```
