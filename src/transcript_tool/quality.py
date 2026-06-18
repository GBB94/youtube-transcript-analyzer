"""Source-aware quality gates.

Hard gates (reject -> escalate to next strategy): timestamp validity and
duration bounds. Soft gates (warn, do not reject in v1): repetition ratio and
characters-per-second — fast speech and lyrics are legitimate. No minimum word
count (it rejects valid Shorts).
"""
from __future__ import annotations

from .normalize import Cue
from .policy import QualityConfig
from .schema import GateResult


def _repetition_ratio(text: str) -> float:
    words = text.split()
    if not words:
        return 0.0
    return 1.0 - (len(set(words)) / len(words))


def _cps(text: str, duration: float) -> float:
    if duration <= 0:
        return 0.0
    return len(text) / duration


def evaluate(cues: list[Cue], text: str, cfg: QualityConfig) -> list[GateResult]:
    gates: list[GateResult] = []

    # --- hard: timestamp monotonicity ---
    monotonic = all(
        cues[i].start <= cues[i].end and cues[i].end <= cues[i + 1].start + 1e-6
        for i in range(len(cues) - 1)
    ) and (not cues or cues[-1].start <= cues[-1].end)
    if cfg.require_monotonic_timestamps and cues and not monotonic:
        gates.append(GateResult(name="timestamps", result="reject",
                                detail="non-monotonic or inverted cue timestamps"))
    else:
        gates.append(GateResult(name="timestamps", result="pass"))

    # --- hard: duration bounds ---
    duration = cues[-1].end if cues else 0.0
    if duration < cfg.min_duration_seconds:
        gates.append(GateResult(name="duration", result="reject", value=duration,
                                detail="below minimum duration"))
    else:
        gates.append(GateResult(name="duration", result="pass", value=duration))

    # --- soft: characters-per-second (warn) ---
    cps = _cps(text, duration)
    gates.append(GateResult(name="cps", value=round(cps, 2),
                            result="warn" if cps > cfg.max_cps else "pass"))

    # --- soft: repetition ratio (warn) ---
    rep = _repetition_ratio(text)
    gates.append(GateResult(name="repetition", value=round(rep, 3),
                            result="warn" if rep > cfg.max_repetition_ratio else "pass"))

    return gates


def rejected(gates: list[GateResult]) -> bool:
    return any(g.result == "reject" for g in gates)


def rejection_reasons(gates: list[GateResult]) -> list[str]:
    return [g.name for g in gates if g.result == "reject"]
