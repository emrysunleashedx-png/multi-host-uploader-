"""
firestore_publish.py
---------------------------------------------------------------------------
Publishes a finished Doodstream upload link into the EmrysHub Firestore
`movies` collection, matching the existing admin-panel document shape
(see js/admin.js: executeAdminMovieUpload / collectDirectLinks).

Uses the Firebase Admin SDK (service-account auth), which bypasses
firestore.rules entirely -- the same mechanism functions/index.js uses.
This is required because firestore.rules restricts all `movies` writes to
authenticated users with a custom `admin: true` claim, and this bot is not
a logged-in Firebase user.

SETUP (one-time):
  1. pip install firebase-admin --break-system-packages
  2. Firebase Console -> Project Settings -> Service Accounts ->
     "Generate new private key" -> download the JSON file.
  3. Set an env var pointing at it:
       export FIREBASE_SERVICE_ACCOUNT_PATH=/path/to/serviceAccountKey.json
     (Keep this file out of git -- it's a full-admin credential, not a
     public client key like the one in js/firebase.js.)

MATCHING LOGIC:
  New uploads are matched to an existing series by slugify(title) -- the
  same slug the site already computes and stores on every doc (see
  js/slug.js and payload.slug in admin.js). If a doc with that slug
  exists, the new link is appended to its `directLinks` array. Otherwise
  a new minimal `movies` doc is created.

  This intentionally mirrors, rather than duplicates, the shape admin.js
  writes -- so entries created by the bot look identical in the admin
  panel to ones created by hand, including the placeholder Streamtape-era
  fields (unused for Doodstream, but kept so existing admin.js rendering
  code that reads link.streamtapeStatus etc. doesn't choke on missing keys).

EPISODE FORMAT:
  Episodes are tracked and matched everywhere in this bot (dedup checks,
  /edit, inference, Telegram messages) using the compact "S01E02" form,
  since that's compact, sortable, and easy to parse back out of a
  filename/caption. The *site's* link builder, however, expects a plain
  "Episode 2" label in each link's episode field (see admin.html's
  Episode / Server Link Builder screenshots) -- it doesn't understand or
  re-render "S01E02". build_link_entry() is the single place that
  converts from the internal S01E02 form to the site-facing "Episode N"
  form, right at the point a link entry is built for Firestore. Nothing
  upstream of that (dedup, inference, etc.) needs to change, since they
  all operate on the internal form via the `episode` argument callers
  pass in.

CATEGORY VALUES:
  The site (js/admin.js) stores non-series entries with category ==
  "movies" (plural) -- see admCategory's <option value="movies"> and every
  `item.category !== "series"` / `item.category === "series"` check in
  admin.js's Catalog Ledger filter and stats strip. This module and
  main.py write "movies"/"series" to match. (An earlier version of this
  bot wrote singular "movie", which silently fell through admin.html's
  "Movies only" filter since it only ever matches "series" or falls back
  to "not series" -- "movie" != "series" so it still displayed under "All",
  just not under the movies-only filter. If you have older bot-published
  docs with category "movie", see migrate_movie_category_typos() below.)
"""

import os
import re
import unicodedata
import logging

logger = logging.getLogger("firestore_publish")

ROOT_COLLECTION = "movies"

# Marks the start of the bot-managed "episode/server/link" block appended
# to a doc's description field (mirrors what admin.html's "Generate
# Synopsis" button builds by hand -- see format_links_block below). Text
# above this marker is left alone on every publish; everything from the
# marker down is fully rebuilt from the current directLinks each time.
_LINKS_MARKER = "— Links —"

_app = None
_db = None
_firebase_admin = None
_credentials = None
_firestore = None


def _import_firebase_admin():
    """Import firebase_admin lazily so this whole module -- and anything
    that imports it, like bot_fixed.py -- doesn't hard-crash at process
    startup just because `pip install firebase-admin` hasn't been run yet.
    Doodstream uploads don't need Firebase at all; only /confirm and /edit
    do, so failures here should only surface when those are actually used.
    """
    global _firebase_admin, _credentials, _firestore
    if _firebase_admin is not None:
        return
    try:
        import firebase_admin as fa
        from firebase_admin import credentials as cr, firestore as fs
    except ImportError as e:
        raise RuntimeError(
            "firebase-admin is not installed. Run: "
            "pip install firebase-admin --break-system-packages"
        ) from e
    _firebase_admin = fa
    _credentials = cr
    _firestore = fs


def init_firebase():
    """Initialize the Firebase Admin app once. Call this at bot startup
    (or it will be called automatically on first use from publish_doodstream_link)."""
    global _app, _db
    if _app is not None:
        return _db

    _import_firebase_admin()

    cred_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "")
    if not cred_path or not os.path.exists(cred_path):
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT_PATH is not set or the file doesn't exist. "
            "Download a service account key from Firebase Console -> Project "
            "Settings -> Service Accounts, and point this env var at it."
        )

    cred = _credentials.Certificate(cred_path)
    _app = _firebase_admin.initialize_app(cred)
    _db = _firestore.client()
    logger.info("Firebase Admin initialized (project: %s)", cred.project_id)
    return _db


def slugify(title: str) -> str:
    """Python port of js/slug.js's slugify, kept behaviorally identical so
    the bot matches the exact same doc the admin panel would."""
    if not title:
        return ""
    # NFKD normalize + strip combining marks (café -> cafe), matching the JS.
    normalized = unicodedata.normalize("NFKD", title)
    stripped = "".join(c for c in normalized if not unicodedata.combining(c))
    lowered = stripped.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    slug = slug.strip("-")
    return slug[:80]


