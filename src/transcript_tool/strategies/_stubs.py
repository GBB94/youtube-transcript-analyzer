"""Stubs for strategies built in later phases. They are importable so the policy
engine and registry are complete, but raise NotImplementedError until built.

Each carries the contract notes Claude Code needs to implement it correctly.
"""
from __future__ import annotations

from ..policy import Policy
from ..schema import Result, VideoRef


class _Unbuilt:
    name = "unbuilt"
    phase = "?"

    def applicable(self, ref: VideoRef, policy: Policy) -> bool:
        return self.name in policy.enabled_strategies

    async def fetch(self, ref: VideoRef, policy: Policy) -> Result:
        raise NotImplementedError(f"{self.name} is a Phase {self.phase} strategy")


class ApiCaptionsStrategy(_Unbuilt):
    """Phase 2 — youtube-transcript-api. Returns existing caption tracks.
    Falls through with captions_unavailable / language_unavailable; map IP blocks
    to access_challenge (contextual), not a permanent reason."""
    name = "api_captions"
    phase = "2"


class YtdlpSubsStrategy(_Unbuilt):
    """Phase 3 — yt-dlp subtitles. REQUIRES an external JS runtime (Deno/Node) and
    may require a pinned PO-token provider plugin. Cookies opt-in only. Always pass
    `--` before the URL. Parse .vtt via normalize.parse_vtt + dedupe_rolling."""
    name = "ytdlp_subs"
    phase = "3"


class LocalWhisperStrategy(_Unbuilt):
    """Phase 4 — faster-whisper (PyAV decode, no system ffmpeg). Model is
    pre-provisioned+checksummed; load lazily on first ASR use (local) or warm at
    startup (server) — NEVER download mid-request. Enable VAD; capture
    language probability + no-speech. provenance=local_asr."""
    name = "local_whisper"
    phase = "4"


class ManagedNativeStrategy(_Unbuilt):
    """Phase 5 — managed provider, captions-only/native mode (must not silently
    bill paid ASR). Additional capacity, not 'blocking-proof'."""
    name = "managed_native"
    phase = "5"


class ManagedAsrStrategy(_Unbuilt):
    """Phase 5 — managed ASR over acquired media. provenance=managed_asr."""
    name = "managed_asr"
    phase = "5"


class ManagedUrlToAsrStrategy(_Unbuilt):
    """Phase 5 — COMPOUND: provider takes a URL and returns a transcript with no
    intermediate local media artifact. Model distinctly from media+local ASR."""
    name = "managed_url_to_asr"
    phase = "5"
