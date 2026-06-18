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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Environment self-check. Phase 1 checks Python deps + optional tools so the
    later phases fail loudly, not silently."""
    ok = True

    def check(label: str, present: bool, hint: str = "") -> None:
        nonlocal ok
        status = "OK" if present else "MISSING"
        if not present:
            ok = False
        _log(f"  [{status}] {label}" + (f"  -> {hint}" if not present and hint else ""))

    _log("transcript doctor:")
    try:
        import pydantic  # noqa: F401
        check("pydantic", True)
    except Exception:
        check("pydantic", False, "pip install pydantic")

    check("yt-dlp (Phase 3)", shutil.which("yt-dlp") is not None, "pip install yt-dlp")
    check("JS runtime deno/node (Phase 3)",
          any(shutil.which(b) for b in ("deno", "node")),
          "install Deno or Node for full YouTube support")
    check("ffmpeg (yt-dlp, Phase 3)", shutil.which("ffmpeg") is not None,
          "install ffmpeg for best-quality audio")
    _log("  [note] PO-token provider plugin + faster-whisper model are Phase 3/4 setup.")
    _emit({"doctor_ok": ok})
    return 0 if ok else 1


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

    doctor = sub.add_parser("doctor", help="check the environment / dependencies")
    doctor.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