def parse_title_and_episode(caption: str):
    """Best-effort split of a forwarded caption like
    '@dramalandhub The Scarecrow S01E01 eng sub' into
    ('The Scarecrow', 'S01E01'). This is intentionally only a *starting
    guess* -- the bot always shows this to the admin for confirmation
    before publishing (caption formatting varies too much across source
    channels to trust blindly; see conversation history for why this
    isn't auto-published without a human check).
    """
    if not caption:
        return None, None

    text = caption.strip()
    # Drop a leading @channelname mention, allowing an optional space
    # between "@" and the name (seen in the wild: "@ dramalandhub ...").
    text = re.sub(r"^@\s*\S+\s*", "", text)

    match = re.search(r"\bS(\d{1,2})E(\d{1,3})\b", text, re.IGNORECASE)
    episode = None
    title = text

    if match:
        episode = f"S{int(match.group(1)):02d}E{int(match.group(2)):02d}"
        title = text[: match.start()].strip(" .-")

    # Many source channels use dots/underscores as word separators
    # (e.g. "The.Scarecrow.S01E01.mkv") -- normalize those to spaces
    # before stripping noise words, so the guess reads like a real title.
    title = re.sub(r"[._]+", " ", title)

    # Strip bracketed site watermarks, e.g. "(NKIRI.COM)", "[SITENAME.NET]" --
    # these are the source channel's branding, not part of the real title.
    # Matches a bracket/paren group containing a domain-like token
    # (word.word), so it won't accidentally eat a legitimate parenthetical
    # like "(2023)" or "(Uncut)".
    title = re.sub(r"[\(\[][A-Za-z0-9-]+ [A-Za-z]{2,4}[\)\]]", "", title)

    # Strip trailing noise like "eng sub", "1080p", file extensions, etc.
    # Conservative list -- better to leave junk in for a human to remove
    # than to strip something that was actually part of the real title.
    title = re.sub(
        r"\b(eng sub|english sub|multi sub|hd|1080p|720p|480p|mkv|mp4)\b",
        "", title, flags=re.IGNORECASE,
    )
    title = re.sub(r"\s{2,}", " ", title).strip(" .-")

    return title or None, episode


_EPISODE_RE = re.compile(r"^S(\d{1,2})E(\d{1,3})$", re.IGNORECASE)


def infer_next_episode(title: str) -> str:
    """If `title` matches an existing series in Firestore, look at its
    current directLinks and guess the next sequential episode number
    (e.g. if S01E03 is the highest existing episode, guess S01E04).

    Deliberately conservative: only used when the source text had NO
    episode marker at all (see bot_fixed.py's caller) -- if a filename
    already says S01E05, that explicit value always wins over any guess.
    Returns "" (meaning "no guess") if:
      - no matching series exists yet (nothing to infer from)
      - the existing series has no parseable S##E## episodes (e.g. it's
        actually a movie, or uses a numbering scheme this doesn't
        recognize) -- guessing wrong here would mislabel content, so we
        simply decline to guess rather than risk it.

    NOTE: this reads the "episode" field back off of existing directLinks
    entries, which (as of this bot version) are stored in the site-facing
    "Episode N" form rather than "S01E02" -- see build_link_entry(). To
    keep inference working against both older docs (still S01E02-shaped)
    and newer ones, the highest-episode scan below tries both forms.
    """
    db = _db or init_firebase()
    slug = slugify(title)
    if not slug:
        return ""

    existing = find_series_by_slug(db, slug)
    if not existing:
        return ""

    data = existing.to_dict() or {}
    links = data.get("directLinks", []) or []

    best_season, best_episode = None, None
    for link in links:
        raw_episode = (link.get("episode") or "").strip()
        season_num, episode_num = _parse_any_episode_format(raw_episode)
        if episode_num is None:
            continue
        # Season defaults to 1 when the stored form doesn't carry a season
        # number at all (the site-facing "Episode N" form doesn't).
        season_num = season_num or 1
        if best_season is None or (season_num, episode_num) > (best_season, best_episode):
            best_season, best_episode = season_num, episode_num

    if best_season is None:
        return ""

    return f"S{best_season:02d}E{best_episode + 1:02d}"


def _parse_any_episode_format(raw_episode: str):
    """Parse either the internal 'S01E02' form or the site-facing
    'Episode 2' form into (season_or_None, episode_number_or_None).
    Used only by infer_next_episode's back-compat scan.
    """
    if not raw_episode:
        return None, None
    m = _EPISODE_RE.match(raw_episode)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^Episode\s+(\d{1,3})$", raw_episode, re.IGNORECASE)
    if m:
        return None, int(m.group(1))
    return None, None


def format_episode_for_site(episode: str) -> str:
    """Convert the internal 'S01E02' episode form into the plain
    'Episode 2' label the site's Episode / Server Link Builder expects
    (see admin.html screenshots -- fields read "episode 1", "episode 2",
    not "S01E01"). Season info is intentionally dropped since the site
    has no season concept in this field; multi-season series are handled
    by "Add Episode Group" on the site side, not by this bot.

    Non-S##E##-shaped input (already plain text, or something unexpected)
    is passed through unchanged, since guessing at reformatting something
    that doesn't match the expected shape risks mangling it worse than
    leaving it alone.
    """
    if not episode:
        return episode
    match = _EPISODE_RE.match(episode.strip())
    if not match:
        return episode
    episode_num = int(match.group(2))
    return f"Episode {episode_num}"


def detect_quality(source_text: str) -> str:
    """Best-effort video quality detection from a filename/caption, e.g.
    'Kangchi.The.Beginning.S01E01.1080p.mkv' -> '1080p'. Returns '' if no
    recognizable quality tag is found -- callers should treat that as
    'unknown', not 'definitely not 1080p', since plenty of uploads simply
    don't mention quality in the filename at all.
    """
    if not source_text:
        return ""
    match = re.search(r"\b(2160p|4k|1080p|720p|480p)\b", source_text, re.IGNORECASE)
    if not match:
        return ""
    tag = match.group(1).lower()
    if tag == "4k":
        return "2160p"
    return tag


def format_file_size(num_bytes: int) -> str:
    """Human-readable file size for the site's 'File Size (e.g., 450MB)'
    field, e.g. 1_363_148_800 -> '1.3GB'. Uses binary (1024-based) units
    to match how Telegram/most OSes report file sizes, and picks MB vs GB
    based on magnitude so small files don't show as '0.1GB' and large
    ones don't show as '1400MB'.
    """
    if not num_bytes or num_bytes <= 0:
        return ""
    mb = num_bytes / (1024 ** 2)
    if mb >= 1024:
        return f"{mb / 1024:.1f}GB"
    return f"{mb:.0f}MB"


