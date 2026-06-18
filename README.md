# transcript-tool

Staged, policy-driven video transcript acquisition. Caption strategies first,
audio ASR as the floor.

- **Start here:** `CLAUDE.md` (working agreement) and `docs/DESIGN.md` (full spec).
- **Phase 1 task notes:** `docs/PHASE_1_BUILD.md`.

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
- **Phases 5–8** (managed providers, discovery, server profile, hardening suites)
  are stubbed with contract docstrings. See `docs/DESIGN.md §16`.

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
