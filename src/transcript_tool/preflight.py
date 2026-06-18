"""Stage 1 preflight. Produces HINTS, not authoritative truth: a local probe
reporting 'blocked' may be wrong when another strategy would succeed. Only
authoritative terminal conditions short-circuit the request:
  - malformed/invalid input
  - confirmed removal backed by authoritative evidence
Inconclusive metadata must remain a hint and allow strategy attempts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .policy import Policy
from .schema import Reason, Result, VideoRef


def preflight(ref: VideoRef, policy: Policy) -> Optional[Result]:
    # Authoritative: an uploaded_file with no usable path is invalid_input.
    if ref.source == "uploaded_file":
        if not ref.path or not Path(ref.path).expanduser().exists():
            return Result.make_failed(ref, Reason.invalid_input)
        return None

    # URL source: in Phase 1 the network/public-URL capability is gated off.
    if ref.source == "url":
        if not policy.egress.allow_public_url:
            # Not a content problem — a capability/config gate.
            return Result.make_failed(ref, Reason.missing_dependency)
        # When enabled (later phases): resolve id/handle, collect access *evidence*,
        # and short-circuit ONLY on authoritative removal. Otherwise return None.
        return None

    return None