def find_series_by_slug(db, slug: str):
    """Look up an existing movies/ doc by its stored slug field."""
    query = db.collection(ROOT_COLLECTION).where("slug", "==", slug).limit(1)
    results = list(query.stream())
    return results[0] if results else None


def check_existing_episode(title: str, episode: str) -> dict:
    """Check whether a title/episode is already published, BEFORE
    downloading or uploading anything -- avoids wasting Telegram download
    time and Doodstream upload bandwidth on a file that's already on the
    site.

    This is intentionally a lighter-weight check than the one inside
    publish_doodstream_link (which also matches by exact sourceUrl, since
    that's only knowable after a Doodstream URL already exists). Here we
    can only go by title+episode, since that's all that's available
    before the file has been uploaded anywhere.

    `episode` here is always the internal 'S01E02' form (this is called
    before build_link_entry ever converts anything), so comparisons
    against stored links use _parse_any_episode_format for back-compat
    with the site-facing 'Episode N' form now written by build_link_entry.

    Returns {"exists": False} or
            {"exists": True, "doc_id": "...", "title": "...",
             "episode": "..." } for the caller to show the admin.
    """
    db = _db or init_firebase()

    slug = slugify(title)
    if not slug:
        return {"exists": False}

    existing = find_series_by_slug(db, slug)
    if not existing:
        return {"exists": False}

    data = existing.to_dict() or {}
    links = data.get("directLinks", []) or []

    # A movie (no episode) matching an existing slug is itself a duplicate
    # of the whole entry, not just one link within it.
    if not episode:
        return {"exists": True, "doc_id": existing.id, "title": data.get("title", title), "episode": None}

    _, wanted_episode_num = _parse_any_episode_format(episode)

    for link in links:
        stored = (link.get("episode") or "").strip()
        if stored == episode:
            return {"exists": True, "doc_id": existing.id, "title": data.get("title", title), "episode": episode}
        # Also match across formats (internal S01E02 vs site-facing
        # "Episode N"), so a duplicate check doesn't miss an episode just
        # because it's stored in the newer site-facing form.
        _, stored_episode_num = _parse_any_episode_format(stored)
        if wanted_episode_num is not None and stored_episode_num == wanted_episode_num:
            return {"exists": True, "doc_id": existing.id, "title": data.get("title", title), "episode": episode}

    return {"exists": False}


def search_titles(query: str, limit: int = 8) -> list:
    """Find published movies/series whose title contains the query text
    (case-insensitive, substring match).

    Firestore has no native full-text/substring search, so this fetches
    title+slug+category+directLinks for the whole collection and filters
    in Python. That's fine at the catalog sizes this bot is designed for
    (a personal/small-team upload pipeline, not a large streaming site's
    full library) but would need a real search index (Algolia, Typesense,
    Firestore's array-contains tricks, etc.) if the catalog grows into
    the thousands+ range -- worth revisiting if /find starts feeling slow.

    Returns a list of dicts: [{"title", "slug", "category", "episode_count"}, ...]
    """
    db = _db or init_firebase()
    query_lower = query.strip().lower()
    if not query_lower:
        return []

    results = []
    # Only pull the fields actually needed for display, not full docs
    # (poster/description/cast etc aren't needed just to list matches).
    docs = db.collection(ROOT_COLLECTION).select(
        ["title", "slug", "category", "directLinks"]
    ).stream()

    for doc in docs:
        data = doc.to_dict() or {}
        title = data.get("title", "")
        if query_lower in title.lower():
            links = data.get("directLinks", []) or []
            results.append({
                "title": title,
                "slug": data.get("slug", ""),
                "category": data.get("category", ""),
                "episode_count": len(links),
            })
        if len(results) >= limit:
            break

    return results


# Field name -> emoji shown in admin.html's list view (see the flags.push()
# lines in js/admin.js), used to make bot confirmations recognizable
# against what you'd see in the admin panel itself.
TOGGLEABLE_FLAGS = {
    "featured": ("isFeatured", "⭐"),
    "trending": ("isTrending", "🔥"),
    "new": ("isNewRelease", "🆕"),
    "recommended": ("isRecommended", "👍"),
}


def toggle_flag(title: str, flag_name: str, value: bool = None) -> dict:
    """Set (or flip) one of the admin.html quick-toggle booleans
    (isFeatured / isTrending / isNewRelease / isRecommended) on a
    published title, found by exact title match via its slug.

    flag_name must be one of TOGGLEABLE_FLAGS' keys ("featured",
    "trending", "new", "recommended") -- NOT the raw Firestore field name,
    so the bot-facing command surface stays readable without needing to
    know Firestore's internal field naming.

    If value is None, flips whatever the current value is. If value is
    True/False, sets it explicitly regardless of the current value.

    Returns {"found": False} if no matching title exists, or
            {"found": True, "title": "...", "field": "...", "new_value": bool}
    """
    if flag_name not in TOGGLEABLE_FLAGS:
        raise ValueError(f"Unknown flag {flag_name!r}, must be one of {list(TOGGLEABLE_FLAGS)}")

    field_name, _ = TOGGLEABLE_FLAGS[flag_name]
    db = _db or init_firebase()

    slug = slugify(title)
    if not slug:
        return {"found": False}

    existing = find_series_by_slug(db, slug)
    if not existing:
        return {"found": False}

    data = existing.to_dict() or {}
    current = bool(data.get(field_name, False))
    new_value = (not current) if value is None else bool(value)

    existing.reference.update({field_name: new_value})
    logger.info("Toggled %s on doc %s (title=%r) to %s", field_name, existing.id, title, new_value)

    return {
        "found": True,
        "title": data.get("title", title),
        "field": field_name,
        "new_value": new_value,
    }


def build_link_entry(episode: str, server: str, url: str) -> dict:
    """Match the exact shape js/admin.js: collectDirectLinks() produces,
    so entries the bot writes render identically in the admin panel.
    The streamtape* fields are Streamtape-pipeline leftovers unused by
    Doodstream, but kept null (not omitted) since admin.js UI code reads
    link.streamtapeStatus directly without an existence check.

    `episode` is accepted in the bot's internal 'S01E02' form (or None
    for a movie) and converted here to the site-facing 'Episode N' label
    via format_episode_for_site -- this is the one place that conversion
    happens, so every caller elsewhere in the bot keeps working with the
    internal form.
    """
    return {
        "episode": format_episode_for_site(episode) if episode else episode,
        "server": server,
        "sourceUrl": url,
        "streamtapeStatus": None,
        "streamtapeUrl": None,
        "streamtapeId": None,
        "streamtapeError": None,
    }


