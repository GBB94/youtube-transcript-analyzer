"""Phase 6 — discovery. Fake YouTube Data API client returns recorded fixtures;
no live network, no quota spend in CI. Covers channel traversal (with/without
Shorts and live), query search, handle->id resolution + cache hit, batched
videos.list enrichment with a missing id, dual-bucket budgeting, and the metadata
cache (separate store, <=30-day TTL, never the request-result cache)."""
import io
import json
from pathlib import Path

import pytest

from transcript_tool.cache import Cache
from transcript_tool.discover import (
    QuotaExceeded, QuotaTracker, channel_uploads, enrich, resolve_channel_id, search_query,
)

FIX = Path(__file__).parent / "fixtures"
CID = "UCabcdefghijklmnopqrstuv"     # UC + 22 chars


class FakeYouTube:
    def __init__(self):
        self.calls = []
        self.uploads = "UUabcdefghijklmnopqrstuv"
        self.videos = {
            "vidLongAAAAA": {"duration": "PT5M0S", "live": "none", "title": "Long talk"},
            "vidShortBBBB": {"duration": "PT45S", "live": "none", "title": "A Short"},
            "vidLiveCCCCC": {"duration": "PT2H0M", "live": "live", "title": "Livestream"},
            # "vidMissingDD" intentionally absent from the metadata table
        }

    def channels_list(self, **p):
        self.calls.append(("channels_list", p))
        if "forHandle" in p:
            return {"items": [{"id": CID}]}
        return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": self.uploads}}}]}

    def playlist_items_list(self, **p):
        self.calls.append(("playlist_items_list", p))
        return {"items": [{"contentDetails": {"videoId": v}} for v in
                          ("vidLongAAAAA", "vidShortBBBB", "vidLiveCCCCC", "vidMissingDD")],
                "nextPageToken": None}

    def videos_list(self, **p):
        self.calls.append(("videos_list", p))
        items = []
        for vid in p["id"].split(","):
            meta = self.videos.get(vid)
            if not meta:
                continue                      # missing id -> omitted, not an error
            items.append({"id": vid,
                          "snippet": {"title": meta["title"], "channelId": "UCx",
                                      "publishedAt": "2026-01-01T00:00:00Z",
                                      "liveBroadcastContent": meta["live"]},
                          "contentDetails": {"duration": meta["duration"]}})
        return {"items": items}

    def search_list(self, **p):
        self.calls.append(("search_list", p))
        return {"items": [{"id": {"videoId": "vidLongAAAAA"}},
                          {"id": {"videoId": "vidShortBBBB"}}]}


# --- channel traversal -------------------------------------------------------

def test_channel_uploads_includes_all_by_default_and_skips_missing():
    q = QuotaTracker()
    res = channel_uploads(FakeYouTube(), q, CID, max_n=10)
    ids = [v.ref.id for v in res.videos]
    assert ids == ["vidLongAAAAA", "vidShortBBBB", "vidLiveCCCCC"]
    assert "vidMissingDD" not in ids                      # gracefully dropped
    assert q.search_used == 0                             # channel path spends NO search quota
    assert q.general_used > 0
    assert res.videos[0].ref.url == "https://www.youtube.com/watch?v=vidLongAAAAA"


def test_channel_uploads_excludes_shorts():
    res = channel_uploads(FakeYouTube(), QuotaTracker(), CID, max_n=10, include_shorts=False)
    ids = [v.ref.id for v in res.videos]
    assert "vidShortBBBB" not in ids and "vidLongAAAAA" in ids


def test_channel_uploads_excludes_live():
    res = channel_uploads(FakeYouTube(), QuotaTracker(), CID, max_n=10, include_live=False)
    ids = [v.ref.id for v in res.videos]
    assert "vidLiveCCCCC" not in ids and "vidLongAAAAA" in ids


# --- search ------------------------------------------------------------------

