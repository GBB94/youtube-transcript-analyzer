# SLO thresholds (Phase 0 → asserted by Phase 8)

These are the **release gates** Phase 8's SLO-conformance suite asserts against.
Telemetry *measures* latency/cost; this file turns measurements into pass/fail lines.

> **STATUS: PROVISIONAL.** The numbers below are placeholders with sane starting
> values. They MUST be replaced with figures from a real reliability bakeoff
> (`transcript bakeoff`, see `bakeoff.py`) run on the reference hardware — this
> sandbox is IP-blocked and cannot exercise the public-URL paths. Until then,
> Phase 8 conformance tests treat unmet/未measured gates as **skipped, not passed**.

## Reference hardware
- **Local profile:** Apple Silicon (M-series), CPU-only, 16 GB RAM, macOS 14+.
  faster-whisper `small`, `int8`.
- **Server profile:** TBD at deploy (named instance type + GPU/CPU, RAM, disk).

## Gates

| Gate | Metric | Threshold (provisional) | Source |
|------|--------|-------------------------|--------|
| caption latency | p95 wall-clock, caption paths (uploaded/api/ytdlp) | < 8 s | bakeoff |
| ASR real-time factor | local_whisper compute_time / audio_duration on ref HW | < 1.0 | bakeoff |
| corpus success | success rate over the public-eligible corpus | ≥ 70 % | bakeoff |
| cost ceiling | max expected provider cost per processed hour | ≤ $1.50 | bakeoff |
| WER regression | jiwer WER vs. licensed clip baseline (change-detection) | ≤ baseline + 0.05 | asr_eval |

<!-- machine-readable: Phase 8 conformance reads this block. `null` => not yet measured (skip). -->
```json
{
  "reference_hardware": "apple-silicon-m-cpu-16gb",
  "provisional": true,
  "gates": {
    "caption_latency_p95_s": 8.0,
    "asr_realtime_factor_max": 1.0,
    "corpus_success_rate_min": 0.70,
    "cost_per_hour_usd_max": 1.50,
    "wer_regression_delta_max": 0.05
  },
  "measured": {
    "caption_latency_p95_s": null,
    "asr_realtime_factor": null,
    "corpus_success_rate": null,
    "cost_per_hour_usd": null,
    "wer_regression_delta": null
  }
}
```

## How to populate
1. Assemble a licensed/representative corpus (see `bakeoff.py` docstring for the
   distribution to span).
2. `transcript bakeoff --corpus corpus.jsonl --out bakeoff_report.json` on the
   reference hardware (real network).
3. Copy the observed numbers into `measured` above, tighten `gates` to the agreed
   targets, set `"provisional": false`.
4. Phase 8 conformance flips from skip → assert.
