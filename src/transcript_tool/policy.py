"""Acquisition policy + the policy_hash used as a cache key input.

policy_hash MUST include everything that could change the *result* for the same
input: enabled strategies, language preferences, quality configuration, and the
privacy/egress posture. If you add a knob that affects output, add it here or you
will serve stale results across config changes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Literal

# Strategy identifiers (names, not L1/L2/L3 numbering).
StrategyName = Literal[
    "uploaded_caption",   # Phase 1
    "api_captions",       # Phase 2
    "ytdlp_subs",         # Phase 3
    "local_whisper",      # Phase 4
    "managed_native",     # Phase 5
    "managed_asr",        # Phase 5
    "managed_url_to_asr", # Phase 5 (compound: URL -> transcript, no local media artifact)
]

PolicyMode = Literal["captions-only", "prefer-captions", "asr-only"]


@dataclass(frozen=True)
class QualityConfig:
    max_cps: float = 25.0              # characters/second; warn above
    max_repetition_ratio: float = 0.6  # warn above
    require_monotonic_timestamps: bool = True
    min_duration_seconds: float = 0.0
    # NOTE: no minimum word count — it rejects valid Shorts.


@dataclass(frozen=True)
class EgressPolicy:
    allow_network: bool = False        # Phase 1 is offline; uploaded_file only
    allow_public_url: bool = False     # gated capability (compliance, see DESIGN.md §4)
    allow_cookies: bool = False        # explicit opt-in only
    allowed_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Policy:
    mode: PolicyMode = "prefer-captions"
    languages: tuple[str, ...] = ("en",)          # BCP-47 preference list
    enabled_strategies: tuple[StrategyName, ...] = ("uploaded_caption",)
    quality: QualityConfig = field(default_factory=QualityConfig)
    egress: EgressPolicy = field(default_factory=EgressPolicy)

    def policy_hash(self) -> str:
        payload = {
            "mode": self.mode,
            "languages": list(self.languages),
            "enabled_strategies": list(self.enabled_strategies),
            "quality": asdict(self.quality),
            "egress": asdict(self.egress),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()[:16]
