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


def _corpus_refs_from_channel(args: argparse.Namespace, cache) -> tuple[list, dict]:
    """Discovery reused as-is (RETRIEVAL_DESIGN.md §2): channel -> DiscoveredVideos ->
    (refs, per-id CorpusVideo enrichment). No new YouTube access path."""
    import os as _os
    from .corpus.records import CorpusVideo
    from .discover import GoogleApiClient, QuotaExceeded, QuotaTracker, channel_uploads

    api_key = _os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        _log("error: YOUTUBE_API_KEY is not set (discovery uses the YouTube Data API).")
        return [], {}
    quota = QuotaTracker()
    try:
        found = channel_uploads(GoogleApiClient(api_key), quota, args.channel, max_n=args.max,
                                include_shorts=not args.no_shorts,
                                include_live=not args.no_live, cache=cache)
    except QuotaExceeded as qe:
        _log(f"error: quota exceeded for the '{qe.bucket}' bucket")
        return [], {}
    refs, meta = [], {}
    for v in found.videos:
        refs.append(v.ref)
        meta[v.ref.id] = CorpusVideo(
            id=v.ref.id, url=v.ref.url, title=v.title,
            upload_date=(v.published_at or "")[:10] or None,
            duration_s=float(v.duration_seconds) if v.duration_seconds else None,
            channel_id=v.channel_id)
    return refs, meta


def cmd_corpus_add(args: argparse.Namespace) -> int:
    from .corpus.ingest import corpus_add
    from .corpus.store import CorpusStore
    from .web.parse import parse_targets

    cache = None if args.force else Cache(Path(args.cache_dir).expanduser())
    if args.channel:
        refs, meta = _corpus_refs_from_channel(args, cache)
        if not refs:
            return 2
    else:
        raw = _read_targets(args.file) if args.file else list(args.targets)
        parsed = parse_targets("\n".join(raw))
        for bad in parsed.invalid:
            _log(f"skip {bad.raw}: {bad.reason}")
        refs, meta = [t.ref() for t in parsed.valid], {}
    if not refs:
        _log("error: nothing to ingest — provide --channel, --file, or targets")
        return 2

    # Ingest pulls public URLs, so the same gate as `pull` applies (DESIGN.md §4).
    if not args.enable_public_url:
        _log("error: corpus ingest pulls from public URLs, which is gated; "
             "pass --enable-public-url (see DESIGN.md §4).")
        return 2

    egress = EgressPolicy(allow_network=True, allow_public_url=True)
    strategies = tuple(args.strategies) if args.strategies else _URL_STRATEGIES
    policy = Policy(mode=args.policy, languages=tuple(args.lang),
                    enabled_strategies=strategies, egress=egress)
    store = CorpusStore(Path(args.corpus_root).expanduser())

    if getattr(args, "diarize", False):
        from .corpus.diarize import diarize_ingest
        report = diarize_ingest(store, args.slug, refs, policy=policy, cache=cache,
                                meta=meta, force=args.force, log=_log)
    else:
        import asyncio
        report = asyncio.run(corpus_add(store, args.slug, refs, policy=policy, cache=cache,
                                        meta=meta, force=args.force, pull=None, log=_log))
    _log(f"corpus add: {len(report.pulled)} pulled, {len(report.superseded)} superseded, "
         f"{len(report.skipped_existing)} already present, {len(report.unchanged)} unchanged, "
         f"{len(report.failed)} failed")
    _emit(report.as_dict())
    return 0 if not report.failed else 1


def cmd_corpus_status(args: argparse.Namespace) -> int:
    from .corpus.status import corpus_status
    from .corpus.store import CorpusStore

    status = corpus_status(CorpusStore(Path(args.corpus_root).expanduser()), args.slug)
    _log(f"corpus {args.slug}: {status['videos']} videos "
         f"({status['diarized']} diarized), {status['stale']} stale vs current versions")
    _emit(status)
    return 0


