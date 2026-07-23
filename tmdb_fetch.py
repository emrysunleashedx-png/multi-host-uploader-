"""
tmdb_fetch.py
---------------------------------------------------------------------------
Python port of the TMDB auto-fetch logic in js/admin.js (handleTmdbFetchClick
/ applyTmdbSelection and helpers), so the Telegram bot can pull the same
poster/backdrop/synopsis/genre/cast/etc. data admin.html already fetches
when an admin clicks "Auto-fetch from TMDB" -- just triggered automatically
after a Doodstream upload instead of requiring a manual click.

Deliberately scoped to TMDB only (no Trakt/OMDb fallback chain) -- TMDB
alone covers the large majority of titles, and the multi-source fallback
in admin.js adds a lot of surface area for a comparatively rare case.

Uses the SAME public TMDB API key already committed in js/admin.js
(TMDB_API_KEY there) -- it's a free v3 API key, not a secret, so reusing
it here doesn't create a new credential to manage.
"""

import re
import logging

import httpx

logger = logging.getLogger("tmdb_fetch")

TMDB_API_KEY = "66de412107b109a894e2d58a27fea0b6"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w780"
TMDB_BACKDROP_BASE = "https://image.tmdb.org/t/p/w1280"
TMDB_PROFILE_BASE = "https://image.tmdb.org/t/p/w300"
TMDB_THUMB_BASE = "https://image.tmdb.org/t/p/w154"

# Mirrors TMDB_GENRE_MAP in js/admin.js exactly.
TMDB_GENRE_MAP = {
    "Action": "Action", "Adventure": "Adventure", "Comedy": "Comedy",
    "Crime": "Crime", "Drama": "Drama", "Horror": "Horror", "Thriller": "Thriller",
    "Romance": "Romance", "Science Fiction": "Sci-Fi", "Fantasy": "Fantasy",
    "Animation": "Animation", "Documentary": "Documentary", "Family": "Family",
    "Mystery": "Mystery", "War": "War", "War & Politics": "War",
    "Comedy,Drama": "Comedy", "Sci-Fi & Fantasy": "Sci-Fi",
}

_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


