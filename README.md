# transcript-tool

Two halves of one system:

1. **Acquisition ("the pull half") — built.** Staged, policy-driven video
   transcript acquisition. Caption strategies first, audio ASR as the floor.
2. **Corpus & query ("the ask half") — specified, not yet built.** Turn the pulled
   transcripts into a durable, queryable knowledge base: ask a question, get an
   answer grounded in what was said, with a citation back to the episode and exact
   timestamp. Spec: **`docs/RETRIEVAL_DESIGN.md`**.

- **Start here:** `CLAUDE.md` (working agreement), `docs/DESIGN.md` (acquisition
  spec), and `docs/RETRIEVAL_DESIGN.md` (retrieval spec).
- **Phase 1 task notes:** `docs/PHASE_1_BUILD.md`.

## Batch web UI (local)
```
pip install -e ".[web]"
transcript serve            # http://127.0.0.1:8000
```
Paste YouTube links → live per-row progress → download one Markdown file. Captions-first,
local, and private. Durable SQLite jobs (refresh-survivable), a separate worker process,
live SSE updates, per-item **Retry** / **Retry all failed** / **Cancel** (cancel actually
kills in-flight compute), an opt-in audio-transcription fallback with a cost note, and an
accessible UI (`role="status"` live regions, labels, keyboard). Scope: `docs/UI_SCOPE.md`.

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

### Corpus & query (the ask half) — specified, not yet built
Design complete in `docs/RETRIEVAL_DESIGN.md`. Realizes the downstream direction
from `DESIGN.md §18` as open-ended retrieval-augmented Q&A. Core principles:
canonical transcripts are the source of truth and are pulled once; chunks,
embeddings, and indexes are **derived and rebuildable with no network access**;
retrieval is hybrid (dense + BM25 in one LanceDB store) with a rerank stage;
answers are **grounded** with episode+timestamp deep links or an explicit
`insufficient_evidence` outcome;
a built-in eval harness (recall@k / MRR / faithfulness) makes retrieval changes
measured, not vibe-checked. Build phases **R0–R6** (R0 = bank the canonical
corpus). Nothing here is implemented yet.

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
