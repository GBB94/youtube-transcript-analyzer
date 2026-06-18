"""CLI. Machine-readable output goes to STDOUT; all progress/logging to STDERR.

Phase 1 implements `pull <caption-file>`. `find` and `doctor` are functional
skeletons; `find --format ids` exists so it can pipe into `pull --file -` once
discovery lands.
"""
from __future__ import annotations

import argparse
import json
import re
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


_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_STRATEGIES = ("api_captions", "ytdlp_subs", "local_whisper")


def _classify_target(target: str):
    """Return (VideoRef, default_strategies) for a file path, URL, or bare YouTube id
    (what `find --format ids` emits, so the pipe round-trips)."""
    if target.startswith("http://") or target.startswith("https://"):
        return (VideoRef(platform="youtube", source="url", url=target), _URL_STRATEGIES)
    if _YT_ID.match(target) and not Path(target).exists():
        url = f"https://www.youtube.com/watch?v={target}"
        return (VideoRef(platform="youtube", id=target, source="url", url=url), _URL_STRATEGIES)
    p = Path(target).expanduser()
    suffix = p.suffix.lower()
    ref = VideoRef(platform="local", source="uploaded_file", path=str(p))
    if suffix in AUDIO_SUFFIXES:
        return ref, ("local_whisper",)
    return ref, ("uploaded_caption",)


def _pull_one(target: str, args: argparse.Namespace, cache) -> int:
    """Process a single target; emit its Result JSON to stdout. Returns the per-item
    exit code (0 success, 1 non-success, 2 usage/gating)."""
    ref, strategies = _classify_target(target)
    if args.strategies:
        strategies = tuple(args.strategies)
    egress = EgressPolicy(allow_network=ref.source == "url",
                          allow_public_url=args.enable_public_url)
    policy = Policy(mode=args.policy, languages=tuple(args.lang),
                    enabled_strategies=strategies, egress=egress)

    if ref.source == "url" and not args.enable_public_url:
        _log(f"skip {target}: public-URL extraction is gated; pass --enable-public-url "
             "(see DESIGN.md §4).")
        return 2
    if ref.source == "uploaded_file" and not Path(ref.path).exists():
        _log(f"skip {target}: no such file")
        return 2

    label = ref.url or Path(ref.path).name
    _log(f"pull: {label} (policy={policy.mode}, strategies={list(strategies)})")
    result = get_transcript_sync(ref, policy, cache)
    _log(f"outcome={result.outcome.value}"
         + (f" reason={result.reason.value}" if result.reason else "")
         + (" [cache hit]" if result.cache.served_from_cache else ""))
    _emit(result.model_dump(mode="json"))
    return 0 if result.outcome.value == "success" else 1