def cmd_corpus_build(args: argparse.Namespace) -> int:
    from .corpus.store import CorpusStore
    from .retrieval.build import corpus_build

    store = CorpusStore(Path(args.corpus_root).expanduser())
    report = corpus_build(store, args.slug, rebuild=args.rebuild,
                          embedder_kind=args.embedder, use_context=not args.no_context,
                          log=_log)
    _log(f"corpus build: {report['built']} videos (re)indexed, "
         f"{report['unchanged']} already current, {report['removed']} removed")
    _emit(report)
    return 0


def _only_slug(corpus_root: Path) -> str | None:
    """When --source is omitted and exactly one corpus exists, use it."""
    if not corpus_root.exists():
        return None
    slugs = [p.parent.name for p in corpus_root.glob("*/manifest.json")]
    return slugs[0] if len(slugs) == 1 else None


def cmd_ask(args: argparse.Namespace) -> int:
    """Grounded answering (§10). Answer text -> stdout; logs -> stderr.
    Exit codes: 0 grounded, 1 insufficient evidence, 2 failed/usage."""
    from .corpus.store import CorpusStore
    from .retrieval.answer import ask_sync
    from .retrieval.retrieve import Filters, LocalCrossEncoder, RetrieveConfig

    corpus_root = Path(args.corpus_root).expanduser()
    slug = args.source or _only_slug(corpus_root)
    if not slug:
        _log("error: pass --source <slug> (multiple or zero corpora found)")
        return 2

    store = CorpusStore(corpus_root)
    filters = Filters(since=args.since, until=args.until, speaker=args.speaker)
    reranker = None if args.no_rerank else LocalCrossEncoder()
    _log(f"ask: {slug} (k={args.k}, rerank={not args.no_rerank})")
    answer = ask_sync(args.question, store=store, slug=slug, filters=filters,
                      reranker=reranker,
                      retrieve_config=RetrieveConfig(k=args.k))

    if args.json:
        _emit(answer.model_dump())
    elif answer.answer_outcome == "grounded":
        print(answer.answer, file=sys.stdout)
        print("", file=sys.stdout)
        for c in answer.citations:
            label = c.title or c.video_id
            print(f"- {label} ({c.provenance}) {c.url_with_timestamp}", file=sys.stdout)
    elif answer.answer_outcome == "insufficient_evidence":
        print("No supporting passages found in the corpus for this question.",
              file=sys.stdout)
    else:
        _log(f"ask failed: {answer.reason}")

    return {"grounded": 0, "insufficient_evidence": 1}.get(answer.answer_outcome, 2)


