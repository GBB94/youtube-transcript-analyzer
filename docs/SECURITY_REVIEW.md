# Security review (Phase 8 — checklist audit)

Audit of the current tree against the Phase 8 security checklist. Each item lists the
status and where it's enforced. Re-run on any change to `security.py`, the subprocess
strategies, or the managed/discovery network paths.

## Subprocess discipline
- ✅ **Argument arrays, never a shell.** `security.build_subprocess_args` returns a
  list; `ytdlp_subs._real_runner` and `media.acquire_audio` call `subprocess.run(args,
  …)` with no `shell=True`.
- ✅ **`--` before untrusted positionals.** `build_subprocess_args` inserts `--` before
  the URL, so a value like `-foo` or a URL beginning `-` cannot be parsed as a flag.
- ✅ **No remote-title filenames.** Output templates use `%(id)s.%(ext)s`;
  `security.safe_temp_name` is content-addressed. Titles are never used for paths.

## URL / input trust
- ✅ **Scheme + host allowlist.** `security.assert_safe_url` rejects non-http(s) and,
  when `allowed_hosts` is set, off-allowlist hosts. Called by `ytdlp_subs` and
  `media.acquire_audio` before any fetch.
- ✅ **Remote metadata treated as untrusted.** Discovery enrichment reads only typed
  fields; missing/extra fields are ignored, missing ids dropped (not errored).
- ✅ **Public-URL gating.** All network strategies require `EgressPolicy.allow_public_url`;
  preflight returns a config failure (not a content reason) when the gate is off.

## Cookies
- ✅ **Opt-in only, never auto-read.** No browser-cookie reading anywhere; `ytdlp_subs`
  omits cookies unless explicitly configured. Account-risk documented in
  COMPLIANCE.md §2.
- ✅ **No `--no-check-certificates`** anywhere in the tree (enterprise CA is the
  documented path). Verified by grep.

## Secrets & observability
- ✅ **Keys never logged.** Managed `HttpxClient` sends the key as a Bearer header and
  never logs it; the key is read from `MANAGED_API_KEY`.
- ✅ **Redaction.** `security.redact` scrubs `token=/key=/secret=/cookie=/authorization=`
  **and** `Authorization: Bearer <token>` (the bearer pattern was added in Phase 5
  after a test showed the space after "Bearer" leaked the token). `Attempt.provider_request_id`
  is redacted on capture.
- ⚠️ **Follow-up:** redaction is best-effort string scrubbing. For the server profile
  (Phase 7) prefer structured logging that never serializes secret fields in the first
  place, rather than relying on post-hoc redaction.

## Resource & temp hygiene
- ✅ **Atomic cache writes.** `Cache._atomic_write` uses tempfile + `os.replace`; a crash
  mid-write leaves a temp file, never a half-written result. Corrupt entries are treated
  as a miss and removed (`get`).
- ⚠️ **Temp-media cleanup.** `ytdlp_subs` / `media.acquire_audio` create temp dirs that
  are not always unlinked after use. **Action (Phase 7):** ensure work dirs are removed
  in a `finally`, and use a reserved, mode-restricted workspace on the server.
- ⚠️ **Subprocess-tree kill.** `subprocess.run(timeout=…)` kills the direct child but not
  necessarily a process tree (yt-dlp spawns helpers). **Action (Phase 7):** process-group
  kill on timeout/shutdown.

## Supply chain
- ✅ **Pins exist** via `pyproject.toml` extras + `uv.lock` (62 packages).
- ⚠️ **Review cadence:** PO-token plugin and managed SDK are unaffiliated third parties;
  COMPLIANCE.md §5 owns the review-on-bump process. `yt-dlp` moves fast — canaries (below)
  are the early-warning.

## Sandboxing
- ⚠️ **Untrusted-media decode** runs in-process locally. **Action (Phase 7):** decode in a
  supervised child/container (defense-in-depth) for the server profile.

## Platform matrix
- ✅ **Tested target:** macOS ARM (CPU) — unit suite green; P2–P4 live-verified.
- ⚠️ Linux x86-64 is best-effort; process-tree kill, filesystem locking, GPU, and memory
  enforcement are OS-specific and gated. Verify on the server target before release.

## Sign-off
- Reviewer: ____________________  Date: __________
- Open ⚠️ items tracked into Phase 7/8: temp cleanup, process-tree kill, structured-log
  redaction, media-decode sandbox, Linux matrix verification.
