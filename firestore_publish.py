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


def find_series_by_slug(db, slug: str):
    """Look up an existing movies/ doc by its stored slug field."""
    query = db.collection(ROOT_COLLECTION).where("slug", "==", slug).limit(1)
    results = list(query.stream())
    return results[0] if results else None


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


def publish_doodstream_link(title: str, episode: str, doodstream_url: str,
                             server_label: str = "Doodstream",
                             category: str = "series",
                             extra_fields: dict = None) -> dict:
    """Create-or-append the finished Doodstream link into Firestore.

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
        doc_ref.update({"directLinks": links})
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
        "p1080": False,
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

    _, doc_ref = db.collection(ROOT_COLLECTION).add(payload)
    logger.info("Created new doc %s for title=%r (slug=%s)", doc_ref.id, title, slug)
    return {"action": "created", "doc_id": doc_ref.id, "slug": slug}
