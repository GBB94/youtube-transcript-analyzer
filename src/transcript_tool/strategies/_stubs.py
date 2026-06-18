"""Base for strategies still to be built. Importable so the registry can stay
complete; raises NotImplementedError until built.

As of Phase 5 no strategy stubs remain: uploaded_caption (P1), api_captions (P2),
ytdlp_subs (P3), local_whisper (P4) and the managed_* trio (P5) are all real
modules. `_Unbuilt` stays as the base for any future stubbed strategy so the
contract is consistent when one is added."""
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
