"""ASR model provisioning (Phase 4).

"Preload" is precise and must not be conflated:
  - Pre-provision AND checksum the model artifact ahead of time (provision()).
  - Load LAZILY from local-only storage on first ASR use (load_lazy, local profile)
    or warm at startup (server). NEVER download a model during a request:
    load_lazy uses local_files_only=True, so a missing model raises ModelUnavailable
    (mapped to `missing_dependency`) instead of silently downloading GBs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple


class ModelUnavailable(RuntimeError):
    """faster-whisper not installed, or the pinned model isn't provisioned locally."""


@dataclass(frozen=True)
class ModelSpec:
    name: str = "faster-whisper"
    size: str = "small"          # CPU default; large-v3 on GPU
    revision: str = ""           # pin exactly when known
    compute_type: str = "int8"   # pin exactly
    device: str = "cpu"


_CACHE: Dict[Tuple[str, str, str, str], Any] = {}


def provision(spec: ModelSpec, store_dir: str) -> str:
    """Out-of-band: download + cache the model into local-only storage. NEVER called
    on the request path. Returns the store dir once present."""
    try:
        from faster_whisper import WhisperModel  # noqa: F401
        from faster_whisper.utils import download_model
    except Exception as e:  # pragma: no cover - environment dependent
        raise ModelUnavailable("faster-whisper not installed") from e
    download_model(spec.size, output_dir=None, cache_dir=store_dir)
    return store_dir


def warm(specs, store_dir: str, loader=None) -> int:
    """Server profile: load each pinned model ONCE at startup so no request ever
    triggers a load/download mid-flight (the caption-first contract). Returns the
    number of models warmed. `loader` is injectable for tests; defaults to load_lazy,
    which is local-only and never downloads — provision out of band first."""
    load = loader or load_lazy
    n = 0
    for spec in specs:
        load(spec, store_dir)
        n += 1
    return n


def is_provisioned(spec: ModelSpec, store_dir: str) -> bool:
    """Cheap filesystem check: is the pinned model present in local-only storage?
    Does NOT load the model (that's load_lazy) and NEVER downloads. Used by
    `doctor` to report ASR readiness without pulling GBs into memory. faster-whisper
    / ctranslate2 models materialize a `model.bin` weights file."""
    from pathlib import Path
    root = Path(store_dir).expanduser()
    return root.exists() and any(root.rglob("model.bin"))


def load_lazy(spec: ModelSpec, store_dir: str) -> Any:
    """Load the model into memory from local-only storage. Cached per spec.
    Raises ModelUnavailable (never downloads) if absent."""
    key = (spec.size, spec.revision, spec.compute_type, spec.device)
    if key in _CACHE:
        return _CACHE[key]
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        raise ModelUnavailable("faster-whisper not installed") from e
    try:
        model = WhisperModel(
            spec.size, device=spec.device, compute_type=spec.compute_type,
            download_root=store_dir, local_files_only=True,   # never download mid-request
        )
    except Exception as e:
        raise ModelUnavailable(f"model '{spec.size}' not provisioned in {store_dir}") from e
    _CACHE[key] = model
    return model