def format_links_block(links: list) -> str:
    """Python port of the line-building loop inside
    js/admin.js: generateSynopsisFromBuilder() -- NOT the whole function,
    just the part that turns directLinks rows into text lines:
        Episode 1
        Download link: https://dood.to/d/abc
        Download link: https://dood.to/d/xyz   (no repeated episode line
                                                  if it matches the previous row)
    Rows without a URL are skipped, matching the JS's `if (!url) return;`.

    The label is always "Download link" rather than the hoster's actual
    name (e.g. "Doodstream") -- this is a display choice for the site's
    synopsis text, independent of the `server` field itself, which still
    stores the real hoster name for any other logic that needs it.

    Links' `episode` field is already in the site-facing "Episode N" form
    by the time it reaches here (build_link_entry converts it), so no
    further conversion is needed in this function.
    """
    lines = []
    last_episode = None
    for link in links:
        url = (link.get("sourceUrl") or "").strip()
        if not url:
            continue
        episode = (link.get("episode") or "").strip()
        if episode and episode != last_episode:
            lines.append(episode)
            last_episode = episode
        lines.append(f"Download link: {url}")
    return "\n".join(lines)


def build_full_description(tmdb_synopsis: str, links: list) -> str:
    """Build the initial description for a brand-new doc: TMDB synopsis
    (if any), then the marked bot-managed links block. Uses the same
    _LINKS_MARKER as rebuild_description_with_links so later episode
    appends can find and replace this block instead of accumulating a
    second, duplicate copy of it.
    """
    return rebuild_description_with_links(tmdb_synopsis, links)


def rebuild_description_with_links(existing_description: str, links: list) -> str:
    """Rebuild the description's auto-generated links block from the
    complete current set of directLinks, replacing any block this bot
    previously appended rather than piling up a fresh copy on every new
    episode.

    admin.html's own "Generate Synopsis" button doesn't guard against this
    at all -- clicking it repeatedly just keeps re-appending -- but since
    the bot calls this automatically on every single confirmed upload,
    blindly porting that same behavior would visibly duplicate the whole
    links list every time a new episode is added. Instead, everything
    below a fixed marker line is treated as bot-managed and fully
    replaced; everything above it (the TMDB synopsis, or anything the
    admin typed by hand) is left untouched.
    """
    marker = "\n\n" + _LINKS_MARKER + "\n"
    existing = existing_description or ""

    if _LINKS_MARKER in existing:
        prose_part = existing.split(_LINKS_MARKER, 1)[0].rstrip()
    else:
        prose_part = existing.strip()

    links_block = format_links_block(links)
    if not links_block:
        return prose_part

    if prose_part:
        return f"{prose_part}{marker}{links_block}"
    return f"{_LINKS_MARKER}\n{links_block}"


def publish_doodstream_link(title: str, episode: str, doodstream_url: str,
                             server_label: str = "Doodstream",
                             category: str = "series",
                             extra_fields: dict = None,
                             quality: str = "",
                             file_size_bytes: int = 0) -> dict:
    """Create-or-append the finished Doodstream link into Firestore.

    quality, if given (e.g. "1080p" from detect_quality()), sets the
    p1080 badge field. On an existing doc, p1080 is only ever upgraded
    (False -> True), never downgraded -- the badge represents "1080p is
    available for this title", and an admin-confirmed 1080p episode
    shouldn't get quietly reset to False just because a later episode's
    filename didn't happen to mention quality.

    extra_fields, if given, is merged into a *newly created* doc only
    (e.g. TMDB-fetched poster/description/genre/cast/etc. -- see
    tmdb_fetch.py). It is intentionally NOT applied when appending to an
    existing series, so re-confirming an episode for an already-published
    series never silently overwrites admin-edited fields like a
    hand-picked poster or corrected synopsis.

    file_size_bytes, if given, is the raw Telegram file size for this
    upload. It's used as a fallback for the doc's `size` field only when
    TMDB didn't already supply one via extra_fields (TMDB movie/tv detail
    responses essentially never include a file size, so in practice this
    fallback is what actually populates the field most of the time -- see
    format_file_size). Like quality/p1080, size is only ever set from
    within this function on doc creation; it's not re-derived or
    overwritten on append, since a series' "size" field describes
    whichever single file (or nominal size) was recorded when the entry
    was first created, and silently changing it every time a new episode
    with a different file size is appended would be more confusing than
    useful.

    category should be "movies" or "series" (matching the site's stored
    values -- see the CATEGORY VALUES note at the top of this file).

    Returns a dict describing what happened, e.g.:
        {"action": "appended", "doc_id": "...", "slug": "..."}
        {"action": "created",  "doc_id": "...", "slug": "..."}
        {"action": "skipped_duplicate", "doc_id": "...", "slug": "..."}
    """
    db = _db or init_firebase()

    slug = slugify(title)
    if not slug:
        raise ValueError(f"Could not derive a slug from title: {title!r}")

    existing = find_series_by_slug(db, slug)
    new_link = build_link_entry(episode, server_label, doodstream_url)

    if existing:
        doc_ref = existing.reference
        data = existing.to_dict() or {}
        links = data.get("directLinks", []) or []

        # Avoid duplicate entries if the same episode+url is re-processed
        # (e.g. bot restart re-handling an already-uploaded file). Compare
        # by the already-converted site-facing episode label here, since
        # that's the form both the new and existing entries are stored in.
        already_present = any(
            l.get("sourceUrl") == doodstream_url or
            (new_link["episode"] and l.get("episode") == new_link["episode"] and l.get("server") == server_label)
            for l in links
        )
        if already_present:
            logger.info("Skipping duplicate link for slug=%s episode=%s", slug, episode)
            return {"action": "skipped_duplicate", "doc_id": doc_ref.id, "slug": slug}

        links.append(new_link)
        new_description = rebuild_description_with_links(data.get("description", ""), links)
        update_payload = {"directLinks": links, "description": new_description}

        # Upgrade-only: set p1080 True if this upload is 1080p+ and the
        # doc doesn't already have it set, but never flip an existing
        # True back to False just because this particular episode's
        # filename didn't mention quality.
        is_1080_or_better = quality in ("1080p", "2160p")
        if is_1080_or_better and not data.get("p1080"):
            update_payload["p1080"] = True

        doc_ref.update(update_payload)
        logger.info("Appended link to existing doc %s (slug=%s, episode=%s)",
                    doc_ref.id, slug, episode)
        return {"action": "appended", "doc_id": doc_ref.id, "slug": slug}

    # No existing series found -- create a new doc. Start from the minimal
    # defaults, then layer any TMDB-fetched fields on top so poster/genre/
    # cast/etc. are pre-filled when available, matching what admin.html's
    # "Auto-fetch from TMDB" button would have produced by hand.
    payload = {
        "title": title,
        "slug": slug,
        "category": category,
        "image": "",
        "year": "",
        "size": "",
        "genre": "",
        "description": "",
        "p1080": quality in ("1080p", "2160p"),
        "isFeatured": False,
        "isTrending": False,
        "isNewRelease": True,
        "isRecommended": False,
        "views": 0,
        "directLinks": [new_link],
        "timestamp": _firestore.SERVER_TIMESTAMP,
    }
    if extra_fields:
        # Only accept known/expected keys -- never let an unexpected key
        # from a TMDB response shape change accidentally clobber something
        # like `slug` or `directLinks` computed just above.
        allowed_keys = {
            "title", "year", "image", "backdrop", "description", "duration",
            "genre", "genres", "trailer", "director", "contentRating",
            "voteAverage", "cast", "language", "size",
        }
        for key, value in extra_fields.items():
            if key in allowed_keys and value not in (None, "", []):
                payload[key] = value

    # File size: prefer whatever TMDB supplied (rare, but respected if
    # present via extra_fields above), otherwise fall back to the actual
    # Telegram file size of this upload -- this is the field admin.html
    # labels "File Size (e.g., 450MB)" and, per the screenshots, is
    # otherwise left for the admin to type in by hand every time.
    if not payload.get("size") and file_size_bytes:
        formatted_size = format_file_size(file_size_bytes)
        if formatted_size:
            payload["size"] = formatted_size

    # Build the initial description exactly like admin.html's "Generate
    # Synopsis" button would: TMDB synopsis (if any) followed by the
    # formatted episode/link block for this first upload.
    payload["description"] = build_full_description(payload.get("description", ""), payload["directLinks"])

    _, doc_ref = db.collection(ROOT_COLLECTION).add(payload)
    logger.info("Created new doc %s for title=%r (slug=%s)", doc_ref.id, title, slug)
    return {"action": "created", "doc_id": doc_ref.id, "slug": slug}