def simplify_title(title: str) -> str:
    """Python port of simplifyTitle() in js/admin.js."""
    t = title
    t = re.sub(r"\(.*?\)", "", t)
    t = re.sub(r"\bseason\s*\d+\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(19|20)\d{2}\b", "", t)
    t = re.sub(r"[:\-\u2013\u2014].*$", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


async def _tmdb_search_by_type(client: httpx.AsyncClient, tmdb_type: str, query: str) -> list:
    url = f"https://api.themoviedb.org/3/search/{tmdb_type}"
    resp = await client.get(url, params={
        "api_key": TMDB_API_KEY, "query": query, "include_adult": "false",
    }, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("results", [])


async def _tmdb_search_multi(client: httpx.AsyncClient, query: str, want_type: str) -> list:
    url = "https://api.themoviedb.org/3/search/multi"
    resp = await client.get(url, params={
        "api_key": TMDB_API_KEY, "query": query, "include_adult": "false",
    }, timeout=_TIMEOUT)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return [r for r in results if r.get("media_type") == want_type]


async def search_tmdb(title: str, category: str) -> dict:
    """Port of handleTmdbFetchClick()'s search cascade (direct -> multi ->
    simplified-title retry). Returns {"matches": [...], "used_query": str}
    or {"matches": [], "used_query": None} if nothing was found anywhere.
    """
    tmdb_type = "tv" if category == "series" else "movie"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        matches = await _tmdb_search_by_type(client, tmdb_type, title)
        used_query = title

        if not matches:
            matches = await _tmdb_search_multi(client, title, tmdb_type)

        simplified = simplify_title(title)
        if not matches and simplified and simplified.lower() != title.lower():
            matches = await _tmdb_search_by_type(client, tmdb_type, simplified)
            if not matches:
                matches = await _tmdb_search_multi(client, simplified, tmdb_type)
            used_query = simplified

    if not matches:
        return {"matches": [], "used_query": None, "tmdb_type": tmdb_type}

    return {"matches": matches[:8], "used_query": used_query, "tmdb_type": tmdb_type}


def rank_matches_by_year(matches: list, year_hint: str = None) -> list:
    """Sort candidates to put the best year match first, when a year hint
    is available. Without a hint, preserves TMDB's own relevance ordering
    (matches list is returned as-is).
    """
    if not year_hint:
        return matches

    def match_year(m):
        date = m.get("release_date") or m.get("first_air_date") or ""
        return date[:4]

    def sort_key(m):
        # Exact year match sorts first (key 0), everything else keeps its
        # original relative order after that (stable sort preserves it).
        return 0 if match_year(m) == year_hint else 1

    return sorted(matches, key=sort_key)


def format_candidate_line(m: dict, index: int) -> str:
    """One line per candidate for a Telegram text list, e.g.:
    '1. The Scarecrow (2023)'"""
    name = m.get("title") or m.get("name") or "Untitled"
    date = m.get("release_date") or m.get("first_air_date") or ""
    year = date[:4] if date else ""
    return f"{index}. {name}" + (f" ({year})" if year else "")


def _pick_best_trailer(videos: list) -> str:
    yt = [v for v in videos if v.get("site") == "YouTube"]
    if not yt:
        return ""

    def rank(v):
        type_score = {"Trailer": 2, "Teaser": 1}.get(v.get("type"), 0)
        return type_score + (1 if v.get("official") else 0)

    yt.sort(key=rank, reverse=True)
    return f"https://www.youtube.com/watch?v={yt[0]['key']}"


def _map_cast(cast_arr: list) -> list:
    out = []
    for c in (cast_arr or [])[:12]:
        name = c.get("name") or ""
        if not name:
            continue
        out.append({
            "name": name,
            "character": c.get("character") or "",
            "photo": f"{TMDB_PROFILE_BASE}{c['profile_path']}" if c.get("profile_path") else "",
        })
    return out


def _pick_director(d: dict) -> str:
    created_by = d.get("created_by") or []
    if created_by:
        return ", ".join(c.get("name", "") for c in created_by if c.get("name"))
    crew = (d.get("credits") or {}).get("crew") or []
    directors = [c["name"] for c in crew if c.get("job") == "Director" and c.get("name")]
    return ", ".join(directors)


def _pick_content_rating(d: dict, tmdb_type: str) -> str:
    if tmdb_type == "tv":
        results = (d.get("content_ratings") or {}).get("results") or []
        us = next((r for r in results if r.get("iso_3166_1") == "US"), None)
        chosen = us or (results[0] if results else None)
        return (chosen or {}).get("rating", "") or ""
    results = (d.get("release_dates") or {}).get("results") or []
    us = next((r for r in results if r.get("iso_3166_1") == "US"), None)
    chosen = us or (results[0] if results else None)
    if not chosen:
        return ""
    for rd in chosen.get("release_dates", []):
        if rd.get("certification"):
            return rd["certification"]
    return ""


async def fetch_tmdb_details(tmdb_id: int, tmdb_type: str) -> dict:
    """Port of applyTmdbSelection()'s detail fetch + field extraction.
    Returns a flat dict of fields ready to merge into a Firestore payload.
    """
    appends = "videos,credits,content_ratings" if tmdb_type == "tv" else "videos,credits,release_dates"
    url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, params={
            "api_key": TMDB_API_KEY, "append_to_response": appends,
        }, timeout=_TIMEOUT)
        resp.raise_for_status()
        d = resp.json()

    name = d.get("title") or d.get("name") or ""
    date = d.get("release_date") or d.get("first_air_date") or ""
    year = date[:4] if date else ""
    poster = f"{TMDB_IMG_BASE}{d['poster_path']}" if d.get("poster_path") else ""
    backdrop = f"{TMDB_BACKDROP_BASE}{d['backdrop_path']}" if d.get("backdrop_path") else ""
    overview = d.get("overview") or ""

    runtime_min = d.get("runtime")
    if not runtime_min:
        ert = d.get("episode_run_time") or []
        runtime_min = ert[0] if ert else None

    genre_names = [g["name"] for g in (d.get("genres") or [])]
    mapped_genres = [TMDB_GENRE_MAP[g] for g in genre_names if g in TMDB_GENRE_MAP]
    primary_genre = mapped_genres[0] if mapped_genres else (genre_names[0] if genre_names else "")

    trailer_url = _pick_best_trailer((d.get("videos") or {}).get("results") or [])
    cast = _map_cast((d.get("credits") or {}).get("cast"))
    director = _pick_director(d)
    content_rating = _pick_content_rating(d, tmdb_type)
    vote_average = d.get("vote_average")
    if isinstance(vote_average, (int, float)):
        vote_average = round(vote_average, 1)
    else:
        vote_average = None

    return {
        "title": name,
        "year": year,
        "image": poster,
        "backdrop": backdrop,
        "description": overview,
        "duration": f"{runtime_min} min" if runtime_min else "",
        "genre": primary_genre,
        "genres": mapped_genres,
        "trailer": trailer_url,
        "director": director,
        "contentRating": content_rating,
        "voteAverage": vote_average,
        "cast": cast,
    }
