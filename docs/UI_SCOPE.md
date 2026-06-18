# Batch Transcript Web App — v1 scope (canonical reference)

A local web UI over the existing engine. **Paste links → review → one button → one
Markdown file.** Local & private, residential IP (no proxy tax), honest provenance +
reasons, captions-first waterfall. A thin UI over a finished engine — not a new product.

## v1 in / out
**In:** forgiving multiline paste (watch / `youtu.be` / Shorts / embed / bare 11-char id;
dedupe preserving order; client + server validation); explicit "Create N transcripts"
button (never auto-start); per-video rows with independent failures + Retry; provenance
badges; one Markdown file by default (or separate/ZIP) in pasted order; real "N of M"
progress announced via `role="status"`; captions-first default with ASR/managed an
explicit per-batch opt-in showing an estimated `Cost`.

**Out (v1):** accounts, transcript editing, AI summaries, playlist expansion,
channel/keyword discovery in-UI (engine has `find`, but v1 is paste-only), permanent
library, multi-user/hosting.

## Engine → UI mapping (the UI is mostly a translation layer over `Result`)
**Provenance → badge:** `human_caption`→Human captions · `platform_auto`→Auto captions ·
`local_asr`→Local ASR · `managed_asr`→Managed ASR · `translated_caption`→Translated (auto).

**Reason → row copy + Retry** (drive Retry off `result.retry.eligible`, NOT the reason
string — the engine separates them):

| reason | message | retry |
|---|---|---|
| captions_unavailable | No captions available for this video. | only if ASR enabled |
| language_unavailable | Captions exist, but not in your language(s). | — |
| no_acceptable_transcript | A transcript was found but failed quality checks. | — |
| no_speech | No speech detected in the audio. | — |
| private / removed | This video is private / removed. | — |
| members_only / age_restricted / geoblocked | Not accessible (members/age/region). | — |
| live | Live stream — try again once archived. | later |
| bot_gated / access_challenge | YouTube blocked the request (anti-bot). | yes |
| rate_limited / timeout / provider_error / audio_download_failed | Temporary — retry. | yes |
| po_token_rejected / missing_* | Setup issue — see `transcript doctor`. | after fix |
| invalid_input | Not a valid YouTube link or file. | — |
| resource_limit_exceeded | Hit a size/time limit. | — |

## Architecture
Local FastAPI app wrapping the library, server-rendered (HTMX) first. SQLite for durable
Job + per-VideoItem status; a **separate worker process** runs `get_transcript` (ASR is
heavy); **SSE** pushes row updates; `GET /jobs/{id}/transcripts.md` assembles Markdown on
demand (partial download works). `EgressPolicy.allow_public_url` = one-time acknowledged
config setting (not a per-batch nag). ASR/managed = per-batch opt-in with a cost line.

## §7 Preconditions (MUST close before attaching the UI) — UI-0
1. **Cache lock robustness** — the local lock must not deadlock if a holder dies. → done:
   `FileLockBackend` now uses `fcntl.flock` (kernel-released on death) + bounded timeout.
2. **Real ASR cancellation** — a `wait_for` around `to_thread` leaves Whisper running. →
   done: `cancellable.run_in_process` runs ASR in a killable child; `LocalWhisperStrategy(use_process=True)`.

## Markdown output (default: one file, pasted order)
YAML front-matter (`generated_at`, `videos_requested/succeeded/failed`,
`language_preferences`) → `# Video Transcripts` → per-video section (source link, channel,
language, provenance, duration) → `### Transcript` with clickable `[mm:ss](…&t=Ns)` links
every 30–60s (cue-by-cue is an advanced option) → `## Items that could not be transcribed`
listing each failure + plain reason.

## Build phases (acceptance-gated)
- **UI-0** close §7 preconditions — *duplicate-link batch + a cancel don't freeze a job.* ← (this branch)
- **UI-1** FastAPI paste → validate (client+server) → synchronous small-batch pull → `.md` download.
- **UI-2** SQLite job model + worker process + SSE live per-row status (refresh-survivable, independent failures).
- **UI-3** Markdown assembler + `/jobs/{id}/transcripts.md` + partial download + badges + reason copy.
- **UI-4** Retry / retry-all / cancel, captions-first + ASR opt-in w/ cost, a11y pass (WCAG 4.1.3, keyboard).

Usable v1 = UI-0 → UI-3; UI-4 is polish.