# ── Catalog editing / deletion ───────────────────────────────────────────
# Mirrors admin.html's Catalog Ledger edit (pencil) / delete (trash)
# actions -- see js/admin.js: triggerAdminEdit / triggerAdminDelete /
# executeAdminMovieUpload's updateDoc branch. Unlike the web form (which
# always writes every field, since it's a full form re-render), these are
# PARTIAL updates: only fields explicitly named by the admin are touched,
# since a chat command has no pre-populated form to submit in full.

# field -> Python type its value should be coerced to before writing.
# "str" fields are passed through as-is (already strings from Telegram
# text). Anything not listed here is rejected by parse_edit_field, rather
# than silently writing an unexpected key that admin.js's rendering code
# doesn't know about.
_EDITABLE_FIELD_TYPES = {
    "title": "str",       # NOTE: does not re-slug or move the doc -- see parse_edit_field
    "year": "str",
    "size": "str",
    "genre": "str",
    "category": "category",
    "description": "str",
    "image": "str",
    "backdrop": "str",
    "trailer": "str",
    "duration": "str",
    "language": "str",
    "director": "str",
    "contentRating": "str",
    "voteAverage": "float",
    "p1080": "bool",
    "isFeatured": "bool",
    "isTrending": "bool",
    "isNewRelease": "bool",
    "isRecommended": "bool",
}


def parse_edit_field(field: str, raw_value: str):
    """Validate and coerce one `/edittitle` field=value argument.
    Raises ValueError with a human-readable message on anything invalid,
    which handle_edit_title in main.py surfaces directly to the admin.
    """
    if field not in _EDITABLE_FIELD_TYPES:
        raise ValueError(
            f"`{field}` isn't an editable field. Editable: {', '.join(sorted(_EDITABLE_FIELD_TYPES))}"
        )

    kind = _EDITABLE_FIELD_TYPES[field]

    if kind == "str":
        return raw_value  # empty string is valid -- clears the field

    if kind == "bool":
        lowered = raw_value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"`{field}` must be true/false, got `{raw_value}`")

    if kind == "float":
        try:
            return float(raw_value)
        except ValueError:
            raise ValueError(f"`{field}` must be a number, got `{raw_value}`")

    if kind == "category":
        val = raw_value.strip().lower()
        if val not in ("movies", "series"):
            raise ValueError(f"`{field}` must be `movies` or `series`, got `{raw_value}`")
        return val

    raise ValueError(f"Unhandled field type for `{field}`")  # pragma: no cover


def get_title_snapshot(title: str) -> dict:
    """Fetch the full current field values for a published title, by
    slug match on the given title text. Returns a dict with all stored
    fields plus "id" (the doc id), or None if no match. Used by /lookup
    (to show current values) and /deletetitle (to confirm what's about
    to be deleted).
    """
    db = _db or init_firebase()
    slug = slugify(title)
    if not slug:
        return None

    existing = find_series_by_slug(db, slug)
    if not existing:
        return None

    data = existing.to_dict() or {}
    data["id"] = existing.id
    return data


