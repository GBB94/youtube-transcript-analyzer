"""Reliability bakeoff harness (Phase 0 — Workstream B).

Runs a representative corpus through the REAL pipeline and reports, by acquisition
path: success/unavailable/failed counts + reasons, latency (p50/p95), and cost. This
is how `docs/SLO.md` thresholds get their numbers.

The corpus should span the real distribution — human-captioned, auto-captioned,
captions-disabled, language-mismatch, blocked/bot-gated, members-only,
age-restricted, geoblocked, live, no-speech, Shorts, and >=2 languages — using ONLY
content you have the right to test against. The public-URL paths must be run on a
real machine (this sandbox is IP-blocked); the harness itself runs anywhere and is
unit-tested against local caption fixtures.

Corpus format (JSONL): one object per line, e.g.
    {"target": "subtitles.vtt"}
    {"target": "https://youtu.be/VIDEO_ID", "expected": "human_caption"}
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .cache import Cache
from .orchestrator import get_transcript_sync
from .policy import Policy
from .schema import Outcome


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 2)


@dataclass
class PathStats:
    success: int = 0
    unavailable: int = 0
    failed: int = 0
    reasons: dict = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    cost_by_unit: dict = field(default_factory=dict)

    def summary(self) -> dict:
        total = self.success + self.unavailable + self.failed
        return {
            "total": total,
            "success": self.success,
            "unavailable": self.unavailable,
            "failed": self.failed,
            "success_rate": round(self.success / total, 3) if total else None,
            "reasons": self.reasons,
            "latency_ms_p50": _percentile(self.latencies_ms, 0.50),
            "latency_ms_p95": _percentile(self.latencies_ms, 0.95),
            "cost_by_unit": self.cost_by_unit,
        }


def _winning_path(result) -> str:
    """The strategy that produced a success, else the reason bucket for a non-success.
    A cache hit clears `attempts` (cache contract), so it can't be attributed to a
    strategy — label it `cache_hit` rather than guessing. Run a bakeoff fresh (no
    cache) to measure real acquisition."""
    if result.cache.served_from_cache:
        return "cache_hit"
    if result.outcome is Outcome.success:
        ok = [a for a in result.attempts if a.ok]
        return ok[-1].strategy if ok else "success"
    return f"unavailable:{result.reason.value}" if result.outcome is Outcome.unavailable \
        else f"failed:{result.reason.value}"


def run_bakeoff(targets: list[str], policy: Optional[Policy] = None,
                cache: Optional[Cache] = None, classify=None) -> dict:
    """Execute each target through the pipeline; aggregate by acquisition path.

    `classify(target) -> (VideoRef, strategies)` lets the caller reuse the CLI's
    target classification; if None, a local-only caption/audio classifier is used.
    Returns a JSON-serializable report.
    """
    if classify is None:
        from .cli import _classify_target as classify  # reuse the CLI's logic

    from .policy import EgressPolicy
    by_path: dict[str, PathStats] = {}
    rows = []
    for target in targets:
        ref, strategies = classify(target)
        pol = policy or Policy(
            enabled_strategies=strategies,
            egress=EgressPolicy(allow_network=ref.source == "url",
                                allow_public_url=ref.source == "url"),
        )
        t0 = time.monotonic()
        result = get_transcript_sync(ref, pol, cache)
        wall_ms = (time.monotonic() - t0) * 1000.0

        path = _winning_path(result)
        st = by_path.setdefault(path, PathStats())
        if result.outcome is Outcome.success:
            st.success += 1
        elif result.outcome is Outcome.unavailable:
            st.unavailable += 1
            st.reasons[result.reason.value] = st.reasons.get(result.reason.value, 0) + 1
        else:
            st.failed += 1
            st.reasons[result.reason.value] = st.reasons.get(result.reason.value, 0) + 1
        st.latencies_ms.append(round(wall_ms, 2))
        for a in result.attempts:
            if a.cost and a.cost.amount:
                st.cost_by_unit[a.cost.unit] = round(
                    st.cost_by_unit.get(a.cost.unit, 0.0) + a.cost.amount, 4)

        rows.append({"target": target, "outcome": result.outcome.value,
                     "reason": result.reason.value if result.reason else None,
                     "path": path, "wall_ms": round(wall_ms, 2),
                     "words": result.word_count})

    overall_success = sum(s.success for s in by_path.values())
    overall_total = sum(s.success + s.unavailable + s.failed for s in by_path.values())
    return {
        "n": len(targets),
        "overall_success_rate": round(overall_success / overall_total, 3) if overall_total else None,
        "by_path": {k: v.summary() for k, v in sorted(by_path.items())},
        "rows": rows,
    }