def cmd_eval(args: argparse.Namespace) -> int:
    """Golden-set retrieval metrics + optional --compare regression gate (§13).
    Exit codes: 0 ok, 3 regression past threshold, 2 usage."""
    from .corpus.store import CorpusStore
    from .retrieval.embed import get_embedder
    from .retrieval.evalharness import compare, format_compare_table, load_golden, run_eval
    from .retrieval.retrieve import LocalCrossEncoder

    corpus_root = Path(args.corpus_root).expanduser()
    golden_path = Path(args.golden) if args.golden else corpus_root / args.slug / "golden.json"
    if not golden_path.exists():
        _log(f"error: no golden set at {golden_path} (author one; see RETRIEVAL_DESIGN.md §13)")
        return 2

    store = CorpusStore(corpus_root)
    reranker = None if args.no_rerank else LocalCrossEncoder()
    report = run_eval(store, args.slug, load_golden(golden_path),
                      embedder=get_embedder(args.embedder), reranker=reranker,
                      k=args.k, log=_log)
    payload = report.as_dict()
    _log(f"eval: recall@{args.k}={payload['recall_at_k']} mrr={payload['mrr']} "
         f"over {payload['n_questions']} questions")

    if args.out:
        Path(args.out).expanduser().write_text(json.dumps(payload, indent=2))
        _log(f"eval: report written to {args.out}")

    if args.compare:
        baseline = json.loads(Path(args.compare).expanduser().read_text())
        result = compare(baseline, payload, threshold=args.threshold)
        _log(format_compare_table(result))
        _emit({"report": payload, "compare": result})
        return 0 if result["ok"] else 3

    _emit(payload)
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

    corpus = sub.add_parser("corpus", help="canonical corpus store + derived index (R0+)")
    csub = corpus.add_subparsers(dest="corpus_cmd", required=True)

    cadd = csub.add_parser("add", help="ingest only-new videos through the existing pipeline")
    cadd.add_argument("slug", help="source slug, e.g. moonshots-diamandis")
    cadd.add_argument("targets", nargs="*", help="video URLs/ids (alternative to --channel/--file)")
    cadd.add_argument("--channel", help="channel id (UC…) or @handle to traverse uploads")
    cadd.add_argument("--file", help="one target per line, or '-' for stdin")
    cadd.add_argument("--max", type=int, default=200, help="channel traversal cap")
    cadd.add_argument("--no-shorts", action="store_true", help="exclude Shorts (<=60s)")
    cadd.add_argument("--no-live", action="store_true", help="exclude live/upcoming livestreams")
    cadd.add_argument("--policy", choices=["captions-only", "prefer-captions", "asr-only"],
                      default="prefer-captions")
    cadd.add_argument("--lang", nargs="+", default=["en"])
    cadd.add_argument("--strategies", nargs="+", help="override the strategy order")
    cadd.add_argument("--enable-public-url", action="store_true",
                      help="acknowledge the policy decision to extract from public URLs (gated)")
    cadd.add_argument("--force", action="store_true",
                      help="re-pull even if present (content-hash guard still applies)")
    cadd.add_argument("--diarize", action="store_true",
                      help="per-episode opt-in: route through audio->ASR + speaker "
                           "segmentation so chunks carry speaker labels (default off, §18/R7)")
    cadd.add_argument("--corpus-root", default="corpus")
    cadd.set_defaults(func=cmd_corpus_add)

    cbuild = csub.add_parser("build", help="(re)build chunks -> context -> embeddings -> index, "
                                           "offline from the canonical layer")
    cbuild.add_argument("slug")
    cbuild.add_argument("--rebuild", action="store_true", help="force a full derived rebuild")
    cbuild.add_argument("--no-context", action="store_true",
                        help="skip contextual enrichment (no text leaves the machine)")
    cbuild.add_argument("--embedder", choices=["local", "api"], default="local")
    cbuild.add_argument("--corpus-root", default="corpus")
    cbuild.set_defaults(func=cmd_corpus_build)

    cstatus = csub.add_parser("status", help="counts, versions, staleness")
    cstatus.add_argument("slug")
    cstatus.add_argument("--corpus-root", default="corpus")
    cstatus.set_defaults(func=cmd_corpus_status)

    ask = sub.add_parser("ask", help="ask a question; grounded answer with deep-link citations")
    ask.add_argument("question")
    ask.add_argument("--source", help="corpus slug (defaults when exactly one exists)")
    ask.add_argument("--since", help="only episodes uploaded on/after YYYY-MM-DD")
    ask.add_argument("--until", help="only episodes uploaded on/before YYYY-MM-DD")
    ask.add_argument("--speaker", help="only chunks attributed to this speaker "
                                       "(diarized episodes only)")
    ask.add_argument("--k", type=int, default=8)
    ask.add_argument("--no-rerank", action="store_true",
                     help="skip the cross-encoder rerank stage")
    ask.add_argument("--json", action="store_true",
                     help="emit the structured answer object (outcome, citations, trace)")
    ask.add_argument("--corpus-root", default="corpus")
    ask.set_defaults(func=cmd_ask)

    ev = sub.add_parser("eval", help="golden-set retrieval metrics + regression gate (§13)")
    ev.add_argument("slug")
    ev.add_argument("--golden", help="golden-set JSON (default corpus/<slug>/golden.json)")
    ev.add_argument("--k", type=int, default=8)
    ev.add_argument("--embedder", choices=["local", "api"], default="local")
    ev.add_argument("--no-rerank", action="store_true")
    ev.add_argument("--compare", help="baseline report JSON to diff against")
    ev.add_argument("--threshold", type=float, default=0.05,
                    help="max allowed per-metric drop before the run fails")
    ev.add_argument("--out", help="write the report JSON here as the next baseline")
    ev.add_argument("--corpus-root", default="corpus")
    ev.set_defaults(func=cmd_eval)

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
