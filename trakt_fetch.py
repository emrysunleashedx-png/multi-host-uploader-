"""
trakt_fetch.py
---------------------------------------------------------------------------
Python port of the Trakt trending-discovery half of js/admin.js's Trending
tab (fetchTraktList / fetchAndRenderTrendingList) -- lets the bot show
what's currently trending/popular/anticipated on Trakt for movies or
series, as a read-only reference list.

Deliberately scoped to discovery only -- NOT a port of admTraktFetchBtn's
title-search-and-autofill flow (traktSearch / applyTraktSelection), which
stages metadata for an upload that hasn't happened yet. That flow has no
clean equivalent here: the bot's existing pipeline (main.py's PENDING_
PUBLISHES + tmdb_fetch.py) already fills in poster/synopsis/cast/etc.
once a title is confirmed via TMDB search after a real upload, so
duplicating Trakt's search+enrich path on top of that would be two
competing ways to do the same thing rather than a genuine gap. If a
"stage metadata for a future upload from a trending pick" workflow is
wanted later, that's a separate, deliberate feature to design -- not a
faithful transcription of the admin panel's version of it.

TMDB_IMG_BASE matches tmdb_fetch.py's constant of the same name -- Trakt
itself doesn't host poster images, but returns the same the TMDB id every
movie/show carries, and TMDB serves the actual artwork, so both modules
draw from the same TMDB image CDN base.
"""

import logging

import httpx

import tmdb_fetch

logger = logging.getLogger("trakt_fetch")

# Same public Client ID committed in js/admin.js (TRAKT_CLIENT_ID there) --
# a free, non-secret Trakt API client id (no OAuth/user login involved),
# so reusing it here doesn't create a new credential to manage. If this
# ever needs to change, get a new one at https://trakt.tv/oauth/applications.
TRAKT_CLIENT_ID = "3a7111b9a1bb3df1cdf4c22e0b97d5f4124292923f759cf3d943e39e76c1a211"

TRAKT_HEADERS = {
    "Content-Type": "application/json",
    "trakt-api-version": "2",
    "trakt-api-key": TRAKT_CLIENT_ID,
}

_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

TRAKT_KINDS = ("trending", "popular", "anticipated")
TRAKT_TYPES = ("movie", "show")


async def fetch_trakt_list(trakt_type: str, kind: str, limit: int = 10) -> list:
    """Port of fetchTraktList() in js/admin.js. trakt_type is "movie" or
    "show"; kind is "trending", "popular", or "anticipated".

    Returns a list of raw Trakt movie/show dicts (title, year, ids.tmdb,
    ids.imdb, etc.) -- trending/anticipated wrap each item under a
    "movie"/"show" key, popular returns the bare item; both shapes are
    normalized here the same way the JS does.
    """
    if trakt_type not in TRAKT_TYPES:
        raise ValueError(f"trakt_type must be one of {TRAKT_TYPES}, got {trakt_type!r}")
    if kind not in TRAKT_KINDS:
        raise ValueError(f"kind must be one of {TRAKT_KINDS}, got {kind!r}")

    plural = "shows" if trakt_type == "show" else "movies"
    url = f"https://api.trakt.tv/{plural}/{kind}"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, params={"limit": limit}, headers=TRAKT_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        raw = resp.json()

    items = []
    for r in raw:
        item = r.get(trakt_type) if isinstance(r, dict) and trakt_type in r else r
        if item:
            items.append(item)
    return items


async def fetch_trending_with_posters(trakt_type: str, kind: str, limit: int = 10) -> list:
    """fetch_trakt_list() plus a poster URL for each item, resolved via
    TMDB using the tmdb id Trakt already gives us (Trakt itself doesn't
    host images -- see the module docstring). Poster lookups run
    concurrently rather than one-by-one, since a 10-item list would
    otherwise mean 10 sequential round-trips to TMDB.

    Returns a list of dicts: [{"title", "year", "tmdb_id", "imdb_id",
    "poster_url"}, ...]. poster_url is "" if the item had no tmdb id, or
    the TMDB lookup failed for that one item -- a single missing poster
    doesn't take down the rest of the list.
    """
    import asyncio

    items = await fetch_trakt_list(trakt_type, kind, limit)
    tmdb_type = "tv" if trakt_type == "show" else "movie"

    async def _resolve_poster(client: httpx.AsyncClient, tmdb_id):
        if not tmdb_id:
            return ""
        try:
            resp = await client.get(
                f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}",
                params={"api_key": tmdb_fetch.TMDB_API_KEY},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            poster_path = data.get("poster_path")
            return f"{tmdb_fetch.TMDB_IMG_BASE}{poster_path}" if poster_path else ""
        except Exception as e:
            logger.warning("Poster lookup failed for tmdb_id=%s: %s", tmdb_id, e)
            return ""

    results = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        poster_tasks = [_resolve_poster(client, (item.get("ids") or {}).get("tmdb")) for item in items]
        posters = await asyncio.gather(*poster_tasks)

    for item, poster_url in zip(items, posters):
        ids = item.get("ids") or {}
        results.append({
            "title": item.get("title") or "Untitled",
            "year": item.get("year") or "",
            "tmdb_id": ids.get("tmdb"),
            "imdb_id": ids.get("imdb"),
            "poster_url": poster_url,
        })

    return results


def format_trending_line(item: dict, index: int) -> str:
    """One line per item for a Telegram text list, e.g.:
    '1. The Scarecrow (2023)'"""
    year_part = f" ({item['year']})" if item.get("year") else ""
    return f"{index}. {item['title']}{year_part}"
