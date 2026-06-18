"""CLI. Machine-readable output goes to STDOUT; all progress/logging to STDERR.

Phase 1 implements `pull <caption-file>`. `find` and `doctor` are functional
skeletons; `find --format ids` exists so it can pipe into `pull --file -` once
discovery lands.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .cache import Cache
from .orchestrator import get_transcript_sync
from .policy import EgressPolicy, Policy
from .schema import VideoRef


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False), file=sys.stdout)


CAPTION_SUFFIXES = {".vtt", ".srt"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".mp4", ".mkv", ".webm", ".mov"}


def _classify_target(target: str):
    """Return (VideoRef, default_strategies) for a file path or URL."""
    if target.startswith("http://") or target.startswith("https://"):
        return (VideoRef(platform="youtube", source="url", url=target),
                ("api_captions", "ytdlp_subs", "local_whisper"))
    p = Path(target).expanduser()
    suffix = p.suffix.lower()
    ref = VideoRef(platform="local", source="uploaded_file", path=str(p))
    if suffix in AUDIO_SUFFIXES:
        return ref, ("local_whisper",)
    return ref, ("uploaded_caption",)


def cmd_pull(args: argparse.Namespace) -> int:
    ref, strategies = _classify_target(args.target)
    if args.strategies:
        strategies = tuple(args.strategies)
    egress = EgressPolicy(
        allow_network=ref.source == "url",
        allow_public_url=args.enable_public_url,
    )
    policy = Policy(
        mode=args.policy,
        languages=tuple(args.lang),
        enabled_strategies=strategies,
        egress=egress,
    )
    cache = None if args.force else Cache(Path(args.cache_dir).expanduser())

    if ref.source == "url" and not args.enable_public_url:
        _log("error: public-URL extraction is a gated capability. "
             "Pass --enable-public-url to acknowledge the policy decision (see DESIGN.md §4).")
        return 2
    if ref.source == "uploaded_file" and not Path(ref.path).exists():
        _log(f"error: no such file: {ref.path}")
        return 2

    label = ref.url or Path(ref.path).name
    _log(f"pull: {label} (policy={policy.mode}, strategies={list(strategies)})")
    result = get_transcript_sync(ref, policy, cache)
    _log(f"outcome={result.outcome.value}"
         + (f" reason={result.reason.value}" if result.reason else "")
         + (" [cache hit]" if result.cache.served_from_cache else ""))
    _emit(result.model_dump(mode="json"))
    return 0 if result.outcome.value == "success" else 1


def cmd_find(args: argparse.Namespace) -> int:
    _log("find: discovery is a Phase 6 capability (YouTube Data API, dual-bucket quota).")
    _log("      `--format ids` will emit one id per line for `pull --file -`.")
    return 3


def _have_module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _strategy_checks(name: str) -> list[dict]:
    """Per-strategy runtime requirements. Each check is
    {label, ok, required, hint}; a strategy is ready iff every *required* check
    passes. `recommended` checks (required=False) only degrade capability."""
    if name == "uploaded_caption":
        # Offline caption parsing — only the core dependency, which is shared.
        return [{"label": "pydantic", "ok": _have_module("pydantic"),
                 "required": True, "hint": "pip install pydantic"}]
    if name == "api_captions":
        return [{"label": "youtube-transcript-api", "ok": _have_module("youtube_transcript_api"),
                 "required": True, "hint": "pip install '.[captions]'"}]
    if name == "ytdlp_subs":
        return [
            {"label": "yt-dlp binary", "ok": shutil.which("yt-dlp") is not None,
             "required": True, "hint": "pip install '.[media]'"},
            {"label": "JS runtime (deno/node)", "ok": any(shutil.which(b) for b in ("deno", "node")),
             "required": True, "hint": "install Deno or Node for YouTube support"},
            {"label": "ffmpeg", "ok": shutil.which("ffmpeg") is not None,
             "required": False, "hint": "brew install ffmpeg (best-quality audio)"},
        ]
    if name == "local_whisper":
        from .provisioning import ModelSpec, is_provisioned
        from .strategies.local_whisper import _store_dir
        spec = ModelSpec()
        return [
            {"label": "faster-whisper", "ok": _have_module("faster_whisper"),
             "required": True, "hint": "pip install '.[asr]'"},
            {"label": f"model '{spec.size}' provisioned ({_store_dir()})",
             "ok": _have_module("faster_whisper") and is_provisioned(spec, _store_dir()),
             "required": True, "hint": "pre-provision the model out of band (never downloaded mid-request)"},
            {"label": "ffmpeg", "ok": shutil.which("ffmpeg") is not None,
             "required": False, "hint": "brew install ffmpeg (URL->audio acquisition)"},
        ]
    return [{"label": f"unknown strategy '{name}'", "ok": False, "required": True, "hint": ""}]


# The strategies that are actually implemented (Phases 1-4).
BUILT_STRATEGIES = ("uploaded_caption", "api_captions", "ytdlp_subs", "local_whisper")


def cmd_doctor(args: argparse.Namespace) -> int:
    """Environment self-check. Validates the runtime dependencies of each enabled
    strategy and reports profile-aware readiness: `doctor_ok` is true only when
    every requested strategy can actually run on this machine."""
    requested = tuple(args.strategies) if args.strategies else BUILT_STRATEGIES

    _log("transcript doctor:")
    report: dict[str, dict] = {}
    all_ready = True
    for name in requested:
        checks = _strategy_checks(name)
        ready = all(c["ok"] for c in checks if c["required"])
        all_ready = all_ready and ready
        missing = [c["label"] for c in checks if c["required"] and not c["ok"]]
        report[name] = {"ready": ready, "missing": missing}

        _log(f"  {name}: {'READY' if ready else 'NOT READY'}")
        for c in checks:
            status = "OK" if c["ok"] else ("MISSING" if c["required"] else "optional")
            line = f"    [{status}] {c['label']}"
            if not c["ok"] and c["hint"]:
                line += f"  -> {c['hint']}"
            _log(line)

    _emit({"doctor_ok": all_ready, "strategies": report})
    return 0 if all_ready else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="transcript")
    p.add_argument("--cache-dir", default="~/.cache/transcript-tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    pull = sub.add_parser("pull", help="produce a transcript from a caption/audio file or URL")
    pull.add_argument("target", help="path to a .vtt/.srt/audio file, or an http(s) URL")
    pull.add_argument("--policy", choices=["captions-only", "prefer-captions", "asr-only"],
                      default="prefer-captions")
    pull.add_argument("--lang", nargs="+", default=["en"])
    pull.add_argument("--strategies", nargs="+", help="override the strategy order")
    pull.add_argument("--enable-public-url", action="store_true",
                      help="acknowledge the policy decision to extract from public URLs (gated)")
    pull.add_argument("--force", action="store_true", help="bypass cache")
    pull.set_defaults(func=cmd_pull)

    find = sub.add_parser("find", help="discover videos (Phase 6)")
    find.add_argument("--channel")
    find.add_argument("--query")
    find.add_argument("--max", type=int, default=25)
    find.add_argument("--format", choices=["ids", "jsonl"], default="ids")
    find.set_defaults(func=cmd_find)

    doctor = sub.add_parser("doctor", help="check per-strategy runtime readiness")
    doctor.add_argument("--strategies", nargs="+",
                        help="scope the check to these strategies (default: all built strategies)")
    doctor.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
