"""Phase 0 — bakeoff harness. Exercised against local caption fixtures (the public-URL
paths need real hardware). Proves per-path aggregation, latency percentiles, and the
JSON report shape that feeds docs/SLO.md."""
from pathlib import Path

from transcript_tool.bakeoff import _percentile, run_bakeoff

FIX = Path(__file__).parent / "fixtures"


def test_percentile_basic():
    assert _percentile([], 0.5) is None
    assert _percentile([10.0], 0.95) == 10.0
    assert _percentile([0.0, 10.0], 0.5) == 5.0


def test_bakeoff_aggregates_by_path(tmp_path):
    targets = [str(FIX / "basic.srt"), str(FIX / "rolling_autocaption.vtt")]
    report = run_bakeoff(targets, cache=None)
    assert report["n"] == 2
    assert report["overall_success_rate"] == 1.0
    # Both succeeded via the offline caption path.
    assert "uploaded_caption" in report["by_path"]
    path = report["by_path"]["uploaded_caption"]
    assert path["success"] == 2 and path["total"] == 2
    assert path["latency_ms_p50"] is not None and path["latency_ms_p95"] is not None
    assert len(report["rows"]) == 2


def test_bakeoff_records_failures_with_reason(tmp_path):
    report = run_bakeoff([str(tmp_path / "missing.vtt")], cache=None)
    # A missing file is failed/invalid_input and shows up under its reason path.
    assert report["overall_success_rate"] == 0.0
    assert any("invalid_input" in p for p in report["by_path"])