def _read_targets(file_arg: str) -> list[str]:
    """One target per line from a file, or stdin when '-'. Blank lines / '#' comments
    are ignored, so `find --format ids | pull --file -` round-trips cleanly."""
    text = sys.stdin.read() if file_arg == "-" else Path(file_arg).expanduser().read_text()
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def cmd_pull(args: argparse.Namespace) -> int:
    cache = None if args.force else Cache(Path(args.cache_dir).expanduser())

    if args.file:
        targets = _read_targets(args.file)
        if not targets:
            _log("error: --file contained no targets")
            return 2
        worst = 0
        for t in targets:
            worst = max(worst, _pull_one(t, args, cache))
        return 1 if worst == 1 else (0 if worst == 0 else 2)

    if not args.target:
        _log("error: provide a target, or --file <path|-> for batch input")
        return 2
    return _pull_one(args.target, args, cache)


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the local batch transcript web UI (UI-1)."""
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        _log("error: the web UI needs the 'web' extra -> pip install '.[web]'")
        return 2
    _log(f"serving the batch UI on http://{args.host}:{args.port}  (Ctrl-C to stop)")
    import uvicorn
    uvicorn.run("transcript_tool.web.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_bakeoff(args: argparse.Namespace) -> int:
    """Phase 0 reliability bakeoff: run a corpus through the real pipeline and report
    per-path success/reason/latency/cost. Run on real hardware for the public-URL
    paths (this sandbox is IP-blocked)."""
    from .bakeoff import run_bakeoff
    targets: list[str] = []
    for line in _read_targets(args.corpus):
        try:
            targets.append(json.loads(line)["target"] if line.startswith("{") else line)
        except (json.JSONDecodeError, KeyError):
            _log(f"skip malformed corpus line: {line}")
    if not targets:
        _log("error: corpus had no usable targets")
        return 2
    # Bakeoff measures real acquisition, so it runs FRESH by default; opt into the
    # cache only if you explicitly want to include cache-hit timings.
    cache = Cache(Path(args.cache_dir).expanduser()) if args.use_cache else None
    _log(f"bakeoff: {len(targets)} targets")
    report = run_bakeoff(targets, cache=cache)
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).expanduser().write_text(text)
        _log(f"bakeoff: report written to {args.out} "
             f"(overall success rate {report['overall_success_rate']})")
    else:
        print(text, file=sys.stdout)
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    """Discovery via the authorized YouTube Data API. Emits VideoRef ids/JSONL to
    stdout (pipeable into `pull --file -`); budget estimate + errors to stderr."""
    import os
    from .discover import (
        DiscoveryResult, GoogleApiClient, QuotaExceeded, QuotaTracker,
        channel_uploads, search_query,
    )

    if not args.channel and not args.query:
        _log("error: provide --channel <id|@handle> or --query \"<text>\"")
        return 2

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        _log("error: YOUTUBE_API_KEY is not set (discovery uses the YouTube Data API).")
        return 2

    client = GoogleApiClient(api_key)
    quota = QuotaTracker()
    cache = Cache(Path(args.cache_dir).expanduser())

    try:
        if args.channel:
            result: DiscoveryResult = channel_uploads(
                client, quota, args.channel, max_n=args.max,
                include_shorts=not args.no_shorts, include_live=not args.no_live, cache=cache)
        else:
            result = search_query(client, quota, args.query, max_n=args.max, order=args.order,
                                  region_code=args.region, relevance_language=args.relevance_language)
    except QuotaExceeded as qe:
        _log(f"error: quota exceeded for the '{qe.bucket}' bucket "
             f"(estimate: {quota.remaining()}). Prefer channel/playlist traversal over search.")
        return 4

    for v in result.videos:
        if args.format == "ids":
            print(v.ref.id, file=sys.stdout)
        else:
            _emit(v.as_dict())

    if result.stability:
        _log(f"find: search params (results are not stable) = {result.stability}")
    _log(f"find: {len(result.videos)} videos | estimated quota remaining = {quota.remaining()}")
    return 0


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
    pull.add_argument("target", nargs="?", help="path to a .vtt/.srt/audio file, or an http(s) URL")
    pull.add_argument("--file", help="batch: read one target per line from a file, or '-' for stdin "
                                     "(pipe from `find --format ids`)")
    pull.add_argument("--policy", choices=["captions-only", "prefer-captions", "asr-only"],
                      default="prefer-captions")
    pull.add_argument("--lang", nargs="+", default=["en"])
    pull.add_argument("--strategies", nargs="+", help="override the strategy order")
    pull.add_argument("--enable-public-url", action="store_true",
                      help="acknowledge the policy decision to extract from public URLs (gated)")
    pull.add_argument("--force", action="store_true", help="bypass cache")
    pull.set_defaults(func=cmd_pull)

    find = sub.add_parser("find", help="discover videos via the YouTube Data API (Phase 6)")
    find.add_argument("--channel", help="channel id (UC…) or @handle to traverse uploads")
    find.add_argument("--query", help="search query (uses the scarce search-quota bucket)")
    find.add_argument("--max", type=int, default=25)
    find.add_argument("--format", choices=["ids", "jsonl"], default="ids")
    find.add_argument("--no-shorts", action="store_true", help="exclude Shorts (<=60s) from a channel")
    find.add_argument("--no-live", action="store_true", help="exclude live/upcoming/past livestreams")
    find.add_argument("--order", default="relevance", help="search order (relevance/date/viewCount/…)")
    find.add_argument("--region", help="search regionCode (persisted for result stability)")
    find.add_argument("--relevance-language", help="search relevanceLanguage (persisted)")
    find.set_defaults(func=cmd_find)

    doctor = sub.add_parser("doctor", help="check per-strategy runtime readiness")
    doctor.add_argument("--strategies", nargs="+",
                        help="scope the check to these strategies (default: all built strategies)")
    doctor.set_defaults(func=cmd_doctor)

    bake = sub.add_parser("bakeoff", help="run a corpus through the pipeline and report metrics (Phase 0)")
    bake.add_argument("--corpus", required=True, help="JSONL/line corpus of targets, or '-' for stdin")
    bake.add_argument("--out", help="write the JSON report here (default: stdout)")
    bake.add_argument("--use-cache", action="store_true",
                      help="include cache hits (default: run fresh to measure acquisition)")
    bake.set_defaults(func=cmd_bakeoff)

    serve = sub.add_parser("serve", help="run the local batch transcript web UI (UI-1)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="auto-reload (dev)")
    serve.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
