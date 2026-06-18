"""discover (Phase 6) — the "find" half.

Turn a channel / playlist / query into a list of VideoRefs for the existing `pull`
pipeline, so operators can batch instead of pasting URLs. Uses the **authorized**
YouTube Data API v3 (lower-risk than scraping). Discovery output still feeds the
**gated** public-URL pull path — discovery does not bypass the EgressPolicy gate.

Dual-bucket quota (the corrected, verified model — do not regress):
- `search.list` lives in its OWN "Search Queries" bucket: default 100 calls/day at
  1 unit/call. It is the scarce resource.
- ALL other reads (channels.list, playlistItems.list, videos.list) draw on the
  separate 10,000-unit/day pool at 1 unit each — abundant.
- The API gives no authoritative remaining-quota readout; we keep a LOCAL estimate
  and treat it as an estimate, not truth. Read the project's actual Cloud Console
  limits rather than assuming defaults.
- Planner guidance: PREFER channel/playlist traversal over search.

`captions.download` is owner-only and is NEVER used here as a transcript source —
transcripts come from the pull strategies.

The API client is injectable; tests pass a fake returning recorded fixtures so CI
spends no quota and makes no network calls.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from .schema import VideoRef

WATCH_URL = "https://www.youtube.com/watch?v={vid}"
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_ISO_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


# --- quota -------------------------------------------------------------------

class QuotaExceeded(RuntimeError):
    """A bucket's local budget estimate is exhausted. Carries which bucket."""
    def __init__(self, bucket: str):
        self.bucket = bucket
        super().__init__(f"quota exceeded for bucket: {bucket}")


@dataclass
class QuotaTracker:
    """Two independent budgets. Defaults are the documented API defaults; pass the
    project's ACTUAL Cloud Console limits when known."""
    search_limit: int = 100        # search.list — its own bucket
    general_limit: int = 10_000    # everything else — shared pool
    search_used: int = 0
    general_used: int = 0

    def charge(self, bucket: str, units: int = 1) -> None:
        if bucket == "search":
            if self.search_used + units > self.search_limit:
                raise QuotaExceeded("search")
            self.search_used += units
        else:
            if self.general_used + units > self.general_limit:
                raise QuotaExceeded("general")
            self.general_used += units

    def remaining(self) -> dict[str, int]:
        # An ESTIMATE — the API exposes no authoritative remaining count.
        return {"search": self.search_limit - self.search_used,
                "general": self.general_limit - self.general_used}


# --- client contract ---------------------------------------------------------

class DiscoveryClient(Protocol):
    """Each method mirrors a YouTube Data API v3 endpoint and returns its raw JSON
    dict. Quota is charged by the discover functions, not the client, so tracking
    holds regardless of which client is injected."""
    def search_list(self, **params) -> dict: ...
    def channels_list(self, **params) -> dict: ...
    def playlist_items_list(self, **params) -> dict: ...
    def videos_list(self, **params) -> dict: ...


class GoogleApiClient:
    """Default client over google-api-python-client (lazy import). Not exercised in
    CI — tests inject a fake."""
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._svc = None

    @property
    def svc(self):
        if self._svc is None:
            from googleapiclient.discovery import build  # lazy
            self._svc = build("youtube", "v3", developerKey=self._api_key, cache_discovery=False)
        return self._svc

    def search_list(self, **params) -> dict:
        return self.svc.search().list(**params).execute()

    def channels_list(self, **params) -> dict:
        return self.svc.channels().list(**params).execute()

    def playlist_items_list(self, **params) -> dict:
        return self.svc.playlistItems().list(**params).execute()

    def videos_list(self, **params) -> dict:
        return self.svc.videos().list(**params).execute()


# --- data --------------------------------------------------------------------

@dataclass
class DiscoveredVideo:
    ref: VideoRef
    title: Optional[str] = None
    channel_id: Optional[str] = None
    published_at: Optional[str] = None
    duration_seconds: Optional[int] = None
    is_live: bool = False
    is_short: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.ref.id, "url": self.ref.url, "title": self.title,
            "channel_id": self.channel_id, "published_at": self.published_at,
            "duration_seconds": self.duration_seconds,
            "is_live": self.is_live, "is_short": self.is_short,
        }


@dataclass
class DiscoveryResult:
    videos: list[DiscoveredVideo] = field(default_factory=list)
    # search.list is non-stable: persist the params so a re-run is interpretable.
    stability: dict = field(default_factory=dict)


def _video_ref(vid: str) -> VideoRef:
    return VideoRef(platform="youtube", id=vid, url=WATCH_URL.format(vid=vid), source="url")