def test_search_uses_search_bucket_and_persists_stability():
    q = QuotaTracker()
    res = search_query(FakeYouTube(), q, "cats", max_n=5, region_code="US")
    assert [v.ref.id for v in res.videos] == ["vidLongAAAAA", "vidShortBBBB"]
    assert q.search_used == 1                             # exactly one search call
    assert q.general_used >= 1                            # videos.list enrichment
    assert res.stability["query"] == "cats" and res.stability["regionCode"] == "US"


# --- dual-bucket budgeting ---------------------------------------------------

def test_buckets_tracked_independently():
    q = QuotaTracker()
    fake = FakeYouTube()
    search_query(fake, q, "cats", max_n=5)               # +1 search, +1 general
    channel_uploads(fake, q, CID, max_n=10)              # +N general, +0 search
    assert q.search_used == 1
    assert q.general_used >= 3                            # search-enrich + channel reads
    assert q.remaining()["search"] == q.search_limit - 1


def test_quota_exceeded_raises_with_bucket():
    q = QuotaTracker(search_limit=0)
    with pytest.raises(QuotaExceeded) as ei:
        search_query(FakeYouTube(), q, "cats")
    assert ei.value.bucket == "search"


# --- resolution + caching ----------------------------------------------------

def test_handle_resolution_and_cache_hit(tmp_path):
    fake, q, cache = FakeYouTube(), QuotaTracker(), Cache(tmp_path)
    cid1 = resolve_channel_id(fake, q, "@creator", cache=cache)
    assert cid1 == CID
    n_channel_calls = sum(1 for c in fake.calls if c[0] == "channels_list")
    # Second resolution is a cache hit: no additional channels_list call.
    cid2 = resolve_channel_id(fake, q, "@creator", cache=cache)
    assert cid2 == CID
    assert sum(1 for c in fake.calls if c[0] == "channels_list") == n_channel_calls


def test_metadata_goes_to_metadata_store_not_results(tmp_path):
    cache = Cache(tmp_path)
    resolve_channel_id(FakeYouTube(), QuotaTracker(), "@creator", cache=cache)
    assert any((tmp_path / "metadata").iterdir())          # written to metadata store
    assert not any((tmp_path / "results").iterdir())       # NOT the request-result cache


def test_metadata_ttl_clamped_to_30_days(tmp_path):
    from transcript_tool.cache import DEFAULT_METADATA_TTL
    cache = Cache(tmp_path)
    key = cache.metadata_key("x")
    cache.put_metadata(key, {"v": 1}, ttl=10 * 365 * 24 * 3600)   # ask for 10 years
    raw = json.loads((tmp_path / "metadata" / f"{key}.json").read_text())
    span = raw["_expires_at"] - __import__("datetime").datetime.fromisoformat(raw["_cached_at"]).timestamp()
    assert span <= DEFAULT_METADATA_TTL + 1


# --- enrichment --------------------------------------------------------------

def test_enrich_batches_and_drops_missing():
    out = enrich(FakeYouTube(), QuotaTracker(), ["vidLongAAAAA", "vidMissingDD"])
    assert "vidLongAAAAA" in out and "vidMissingDD" not in out
    assert out["vidLongAAAAA"].duration_seconds == 300


# --- find | pull round-trip (plumbing, no live network) ----------------------

def test_find_ids_pipe_into_pull_file_stdin(tmp_path, capsys, monkeypatch):
    """`find --format ids | pull --file -`: pull must consume newline-delimited
    targets from stdin. Proven with local caption fixtures so no network is needed."""
    from transcript_tool.cli import main
    targets = f"{FIX/'basic.srt'}\n{FIX/'rolling_autocaption.vtt'}\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(targets))
    rc = main(["--cache-dir", str(tmp_path), "pull", "--file", "-"])
    lines = [l for l in capsys.readouterr().out.strip().splitlines() if l]
    assert len(lines) == 2
    assert all(json.loads(l)["outcome"] == "success" for l in lines)
    assert rc == 0


def test_bare_youtube_id_classifies_as_url():
    from transcript_tool.cli import _classify_target
    ref, strategies = _classify_target("dQw4w9WgXcQ")
    assert ref.source == "url" and ref.id == "dQw4w9WgXcQ"
    assert "api_captions" in strategies
