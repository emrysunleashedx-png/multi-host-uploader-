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
        match = _EPISODE_RE.match((link.get("episode") or "").strip())
        if not match:
            continue
        season_num, episode_num = int(match.group(1)), int(match.group(2))
        if best_season is None or (season_num, episode_num) > (best_season, best_episode):
            best_season, best_episode = season_num, episode_num

    if best_season is None:
        return ""

    return f"S{best_season:02d}E{best_episode + 1:02d}"





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

    for link in links:
        if link.get("episode") == episode:
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
    link.streamtapeStatus directly without an existence check."""
    return {
        "episode": episode,
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
        S01E01
        Download link: https://dood.to/d/abc
        Download link: https://dood.to/d/xyz   (no repeated episode line
                                                  if it matches the previous row)
    Rows without a URL are skipped, matching the JS's `if (!url) return;`.

    The label is always "Download link" rather than the hoster's actual
    name (e.g. "Doodstream") -- this is a display choice for the site's
    synopsis text, independent of the `server` field itself, which still
    stores the real hoster name for any other logic that needs it.
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
                             quality: str = "") -> dict:
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
        # (e.g. bot restart re-handling an already-uploaded file).
        already_present = any(
            l.get("sourceUrl") == doodstream_url or
            (episode and l.get("episode") == episode and l.get("server") == server_label)
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
            "voteAverage", "cast", "language",
        }
        for key, value in extra_fields.items():
            if key in allowed_keys and value not in (None, "", []):
                payload[key] = value

    # Build the initial description exactly like admin.html's "Generate
    # Synopsis" button would: TMDB synopsis (if any) followed by the
    # formatted episode/link block for this first upload.
    payload["description"] = build_full_description(payload.get("description", ""), payload["directLinks"])

    _, doc_ref = db.collection(ROOT_COLLECTION).add(payload)
    logger.info("Created new doc %s for title=%r (slug=%s)", doc_ref.id, title, slug)
    return {"action": "created", "doc_id": doc_ref.id, "slug": slug}


PENDING_TORRENT_COLLECTION = "pending_torrent_uploads"


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
