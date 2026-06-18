# transcript-tool

Staged, policy-driven video transcript acquisition. Caption strategies first,
audio ASR as the floor. This repository is the **Phase 1 starter**: the compliance-
safe, offline `uploaded_file` slice, with later strategies stubbed and labelled.

- **Start here:** `CLAUDE.md` (working agreement) and `docs/PHASE_1_BUILD.md` (current task).
- **Full spec:** `docs/DESIGN.md`.

## Quickstart
```
pip install -e ".[dev]"        # or: pip install pydantic pytest
pytest -q
transcript pull tests/fixtures/rolling_autocaption.vtt
transcript doctor
```

## Status
**Phases 1–4 built and green** (26 tests). Caption files (`uploaded_caption`),
YouTube captions via `api_captions` (youtube-transcript-api) and `ytdlp_subs`
(yt-dlp), and ASR via `local_whisper` (faster-whisper) with a `jiwer` regression
harness. Strategies are unit-tested via dependency injection; live YouTube / real
model paths are verified on a real machine. Phases 5–8 (managed providers,
discovery, server profile, hardening) are stubbed. See `docs/DESIGN.md §16`.

```
# captions from a file
transcript pull subtitles.vtt
# transcribe an audio file (needs a provisioned faster-whisper model)
transcript pull talk.m4a
# YouTube URL (gated capability — explicit opt-in)
transcript pull "https://youtu.be/VIDEO_ID" --enable-public-url
```