def update_title_fields(title: str, updates: dict) -> dict:
    """Apply a partial field update to an existing published title,
    found by slug match on `title`. `updates` must already be validated/
    coerced (see parse_edit_field) -- this function just writes them.

    Renaming via the `title` field is intentionally NOT supported here:
    changing title would also need to move the doc to a new slug (the
    site looks docs up by slug, see find_series_by_slug/middleware.js),
    and silently reslugging risks breaking any external links already
    pointing at the old slug. If a rename is genuinely needed, do it in
    admin.html where the slug-collision check (updateSlugPreview) is
    visible before saving.

    Returns {"found": False} if no matching title exists, or
            {"found": True, "doc_id": "...", "title": "..."}
    """
    db = _db or init_firebase()
    slug = slugify(title)
    if not slug:
        return {"found": False}

    existing = find_series_by_slug(db, slug)
    if not existing:
        return {"found": False}

    write_updates = dict(updates)
    if "title" in write_updates:
        # Reject rather than silently drop -- the admin explicitly asked
        # for this and deserves to know why it didn't happen, rather than
        # having every OTHER field in the same command applied while this
        # one vanishes with no trace.
        raise ValueError(
            "Renaming via `title=` isn't supported (it would also need to move "
            "the doc to a new slug, which risks breaking existing links). "
            "Rename it in admin.html instead, where the slug-collision check "
            "is visible before saving."
        )

    existing.reference.update(write_updates)
    data = existing.to_dict() or {}
    logger.info("Updated doc %s (title=%r) fields: %s", existing.id, title, list(write_updates))

    return {"found": True, "doc_id": existing.id, "title": data.get("title", title)}


def delete_title(doc_id: str) -> dict:
    """Permanently delete a movies/ doc by id. Mirrors admin.js's
    triggerAdminDelete (deleteDoc) exactly -- no soft-delete, no
    recycle bin, matching the site's own behavior.

    Takes a doc_id (not a title) since the caller (handle_delete_title in
    main.py) already resolved and confirmed the exact doc via
    get_title_snapshot before calling this -- looking up by title a
    second time here would re-open the door to a title-text race (e.g.
    two similarly-named titles) that the confirm step exists to close.

    Returns {"found": False} if the doc no longer exists (e.g. already
    deleted by a second concurrent request), or
            {"found": True, "title": "..."}
    """
    db = _db or init_firebase()
    doc_ref = db.collection(ROOT_COLLECTION).document(doc_id)
    snap = doc_ref.get()
    if not snap.exists:
        return {"found": False}

    data = snap.to_dict() or {}
    title = data.get("title", "Untitled")
    doc_ref.delete()
    logger.info("Deleted doc %s (title=%r)", doc_id, title)
    return {"found": True, "title": title}


def migrate_movie_category_typos(dry_run: bool = True) -> dict:
    """One-off cleanup for docs published by an earlier version of this
    bot that wrote category="movie" (singular) instead of the site's
    "movies" (plural) -- see the CATEGORY VALUES note at the top of this
    file. Run manually, not called anywhere in the bot's normal flow:

        python3 -c "import firestore_publish as f; f.init_firebase(); \
                     print(f.migrate_movie_category_typos(dry_run=True))"

    Review the dry-run output, then re-run with dry_run=False to actually
    apply the fix.

    Returns {"matched": [...doc ids/titles...], "updated": bool}
    """
    db = _db or init_firebase()
    query = db.collection(ROOT_COLLECTION).where("category", "==", "movie")
    matched = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        matched.append({"doc_id": doc.id, "title": data.get("title", "Untitled")})
        if not dry_run:
            doc.reference.update({"category": "movies"})

    if dry_run:
        logger.info("migrate_movie_category_typos: found %d doc(s) with category='movie' (dry run, nothing changed)",
                     len(matched))
    else:
        logger.info("migrate_movie_category_typos: updated %d doc(s) from category='movie' to 'movies'",
                     len(matched))

    return {"matched": matched, "updated": not dry_run}


# ── Sitemap generation ────────────────────────────────────────────────────
# Generates sitemap.xml from the live Firestore catalog, matching the URL
# shape js/slug.js's buildMovieUrl() produces client-side. robots.txt
# already points at /sitemap.xml (see repo root), but the file itself has
# always been empty -- there's no build step that populates it, since the
# catalog only exists in Firestore, not as static files. This is that
# missing generation step, triggered manually from the bot rather than
# baked into a Cloud Function (see /sitemap in main.py).
#
# SITE_ORIGIN is the one thing that can't be derived from Firestore or
# ported from the client-side code -- js/slug.js builds absolute URLs from
# `location.origin` at runtime in the browser, which has no equivalent on
# a server. Hardcoded here; update it if the production domain ever changes.
SITE_ORIGIN = "https://novaflix.com.ng"

# Static, non-catalog pages worth listing explicitly -- mirrors the site's
# own top-level nav (index.html links to these; see js/router.js's cases).
# Priority/changefreq follow standard sitemap.org conventions: the
# homepage and catalog listing pages change often and matter most; detail
# pages change less often per-page (mainly when a new episode/link is
# added) and site/legal pages barely change at all.
_STATIC_PAGES = [
    {"path": "/", "changefreq": "daily", "priority": "1.0"},
    {"path": "/movies.html", "changefreq": "daily", "priority": "0.9"},
    {"path": "/series.html", "changefreq": "daily", "priority": "0.9"},
    {"path": "/category.html", "changefreq": "weekly", "priority": "0.6"},
    {"path": "/about.html", "changefreq": "monthly", "priority": "0.3"},
    {"path": "/contact.html", "changefreq": "monthly", "priority": "0.3"},
]


def _xml_escape(text: str) -> str:
    """Minimal XML entity escaping for text going into sitemap.xml. Only
    the five characters XML actually requires escaping for -- no need for
    a full XML library just for this.
    """
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_movie_url_path(item: dict, slug_counts: dict) -> str:
    """Python port of buildMovieUrl() in js/slug.js, computing just the
    path (SITE_ORIGIN is prepended by the caller). slug_counts must be a
    dict of {slug: count_of_titles_producing_that_slug} across the WHOLE
    catalog -- see generate_sitemap_xml for why this can't be computed
    per-item in isolation. Falls back to "title" for a blank/unslugg-able
    title, matching the JS's `slugify(item.title) || "title"`.
    """
    slug = slugify(item.get("title", "")) or "title"
    collides = slug_counts.get(slug, 0) > 1
    if collides:
        return f"/movie/{slug}-{item['id']}"
    return f"/movie/{slug}"


