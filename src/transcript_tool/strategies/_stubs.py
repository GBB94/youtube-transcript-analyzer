"""Stubs for strategies still to be built (Phase 5+). Importable so the registry
is complete; raise NotImplementedError until built. api_captions/ytdlp_subs/
local_whisper are now real modules (see their files)."""
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
    intermediate local media artifact."""
    name = "managed_url_to_asr"
    phase = "5"
