# Compliance, acquisition & retention policy (Phase 0 — Workstream A)

> **STATUS: DRAFT for human approval.** This document is drafted by engineering; the
> acquisition/retention model and the public-URL release sign-off are **human/legal
> decisions** (counsel for any commercial use). A deployment profile is **not** a
> Terms-of-Service exemption. Nothing here flips `EgressPolicy.allow_public_url` for
> anyone but the operator until the sign-off line at the bottom is completed.

## 1. Capability posture (what we extract, and on what authority)
- **Uploaded / licensed media** — operator supplies the file and **asserts they hold
  sufficient rights**; the tool validates the file but cannot verify licensing.
  *(Status: shipping — `uploaded_caption`, `local_whisper` on local audio.)*
- **Owned content via authorized APIs** — YouTube Data API for discovery (metadata),
  `api_captions` for tracks. Authorized, lower-risk than scraping.
- **Arbitrary public URLs** — **OFF by default.** Gated behind an explicit policy
  decision tied to a specific, approved use (`EgressPolicy.allow_public_url` +
  CLI `--enable-public-url`). This is the capability this document gates.

## 2. No ToS-circumvention defaults (verified in code — see SECURITY_REVIEW.md)
- No residential proxies; no geo-bypass defaults.
- **Never** `--no-check-certificates` — install the enterprise CA instead.
- Cookies are **opt-in only**, dedicated credentials, restrictive perms, redacted
  logs; browser cookies are never auto-read.
- The PO-token provider plugin is a pinned, reviewed third-party supply-chain
  dependency, as are any managed-provider SDKs.

## 3. Retention & deletion
- **Transcripts / results cache:** retained per operator policy; success entries until
  stale/evicted, failures on reason-specific negative TTLs (`cache.py`).
- **Discovery metadata:** stored in the **separate metadata cache** with a **≤30-day**
  refresh/delete policy (YouTube developer-policy requirement). Enforced by
  `Cache.put_metadata` (TTL clamped to 30 days); the server profile runs the
  refresh/delete as a scheduled job (Phase 7).
- **Media artifacts:** temp audio is content-addressed and cleaned up after use; never
  named from remote titles.

## 4. Privacy & copyright
- **PII in transcripts:** transcripts may contain personal data; treat as sensitive,
  apply the retention policy, support deletion on request. *(Process owner: TBD.)*
- **Copyrighted content:** extraction does not confer rights; downstream use is the
  operator's responsibility. Document permitted uses for the approved deployment.
- **Attribution:** record source `VideoRef` + provenance on every result.

## 5. Supply-chain review (Phase 0 + ongoing)
- Pin and review: PO-token provider plugin, managed-provider SDK(s),
  `youtube-transcript-api`, `yt-dlp` (fast-moving — budget for bumps), `faster-whisper`.
- Re-review on every dependency bump; canaries (Phase 8) catch upstream drift.

## 6. Exit criteria (Phase 0 release gate)
- [ ] Acquisition/retention/policy model **approved** (counsel for commercial use).
- [ ] PII handling + deletion process owner assigned.
- [ ] Supply-chain pins reviewed.
- [ ] `docs/SLO.md` populated from a real bakeoff on reference hardware.

---

### Public-URL release sign-off
By signing, the approver authorizes enabling `EgressPolicy.allow_public_url` for the
named deployment, consistent with the posture above.

- Deployment / use: ____________________
- Approver (name/role): ____________________   Date: __________
- Scope & expiry: ____________________
