"""Strategy protocol. Every acquisition strategy conforms to the same interface
so the orchestrator just iterates an ordered, policy-selected list. Adding or
reordering strategies is configuration, not code surgery.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..policy import Policy
from ..schema import Result, VideoRef


@runtime_checkable
class Strategy(Protocol):
    name: str

    def applicable(self, ref: VideoRef, policy: Policy) -> bool:
        """Cheap check: could this strategy possibly handle this ref under this policy?"""
        ...

    async def fetch(self, ref: VideoRef, policy: Policy) -> Result:
        """Attempt acquisition. MUST return a Result (success/unavailable/failed),
        never raise for expected outcomes. Unexpected exceptions are caught by the
        orchestrator and converted to a `provider_error` failed Result."""
        ...
