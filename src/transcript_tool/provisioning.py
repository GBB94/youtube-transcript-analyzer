"""ASR model provisioning contract (implemented in Phase 4).

"Preload" is precise here and must not be conflated:
  - Pre-provision AND checksum the model artifact ahead of time (out of band).
  - local profile: lazily LOAD into memory from local-only storage the first time
    ASR is actually needed — a caption-first run must not load a multi-GB model.
  - server profile: WARM ASR workers at startup.
  - NEVER download a model during an active transcription request.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    name: str = "faster-whisper"
    size: str = "small"          # CPU default; large-v3 on GPU
    revision: str = ""           # pin exactly
    compute_type: str = "int8"   # pin exactly


def provision(spec: ModelSpec, store_dir: str) -> str:
    """Download + checksum the model into local-only storage. Out-of-band setup,
    never called on the request path. Returns the verified local path."""
    raise NotImplementedError("Phase 4: provision + checksum model artifact")


def load_lazy(spec: ModelSpec, store_dir: str):
    """Load from local-only storage on first ASR use (local profile). Must fail
    with missing_dependency (not a silent download) if the artifact isn't present."""
    raise NotImplementedError("Phase 4: lazy local load (no network)")