def generate_sitemap_xml() -> str:
    """Build a complete sitemap.xml string from the current Firestore
    catalog plus the static top-level pages. Returns the raw XML text --
    callers decide what to do with it (main.py sends it to the admin as
    a file, since the bot has no direct way to push files into Firebase
    Hosting's static file tree -- see /sitemap's docstring in main.py).

    Collision detection (whether a title needs the "-<id>" suffix)
    mirrors buildMovieUrl() exactly: it's a catalog-wide comparison ("does
    ANY other title produce the same slug"), not a per-item check, so this
    fetches every doc's title once up front to compute slug counts before
    building any individual URL -- getting this wrong would silently
    produce a sitemap with duplicate/colliding URLs for any two titles
    that happen to slugify the same way (e.g. "Alice (2019)" and
    "Alice: 2019" both -> "alice-2019").
    """
    db = _db or init_firebase()

    docs = list(db.collection(ROOT_COLLECTION).select(
        ["title", "category", "timestamp"]
    ).stream())

    items = []
    slug_counts = {}
    for doc in docs:
        data = doc.to_dict() or {}
        title = data.get("title", "")
        slug = slugify(title) or "title"
        slug_counts[slug] = slug_counts.get(slug, 0) + 1
        items.append({
            "id": doc.id,
            "title": title,
            "category": data.get("category", ""),
            "timestamp": data.get("timestamp"),
        })

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')

    for page in _STATIC_PAGES:
        lines.append("  <url>")
        lines.append(f"    <loc>{_xml_escape(SITE_ORIGIN + page['path'])}</loc>")
        lines.append(f"    <changefreq>{page['changefreq']}</changefreq>")
        lines.append(f"    <priority>{page['priority']}</priority>")
        lines.append("  </url>")

    for item in items:
        path = _build_movie_url_path(item, slug_counts)
        lines.append("  <url>")
        lines.append(f"    <loc>{_xml_escape(SITE_ORIGIN + path)}</loc>")
        # lastmod, if the doc has a timestamp -- Firestore SERVER_TIMESTAMP
        # values come back as a datetime-like object with isoformat().
        ts = item.get("timestamp")
        if ts is not None and hasattr(ts, "isoformat"):
            lines.append(f"    <lastmod>{ts.isoformat()}</lastmod>")
        lines.append("    <changefreq>weekly</changefreq>")
        lines.append("    <priority>0.7</priority>")
        lines.append("  </url>")

    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


# ── Promos ────────────────────────────────────────────────────────────────
# Mirrors admin.html's Promo tab (see js/admin.js:
# executeAdminPromoUpload) -- a single site-wide banner image doc at
# banners/activePromo, shown globally when isActive is true. Simpler than
# announcements: just an image URL + destination link, both required by
# the web form, no title/tone/timestamp.

_PROMO_DOC = ("banners", "activePromo")


def get_promo() -> dict:
    """Fetch the current promo banner doc, or a dict of defaults (empty
    URLs, isActive False) if none has ever been set -- mirrors
    admin.html's load path (see the getDoc call around line 525).
    """
    db = _db or init_firebase()
    snap = db.collection(_PROMO_DOC[0]).document(_PROMO_DOC[1]).get()
    if not snap.exists:
        return {"imageUrl": "", "targetUrl": "", "isActive": False}
    data = snap.to_dict() or {}
    return {
        "imageUrl": data.get("imageUrl", ""),
        "targetUrl": data.get("targetUrl", ""),
        "isActive": bool(data.get("isActive", False)),
    }


def set_promo(image_url: str, target_url: str, is_active: bool = None) -> dict:
    """Write the promo banner doc. Full overwrite (setDoc, matching
    admin.html) -- both imageUrl and targetUrl are required, same as the
    web form's `required` attributes on both inputs.

    is_active, if None, PRESERVES the current doc's isActive value, same
    reasoning as set_announcement: a chat command has no checkbox to
    carry along, so re-running `/promo <img> <link>` to fix a typo
    shouldn't silently take a live banner down or bring a hidden one up.

    Returns {"ok": True, "isActive": bool}.
    """
    db = _db or init_firebase()

    image_url = (image_url or "").strip()
    target_url = (target_url or "").strip()
    if not image_url:
        raise ValueError("imageUrl can't be empty")
    if not target_url:
        raise ValueError("targetUrl can't be empty")

    if is_active is None:
        current = get_promo()
        is_active = current["isActive"]

    payload = {
        "imageUrl": image_url,
        "targetUrl": target_url,
        "isActive": bool(is_active),
    }
    db.collection(_PROMO_DOC[0]).document(_PROMO_DOC[1]).set(payload)
    logger.info("Promo banner updated (isActive=%s)", payload["isActive"])
    return {"ok": True, "isActive": payload["isActive"]}


def set_promo_active(is_active: bool) -> dict:
    """Flip just the isActive flag on the existing promo doc, without
    touching imageUrl/targetUrl -- for /promoon and /promooff.

    Raises ValueError if no promo has ever been created (nothing to
    toggle) -- same reasoning as set_announcement_active: turning on an
    empty banner with no image/link would be visibly broken to visitors.
    """
    db = _db or init_firebase()
    doc_ref = db.collection(_PROMO_DOC[0]).document(_PROMO_DOC[1])
    snap = doc_ref.get()
    if not snap.exists:
        raise ValueError("No promo banner has been set yet. Use `/promo <imageUrl> <targetUrl>` first.")

    doc_ref.update({"isActive": bool(is_active)})
    logger.info("Promo banner isActive set to %s", is_active)
    return {"ok": True, "isActive": bool(is_active)}



# Mirrors admin.html's Announcement tab (see js/admin.js:
# executeAdminAnnouncementUpload) -- a single site-wide banner doc at
# announcements/siteAnnouncement, shown in a dismissible bar at the top
# of every page when isActive is true. Unlike catalog editing, this is a
# single fixed doc (setDoc, full overwrite), not a partial-update-by-id
# lookup, so there's no separate "find the doc" step.

ANNOUNCEMENT_TONES = ("info", "success", "warning", "alert")
_ANNOUNCEMENT_DOC = ("announcements", "siteAnnouncement")