def _parse_duration(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    m = _ISO_DUR.fullmatch(iso)
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


# --- resolution --------------------------------------------------------------

def resolve_channel_id(client: DiscoveryClient, quota: QuotaTracker,
                       handle_or_id: str, cache: Any | None = None) -> Optional[str]:
    """Resolve a channel id, @handle, or custom URL to a UC… channel id. Cheap path
    first (already an id), then channels.list?forHandle= (general pool), then a
    search fallback (search bucket). The mapping is cached in the metadata store."""
    handle_or_id = handle_or_id.strip()
    if _CHANNEL_ID.match(handle_or_id):
        return handle_or_id

    handle = handle_or_id if handle_or_id.startswith("@") else "@" + handle_or_id.lstrip("@")
    ckey = cache.metadata_key("channel_resolve", handle) if cache else None
    if cache and ckey:
        hit = cache.get_metadata(ckey)
        if hit and hit.get("channel_id"):
            return hit["channel_id"]

    quota.charge("general")
    resp = client.channels_list(part="id", forHandle=handle)
    items = resp.get("items", [])
    cid = items[0]["id"] if items else None

    if cid is None:                       # fallback: search (scarce bucket)
        quota.charge("search")
        sresp = client.search_list(part="snippet", q=handle, type="channel", maxResults=1)
        sitems = sresp.get("items", [])
        if sitems:
            cid = sitems[0]["snippet"]["channelId"]

    if cache and ckey and cid:
        cache.put_metadata(ckey, {"channel_id": cid})
    return cid


# --- channel / playlist traversal (preferred over search) --------------------

def channel_uploads(client: DiscoveryClient, quota: QuotaTracker, channel_or_handle: str,
                    *, max_n: int = 25, include_shorts: bool = True, include_live: bool = True,
                    cache: Any | None = None) -> DiscoveryResult:
    """Traverse a channel's uploads playlist. Channel-traversal semantics are
    explicit flags with documented defaults:
      - include_shorts (default True): when False, drops videos <= 60s.
      - include_live   (default True): when False, drops current/upcoming livestreams
        and past-livestream items.
    Reordered uploads are returned in playlist order; deleted/private placeholders
    are skipped (their snippet has no resourceId videoId or is enrichment-missing).
    """
    cid = resolve_channel_id(client, quota, channel_or_handle, cache=cache)
    if cid is None:
        return DiscoveryResult()

    quota.charge("general")
    chresp = client.channels_list(part="contentDetails", id=cid)
    items = chresp.get("items", [])
    if not items:
        return DiscoveryResult()
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    # Page through playlistItems (general pool, 1 unit/page) until we have enough ids.
    video_ids: list[str] = []
    page_token: Optional[str] = None
    while len(video_ids) < max_n:
        quota.charge("general")
        presp = client.playlist_items_list(
            part="contentDetails", playlistId=uploads, maxResults=50, pageToken=page_token)
        for it in presp.get("items", []):
            vid = it.get("contentDetails", {}).get("videoId")
            if vid:
                video_ids.append(vid)
        page_token = presp.get("nextPageToken")
        if not page_token:
            break

    enriched = enrich(client, quota, video_ids[: max_n * 2 if (not include_shorts or not include_live) else max_n])
    out: list[DiscoveredVideo] = []
    for vid in video_ids:
        dv = enriched.get(vid)
        if dv is None:                       # inaccessible / deleted placeholder
            continue
        if not include_shorts and dv.is_short:
            continue
        if not include_live and dv.is_live:
            continue
        out.append(dv)
        if len(out) >= max_n:
            break
    return DiscoveryResult(videos=out)


# --- search (the scarce bucket) ----------------------------------------------

def search_query(client: DiscoveryClient, quota: QuotaTracker, query: str, *,
                 max_n: int = 25, order: str = "relevance", region_code: Optional[str] = None,
                 relevance_language: Optional[str] = None, safe_search: str = "none") -> DiscoveryResult:
    """Query search. search.list is NOT stable, so we persist the params that shaped
    the result (regionCode, relevanceLanguage, safeSearch, order, query)."""
    params = dict(part="snippet", q=query, type="video", order=order,
                  maxResults=min(max_n, 50), safeSearch=safe_search)
    if region_code:
        params["regionCode"] = region_code
    if relevance_language:
        params["relevanceLanguage"] = relevance_language

    quota.charge("search")
    resp = client.search_list(**params)
    ids = [it["id"]["videoId"] for it in resp.get("items", []) if it.get("id", {}).get("videoId")]
    enriched = enrich(client, quota, ids)
    videos = [enriched[v] for v in ids if v in enriched][:max_n]
    stability = {"query": query, "order": order, "regionCode": region_code,
                 "relevanceLanguage": relevance_language, "safeSearch": safe_search}
    return DiscoveryResult(videos=videos, stability=stability)


# --- enrichment --------------------------------------------------------------

def enrich(client: DiscoveryClient, quota: QuotaTracker, video_ids: list[str]) -> dict[str, DiscoveredVideo]:
    """Batch videos.list (<=50 ids/call, general pool). Missing ids are treated as
    inaccessible/deleted (omitted), NOT errors."""
    out: dict[str, DiscoveredVideo] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        if not batch:
            continue
        quota.charge("general")
        resp = client.videos_list(part="snippet,contentDetails", id=",".join(batch))
        for it in resp.get("items", []):
            vid = it.get("id")
            if not vid:
                continue
            snip = it.get("snippet", {})
            cd = it.get("contentDetails", {})
            dur = _parse_duration(cd.get("duration"))
            live_state = snip.get("liveBroadcastContent", "none")
            is_live = live_state in ("live", "upcoming") or "liveStreamingDetails" in it
            out[vid] = DiscoveredVideo(
                ref=_video_ref(vid),
                title=snip.get("title"),
                channel_id=snip.get("channelId"),
                published_at=snip.get("publishedAt"),
                duration_seconds=dur,
                is_live=is_live,
                is_short=(dur is not None and dur <= 60),
            )
    return out
