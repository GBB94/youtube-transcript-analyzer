"""Operator driver: bank a channel's long-form transcripts into the corpus,
without a YouTube Data API key.

`transcript corpus add --channel` uses the authorized Data API and remains the
preferred discovery path when YOUTUBE_API_KEY is configured. This script is the
keyless alternative: enumeration and per-video metadata go through yt-dlp — the
same tool the gated `ytdlp_subs` pull strategy already uses — so it introduces
no new kind of access, and it refuses to run without the same explicit
`--enable-public-url` opt-in the CLI requires (DESIGN.md §4). A bonus over the
Data API: yt-dlp metadata carries published CHAPTERS, which the chunker treats
as hard boundaries (RETRIEVAL_DESIGN.md §6).

Ingest itself is the library path: corpus_add -> the staged pull pipeline,
captions-only by default, only-new manifest incrementality, content-hash guard.
Sequential with jitter — one IP, be gentle. Non-selected videos (clips, shorts,
out-of-window) are remembered in a per-slug enumeration cache so recurring runs
skip them without re-fetching metadata.

Usage (from the repo root, with the project venv):
  .venv/bin/python scripts/pull_channel.py moonshots-diamandis \
      --channel-id UCvxm0qTrGN_1LMYgUaftWyQ --enable-public-url [--build]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from transcript_tool.cache import Cache                      # noqa: E402
from transcript_tool.corpus.ingest import corpus_add         # noqa: E402
from transcript_tool.corpus.records import Chapter, CorpusVideo  # noqa: E402
from transcript_tool.corpus.store import CorpusStore         # noqa: E402
from transcript_tool.orchestrator import get_transcript      # noqa: E402
from transcript_tool.policy import EgressPolicy, Policy      # noqa: E402
from transcript_tool.schema import VideoRef                  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_ytdlp() -> str:
    candidate = REPO / ".venv" / "bin" / "yt-dlp"
    if candidate.exists():
        return str(candidate)
    return "yt-dlp"


def ytdlp_json(ytdlp: str, url: str, *flags: str) -> dict:
    proc = subprocess.run([ytdlp, "-J", "--no-download", *flags, "--", url],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1] if proc.stderr else "yt-dlp failed"
        raise RuntimeError(tail)
    return json.loads(proc.stdout)


class EnumerationCache:
    """Non-selected video ids (clips/shorts/out-of-window) so recurring runs
    never re-fetch their metadata. Lives beside the manifest, git-ignored with
    the rest of the corpus. Live entries are NOT cached — a stream becomes a
    normal video later."""

    def __init__(self, store: CorpusStore, slug: str):
        self.path = store.slug_dir(slug) / ".enumeration_cache.json"
        self.skipped: dict[str, str] = {}
        if self.path.exists():
            self.skipped = json.loads(self.path.read_text(encoding="utf-8"))

    def skip(self, video_id: str, reason: str) -> None:
        self.skipped[video_id] = reason

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.skipped, indent=0, sort_keys=True),
                             encoding="utf-8")


def collect(args, ytdlp: str, known: set[str], cache: EnumerationCache):
    cutoff = (date.today() - timedelta(days=args.cutoff_days)).strftime("%Y%m%d")
    log(f"enumerating channel {args.channel_id} (cutoff {cutoff})…")
    listing = ytdlp_json(ytdlp,
                         f"https://www.youtube.com/channel/{args.channel_id}/videos",
                         "--flat-playlist")
    entries = [e for e in (listing.get("entries") or []) if e and e.get("id")]
    log(f"{len(entries)} entries on the videos tab")

    refs, meta = [], {}
    consecutive_old = 0
    fetches = 0
    for e in entries:
        vid = e["id"]
        if vid in known or vid in cache.skipped:
            continue
        if fetches >= args.max_fetches:
            log(f"metadata cap ({args.max_fetches}) reached")
            break
        url = f"https://www.youtube.com/watch?v={vid}"
        fetches += 1
        try:
            d = ytdlp_json(ytdlp, url)
        except Exception as err:
            log(f"metadata error for {vid}: {err}")
            continue
        finally:
            time.sleep(1.5 + random.random() * 1.5)

        upload_date = d.get("upload_date") or ""
        duration = d.get("duration") or 0
        if upload_date and upload_date < cutoff:
            cache.skip(vid, f"old:{upload_date}")
            consecutive_old += 1
            if consecutive_old >= args.consecutive_old_stop:
                log("past the cutoff window; enumeration complete")
                break
            continue
        consecutive_old = 0
        if d.get("live_status") in ("is_live", "is_upcoming"):
            continue                     # deliberately uncached; re-check next run
        if duration < args.min_longform_s:
            cache.skip(vid, f"shortform:{duration}s")
            continue

        meta[vid] = CorpusVideo(
            id=vid, url=url, title=d.get("title"),
            upload_date=f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
                        if len(upload_date) == 8 else None,
            duration_s=float(duration), channel_id=d.get("channel_id"),
            channel_title=d.get("channel"),
            chapters=[Chapter(start=c["start_time"], title=c.get("title") or "")
                      for c in (d.get("chapters") or [])])
        refs.append(VideoRef(platform="youtube", id=vid, url=url, source="url"))
        log(f"selected {vid} ({upload_date}, {duration // 60}min, "
            f"{len(meta[vid].chapters)} chapters)")
    cache.save()
    return refs, meta


async def paced_pull(ref, policy, cache):
    result = await get_transcript(ref, policy, cache)
    await asyncio.sleep(2.5 + random.random() * 2.5)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("slug")
    p.add_argument("--channel-id", required=True, help="UC… channel id")
    p.add_argument("--cutoff-days", type=int, default=730)
    p.add_argument("--min-longform-s", type=int, default=1200,
                   help="videos shorter than this are treated as clips and skipped")
    p.add_argument("--max-fetches", type=int, default=350)
    p.add_argument("--consecutive-old-stop", type=int, default=8)
    p.add_argument("--corpus-root", default=str(REPO / "corpus"))
    p.add_argument("--enable-public-url", action="store_true",
                   help="acknowledge the gated public-URL policy decision (DESIGN.md §4)")
    p.add_argument("--build", action="store_true",
                   help="run the offline corpus build after ingest (local embedder; "
                        "context only if a contextizer is configured via the CLI)")
    args = p.parse_args()

    if not args.enable_public_url:
        log("refusing: enumeration and pull use public URLs, which is gated; "
            "pass --enable-public-url (see DESIGN.md §4).")
        return 2

    ytdlp = find_ytdlp()
    store = CorpusStore(Path(args.corpus_root).expanduser())
    known = store.video_ids(args.slug)
    log(f"manifest already holds {len(known)} videos")
    enum_cache = EnumerationCache(store, args.slug)

    refs, meta = collect(args, ytdlp, known, enum_cache)
    log(f"collected {len(refs)} new episodes to pull")

    policy = Policy(mode="captions-only", languages=("en",),
                    enabled_strategies=("api_captions", "ytdlp_subs"),
                    egress=EgressPolicy(allow_network=True, allow_public_url=True))
    pull_cache = Cache(Path("~/.cache/transcript-tool").expanduser())
    report = asyncio.run(corpus_add(store, args.slug, refs, policy=policy,
                                    cache=pull_cache, meta=meta, pull=paced_pull,
                                    log=log))
    payload = report.as_dict()
    log(f"ingest: {len(payload['pulled'])} pulled, {len(payload['failed'])} failed, "
        f"{len(payload['skipped_existing'])} already present")
    print(json.dumps(payload, ensure_ascii=False))

    if args.build and (payload["pulled"] or payload["superseded"]):
        from transcript_tool.retrieval.build import corpus_build
        build = corpus_build(store, args.slug, use_context=False, log=log)
        log(f"build: {build['built']} videos (re)indexed, {build['chunks']} chunks touched")

    return 0 if not payload["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