def get_announcement() -> dict:
    """Fetch the current site announcement doc, or a dict of defaults
    (all empty, isActive False) if none has ever been set -- mirrors
    admin.html's load path (see the getDoc call around line 543), which
    leaves the form blank rather than erroring when the doc doesn't
    exist yet.
    """
    db = _db or init_firebase()
    snap = db.collection(_ANNOUNCEMENT_DOC[0]).document(_ANNOUNCEMENT_DOC[1]).get()
    if not snap.exists:
        return {"title": "", "message": "", "tone": "info", "linkUrl": "", "linkLabel": "", "isActive": False}
    data = snap.to_dict() or {}
    return {
        "title": data.get("title", ""),
        "message": data.get("message", ""),
        "tone": data.get("tone", "info"),
        "linkUrl": data.get("linkUrl", ""),
        "linkLabel": data.get("linkLabel", ""),
        "isActive": bool(data.get("isActive", False)),
    }


def set_announcement(message: str, title: str = "", tone: str = "info",
                      link_url: str = "", link_label: str = "",
                      is_active: bool = None) -> dict:
    """Write the site announcement doc. Full overwrite (setDoc, matching
    admin.html), not a partial update -- every field is written every
    time, since the announcement is one small form, not a large record
    where a partial patch is worth supporting.

    message is required (mirrors the form's `required` attribute + 220
    char maxlength) and truncated to 220 chars rather than rejected
    outright for a slight overage, since a chat message is easy to paste
    a little long by accident. title is capped at 40 chars, link_label
    at 30, matching the form's maxlength attributes.

    is_active, if None, PRESERVES whatever the current doc's isActive
    value is (so `/announce <message>` alone doesn't silently flip a
    currently-live announcement off, or a currently-hidden one on) --
    this is the one place this function's behavior deliberately departs
    from admin.html's plain setDoc, since the web form always has an
    explicit checkbox state to submit and a chat command doesn't unless
    the admin says so.

    Returns {"ok": True, "isActive": bool}.
    """
    db = _db or init_firebase()

    tone = (tone or "info").strip().lower()
    if tone not in ANNOUNCEMENT_TONES:
        raise ValueError(f"tone must be one of {', '.join(ANNOUNCEMENT_TONES)}, got {tone!r}")

    message = (message or "").strip()[:220]
    if not message:
        raise ValueError("message can't be empty")

    if is_active is None:
        current = get_announcement()
        is_active = current["isActive"]

    payload = {
        "title": (title or "").strip()[:40],
        "message": message,
        "tone": tone,
        "linkUrl": (link_url or "").strip(),
        "linkLabel": (link_label or "").strip()[:30],
        "isActive": bool(is_active),
        "updatedAt": _firestore.SERVER_TIMESTAMP,
    }
    db.collection(_ANNOUNCEMENT_DOC[0]).document(_ANNOUNCEMENT_DOC[1]).set(payload)
    logger.info("Announcement updated (isActive=%s, tone=%s)", payload["isActive"], tone)
    return {"ok": True, "isActive": payload["isActive"]}


def set_announcement_active(is_active: bool) -> dict:
    """Flip just the isActive flag on the existing announcement doc,
    without touching message/title/tone/link -- for /announceon and
    /announceoff, which shouldn't require re-typing the whole message
    just to toggle visibility.

    Uses `update` (partial), not `set` (full overwrite) -- unlike
    set_announcement above, which always rewrites every field to match
    admin.html's form-submit behavior, this is specifically for the
    "just flip the switch" case.

    Raises ValueError if no announcement has ever been created (nothing
    to toggle) -- mirrors the fact that /announceon before any /announce
    would otherwise show an empty banner to every visitor, which is
    almost certainly not what was intended.
    """
    db = _db or init_firebase()
    doc_ref = db.collection(_ANNOUNCEMENT_DOC[0]).document(_ANNOUNCEMENT_DOC[1])
    snap = doc_ref.get()
    if not snap.exists:
        raise ValueError("No announcement has been set yet. Use `/announce <message>` first.")

    doc_ref.update({"isActive": bool(is_active), "updatedAt": _firestore.SERVER_TIMESTAMP})
    logger.info("Announcement isActive set to %s", is_active)
    return {"ok": True, "isActive": bool(is_active)}





def fetch_unprocessed_torrent_uploads(limit: int = 10) -> list:
    """Poll for finished torrent uploads the Torrent Fetcher bot has
    queued (see that bot's firestore_handoff.py). This replaces the
    original design where Torrent Fetcher posted a message to a shared
    Telegram group for Media Router to react to -- that approach turned
    out to be unreliable for reasons never fully pinned down (every
    individually-checkable cause -- group membership, Privacy Mode,
    peer resolution, dispatch priority -- came back fine, and the
    message still silently never reached an on_message handler).
    Firestore polling sidesteps the whole Telegram-to-Telegram messaging
    problem by using a data store both bots already reliably talk to.

    Returns a list of dicts: [{"doc_id", "doodstreamUrl", "originalFilename",
    "requestedByChatId"}, ...], oldest first. Does NOT mark them
    processed -- call mark_torrent_upload_processed() after successfully
    handling each one, so a crash between fetch and processing doesn't
    silently lose the upload (it'll just be picked up again next poll).
    """
    db = _db or init_firebase()

    query = (
        db.collection(PENDING_TORRENT_COLLECTION)
        .where("processed", "==", False)
        .limit(limit)
    )
    results = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        results.append({
            "doc_id": doc.id,
            "doodstreamUrl": data.get("doodstreamUrl", ""),
            "originalFilename": data.get("originalFilename", ""),
            "requestedByChatId": data.get("requestedByChatId"),
        })
    return results


def mark_torrent_upload_processed(doc_id: str):
    """Mark a pending_torrent_uploads doc as handled so it isn't picked
    up again on the next poll. Doesn't delete it -- keeping a processed
    record around is cheap and useful for debugging what's come through,
    unlike leaving it in the "unprocessed" state which would cause
    reprocessing loops.
    """
    db = _db or init_firebase()
    db.collection(PENDING_TORRENT_COLLECTION).document(doc_id).update({
        "processed": True,
        "processedAt": _firestore.SERVER_TIMESTAMP,
    })