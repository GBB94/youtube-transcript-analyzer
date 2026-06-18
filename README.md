# transcript-tool

Staged, policy-driven video transcript acquisition. Caption strategies first,
audio ASR as the floor. This repository is the **Phase 1 starter**: the compliance-
safe, offline `uploaded_file` slice, with later strategies stubbed and labelled.

- **Start here:** `CLAUDE.md` (working agreement) and `docs/PHASE_1_BUILD.md` (current task).
- **Full spec:** `docs/DESIGN.md`.

## Quickstart
```
pip install -e ".[dev]"        # or: pip install pydantic pytest
pytest -q
transcript pull tests/fixtures/rolling_autocaption.vtt
transcript doctor
```

## Status
Phase 1 built and green (schema/outcome model, policy, preflight, normalization +
dedup, source-aware quality gates, two-layer cache with full lifecycle contract,
orchestrator + singleflight, CLI, `uploaded_caption`). Phases 2–8 are stubbed with
contract docstrings. See `docs/DESIGN.md §16` for the roadmap.
