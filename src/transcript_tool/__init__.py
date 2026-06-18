"""transcript_tool — staged, policy-driven video transcript acquisition.

Public API (async canonical + sync wrapper):
    from transcript_tool import get_transcript, get_transcript_sync, Policy, VideoRef
"""
from .orchestrator import get_transcript, get_transcript_sync
from .policy import Policy, EgressPolicy, QualityConfig
from .schema import Result, VideoRef, Outcome, Reason

__all__ = [
    "get_transcript", "get_transcript_sync",
    "Policy", "EgressPolicy", "QualityConfig",
    "Result", "VideoRef", "Outcome", "Reason",
]
