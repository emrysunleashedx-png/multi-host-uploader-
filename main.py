import os
import logging
import asyncio
import httpx
from pyrogram import Client, filters
from pyrogram.types import Message

import firestore_publish
import tmdb_fetch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("multi_uploader_bot")

# Environment variables
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Per-hoster config: add new hosters here without touching upload logic.
HOSTERS = {
    "Doodstream": {
        "api_key": os.getenv("DOODSTREAM_API_KEY", ""),
        "server_url": "https://doodapi.com/api/upload/server",
        "download_url_fmt": "https://dood.to/d/{code}",
    },
}

# Upload network timeout. None (no timeout) risks the bot hanging forever
# on a stalled connection; use generous but finite limits instead.
UPLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)

# In-memory pending-publish state, keyed by chat_id. Holds the parsed guess
# plus the finished Doodstream URL while waiting for the admin to /confirm
# or /edit it. This is intentionally simple (no persistence) -- if the bot
# restarts mid-confirmation the admin just re-sends /confirm and gets told
# there's nothing pending; the uploaded file itself is not lost since it's
# already on Doodstream by this point, only the Firestore publish step
# needs re-triggering (which would mean re-running with the link manually
# added via admin.html, since the pending state doesn't survive a restart).
PENDING_PUBLISHES = {}

# Holds the original Message object for a file that triggered a duplicate
# warning, keyed by chat_id, so /forceupload can re-run the real upload
# flow on it without the admin having to re-forward the file. Cleared once
# /forceupload is used or a new file comes in (only the most recent
# duplicate warning can be force-uploaded).
PENDING_FORCE_UPLOAD = {}

app = Client("multi_uploader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _extract_filecode_from_entry(entry):
    """Pull a filecode out of a single dict entry, whichever key it uses."""
    if isinstance(entry, dict):
        return entry.get("filecode") or entry.get("file_code")
    return None


def _extract_filecode(upload_data):
    """Extract the filecode from a Doodstream upload response.

    Doodstream returns the filecode under top-level "result", as either a
    list of dicts or a single dict, using either "filecode" or "file_code"
    as the key depending on server instance.
    """
    result = upload_data.get("result")
    if isinstance(result, list) and result:
        result = result[0]
    return _extract_filecode_from_entry(result)


async def upload_to_hoster(client: httpx.AsyncClient, hoster_name: str, file_path: str,
                            max_retries: int = 2) -> str:
    """Generic uploader used for any hoster following the doodapi-style
    upload-server protocol (fetch server -> POST file -> parse filecode).

    Retries transient failures (timeouts, connection errors) up to
    max_retries times with a short backoff, since these are the failure
    modes most likely to succeed on a second attempt (a dropped connection
    or a momentarily slow server). Permanent failures -- missing API key,
    a malformed response, an explicit error message from the hoster -- are
    NOT retried, since retrying those would just waste time and bandwidth
    on something that won't change.
    """
    config = HOSTERS[hoster_name]
    api_key = config["api_key"]

    if not api_key:
        return "Key Missing ⚠️"

    filename = os.path.basename(file_path)
    last_transient_error = None

    for attempt in range(1, max_retries + 2):  # e.g. max_retries=2 -> attempts 1,2,3
        if attempt > 1:
            backoff = 2 ** (attempt - 1)  # 2s, 4s, ...
            logger.info("%s: retrying upload (attempt %d/%d) after %ds backoff",
                        hoster_name, attempt, max_retries + 1, backoff)
            await asyncio.sleep(backoff)

        result = await _attempt_upload(client, hoster_name, config, api_key, filename, file_path)

        if result.startswith("http"):
            return result

        if result.startswith("TRANSIENT:"):
            last_transient_error = result[len("TRANSIENT:"):]
            continue  # try again

        # Permanent failure (bad key, malformed response, explicit hoster
        # error) -- no point retrying, return immediately.
        return result

    # Exhausted all retries on transient errors only.
    return (f"Error: upload failed after {max_retries + 1} attempts "
            f"(last error: {last_transient_error})")


async def _attempt_upload(client: httpx.AsyncClient, hoster_name: str, config: dict,
                           api_key: str, filename: str, file_path: str) -> str:
    """Single upload attempt. Returns either the finished URL, a string
    starting with "TRANSIENT:" for retryable errors, or any other string
    for permanent/non-retryable failures."""

    try:
        server_resp = await client.get(
            config["server_url"],
            params={"key": api_key},
            headers=HEADERS,
            timeout=UPLOAD_TIMEOUT,
        )
    except httpx.TimeoutException:
        logger.warning("%s: timed out fetching upload server", hoster_name)
        return "TRANSIENT:request to fetch upload server timed out"
    except httpx.HTTPError as e:
        logger.warning("%s: network error fetching upload server: %s", hoster_name, e)
        return f"TRANSIENT:network issue contacting {hoster_name} ({type(e).__name__})"

    try:
        server_data = server_resp.json()
    except ValueError:
        logger.warning("%s: non-JSON server response (HTTP %s): %r",
                        hoster_name, server_resp.status_code, server_resp.text[:200])
        # A non-JSON response with a 5xx status usually means the hoster's
        # backend is down/overloaded -- worth a retry. A non-JSON response
        # with a 2xx/4xx status is more likely a permanent API contract
        # issue (wrong endpoint, bad key format) -- not worth retrying.
        if server_resp.status_code >= 500:
            return f"TRANSIENT:server error (HTTP {server_resp.status_code})"
        return f"API Error (HTTP {server_resp.status_code})"

    upload_url = server_data.get("result")
    if not upload_url:
        msg = server_data.get("msg", "No Upload URL")
        # Some hosters return a "server busy"/"try again" style message
        # here rather than an HTTP error code -- treat those as transient.
        if isinstance(msg, str) and any(w in msg.lower() for w in ("busy", "try again", "unavailable", "overload")):
            return f"TRANSIENT:server reported: {msg}"
        return f"Server Error: {msg}"

    # Doodstream's API expects the form field name "api_key".
    extra_data = {"api_key": api_key}

    try:
        # Reading the file happens inside a worker thread via to_thread so we
        # never block the event loop on disk I/O for large files.
        async def _post():
            with open(file_path, "rb") as f:
                files = {"file": (filename, f, "application/octet-stream")}
                return await client.post(
                    upload_url,
                    files=files,
                    data=extra_data,
                    headers=HEADERS,
                    timeout=UPLOAD_TIMEOUT,
                )

        upload_resp = await _post()
    except httpx.TimeoutException:
        logger.warning("%s: upload timed out for %s", hoster_name, filename)
        return "TRANSIENT:upload timed out"
    except httpx.HTTPError as e:
        logger.warning("%s: network error during upload: %s", hoster_name, e)
        return f"TRANSIENT:network issue during upload ({type(e).__name__})"
    except OSError as e:
        # The local file disappeared or became unreadable -- retrying won't
        # help since the source file itself is the problem, not the network.
        logger.error("%s: could not read local file %s: %s", hoster_name, file_path, e)
        return "Error: could not read downloaded file"

    try:
        upload_data = upload_resp.json()
    except ValueError:
        logger.warning("%s: invalid upload response: %r", hoster_name, upload_resp.text[:200])
        if upload_resp.status_code >= 500:
            return f"TRANSIENT:server error during upload (HTTP {upload_resp.status_code})"
        return f"Response Parse Error: {upload_resp.text[:100]}"

    file_code = _extract_filecode(upload_data)
    if file_code:
        return config["download_url_fmt"].format(code=file_code)

    # upload_data.get('msg') can be a reassuring "OK" even when result/files
    # doesn't actually contain a usable filecode, so surface the raw payload
    # rather than that misleading top-level message. Not retried -- a
    # missing filecode with an "OK" status is a data-shape issue, not a
    # transient failure that a retry would fix.
    logger.warning("%s: upload failed, response=%r", hoster_name, upload_data)
    return f"Upload Failed: no filecode in response ({upload_data})"


@app.on_message(filters.private & (filters.video | filters.document))
async def handle_media(client: Client, message: Message):
    # Parse title/episode from caption/filename BEFORE downloading anything,
    # so an already-published episode can be caught without spending any
    # Telegram download time or Doodstream upload bandwidth on it.
    filename = getattr(message.document, "file_name", None) or \
               getattr(message.video, "file_name", None) or ""
    source_text = (message.caption or "").strip() or filename
    guess_title, guess_episode = firestore_publish.parse_title_and_episode(source_text)

    if guess_title:
        try:
            dup_check = await asyncio.to_thread(
                firestore_publish.check_existing_episode, guess_title, guess_episode
            )
        except Exception as e:
            # If the duplicate check itself fails (e.g. Firebase not
            # configured), don't block the upload over it -- just log and
            # proceed as if nothing was found. Losing the dedup check for
            # one upload is much better than losing the ability to upload
            # at all because of an unrelated Firestore hiccup.
            logger.warning("Duplicate check failed, proceeding anyway: %s", e)
            dup_check = {"exists": False}

        if dup_check.get("exists"):
            episode_note = f" ({dup_check['episode']})" if dup_check.get("episode") else ""
            await message.reply_text(
                f"⚠️ **Possible duplicate detected**{episode_note} for `{dup_check['title']}` "
                "-- it looks like this is already published on the site.\n\n"
                "Skipping download/upload to save bandwidth. If this is wrong "
                "(e.g. a different cut, re-upload, or the title guess is off), "
                "reply `/forceupload` within the next few minutes to upload anyway."
            )
            # Stash enough info for /forceupload to re-trigger the real
            # handler without re-parsing everything from scratch.
            PENDING_FORCE_UPLOAD[message.chat.id] = message
            return

    await _download_and_upload(client, message, guess_title, guess_episode, source_text)


@app.on_message(filters.private & filters.command("forceupload"))
async def handle_force_upload(client: Client, message: Message):
    original_message = PENDING_FORCE_UPLOAD.pop(message.chat.id, None)
    if not original_message:
        await message.reply_text(
            "Nothing to force-upload. This only works right after a "
            "duplicate warning, for that same file."
        )
        return

    filename = getattr(original_message.document, "file_name", None) or \
               getattr(original_message.video, "file_name", None) or ""
    source_text = (original_message.caption or "").strip() or filename
    guess_title, guess_episode = firestore_publish.parse_title_and_episode(source_text)

    await message.reply_text("Proceeding with download/upload despite the duplicate warning...")
    await _download_and_upload(client, original_message, guess_title, guess_episode, source_text)


async def _download_and_upload(client: Client, message: Message, guess_title: str,
                                guess_episode: str, source_text: str):
    status_msg = await message.reply_text("📥 Downloading file from Telegram...")

    try:
        # Pyrogram's download() is already async-native (runs its own I/O off
        # the main loop internally), so no to_thread wrapper needed here.
        file_path = await message.download()
    except Exception as e:
        logger.error("Download failed: %s", e)
        await status_msg.edit_text("❌ Download failed.")
        return

    if not file_path or not os.path.exists(file_path):
        await status_msg.edit_text("❌ Download failed.")
        return

    try:
        await status_msg.edit_text("🚀 Uploading to Doodstream...")

        async with httpx.AsyncClient(follow_redirects=True) as http_client:
            dood_url = await upload_to_hoster(http_client, "Doodstream", file_path)

        # If the upload itself failed, there's nothing to publish -- just
        # report the failure and stop (no point asking the admin to
        # confirm a title for a link that doesn't exist). Distinguish
        # outage-style failures (worth telling the admin plainly that
        # it's Doodstream's side, not theirs) from other failure types.
        if not dood_url.startswith("http"):
            await status_msg.edit_text(_format_upload_failure(dood_url))
            return

        logger.info("Parsing title/episode from source_text=%r (caption=%r, filename=%r)",
                    source_text, message.caption, getattr(message.document, "file_name", None))

        PENDING_PUBLISHES[message.chat.id] = {
            "title": guess_title,
            "episode": guess_episode,
            "url": dood_url,
            "year": None,
            "category": "series" if guess_episode else "movie",
            "tmdb_candidates": None,  # populated once /confirm triggers a TMDB search
            "tmdb_type": None,
        }

        title_line = guess_title or "(couldn't guess a title)"
        episode_line = guess_episode or "(none detected)"
        category_line = PENDING_PUBLISHES[message.chat.id]["category"]
        response = (
            "✅ **Upload complete!**\n\n"
            f"🍿 **Doodstream:** {dood_url}\n\n"
            "**Ready to publish to the site.** Best guess from the caption:\n"
            f"• Title: `{title_line}`\n"
            f"• Episode: `{episode_line}`\n"
            f"• Category: `{category_line}` (guessed from whether an episode was detected)\n\n"
            "Reply `/confirm` to look this up on TMDB and publish, or "
            "`/edit <title> | <episode> | <year> | <category>` to correct it first.\n"
            "All parts after title are optional, e.g.:\n"
            "`/edit The Scarecrow | S01E02`\n"
            "`/edit Parasite | | 2019 | movie`"
        )
        await status_msg.edit_text(response)

    except Exception as e:
        logger.exception("Unexpected error while processing %s", file_path)
        await status_msg.edit_text(f"❌ Unexpected error: {type(e).__name__}")

    finally:
        # Always clean up the downloaded file, even if uploads raised.
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning("Failed to remove temp file %s: %s", file_path, e)


def _format_upload_failure(error_text: str) -> str:
    """Give the admin a plain-language read on WHY an upload failed,
    distinguishing 'Doodstream itself is having problems' (nothing you can
    do but wait/retry later) from other failure categories, rather than
    always showing the same generic wall of text.
    """
    lower = error_text.lower()
    # Only classify as an outage/slowdown if it's specifically one of the
    # TRANSIENT-tagged failure phrasings that made it through all retries
    # (timeouts, network errors, 5xx responses) -- checking for "attempts"
    # anchors this to the retry-exhaustion message specifically, rather
    # than matching any string that happens to contain "server error"
    # (e.g. a permanent "Server Error: quota exceeded" message, which is
    # a config/account issue, not an outage).
    is_retry_exhaustion = "upload failed after" in lower and "attempts" in lower
    if is_retry_exhaustion:
        return (
            f"❌ **Upload failed — looks like a Doodstream outage or slowdown**, not a problem with your file.\n\n"
            f"Details: {error_text}\n\n"
            "This was retried automatically and still failed. Worth trying again "
            "in a few minutes, or checking Doodstream's status if it keeps happening."
        )
    if "key missing" in lower:
        return f"❌ **Doodstream API key isn't configured** on the bot. Details: {error_text}"
    return f"❌ Upload failed: {error_text}"


@app.on_message(filters.private & filters.command(["status", "pending"]))
async def handle_status(client: Client, message: Message):
    pending = PENDING_PUBLISHES.get(message.chat.id)
    if not pending:
        await message.reply_text("Nothing pending right now. Send a video/file to get started.")
        return

    title_line = pending.get("title") or "(couldn't guess a title)"
    episode_line = pending.get("episode") or "(none)"
    year_line = pending.get("year") or "(none)"
    category_line = pending.get("category", "series")

    lines = [
        "📋 **Pending upload:**",
        f"• Title: `{title_line}`",
        f"• Episode: `{episode_line}`",
        f"• Year: `{year_line}`",
        f"• Category: `{category_line}`",
        f"• Doodstream link: {pending.get('url', '(none)')}",
    ]

    if pending.get("tmdb_candidates"):
        lines.append("")
        lines.append(f"⏳ Waiting on `/pick <number>` — {len(pending['tmdb_candidates'])} TMDB matches shown above.")
    else:
        lines.append("")
        lines.append("Reply `/confirm` to publish, or `/edit ...` to correct it first.")

    await message.reply_text("\n".join(lines))


@app.on_message(filters.private & filters.command("find"))
async def handle_find(client: Client, message: Message):
    raw = message.text.split(None, 1)
    query = raw[1].strip() if len(raw) > 1 else ""
    if not query:
        await message.reply_text("Usage: `/find <title>` -- e.g. `/find Scarecrow`")
        return

    status = await message.reply_text(f"🔎 Searching the site for \"{query}\"...")
    try:
        results = await asyncio.to_thread(firestore_publish.search_titles, query)
    except Exception as e:
        logger.exception("Title search failed")
        await status.edit_text(f"❌ Search failed: {type(e).__name__}: {e}")
        return

    if not results:
        await status.edit_text(f"No published titles matching \"{query}\" found.")
        return

    lines = [f"Found {len(results)} match(es) for \"{query}\":", ""]
    for r in results:
        ep_note = f"{r['episode_count']} episode(s)" if r["category"] == "series" else "movie"
        lines.append(f"• **{r['title']}** ({ep_note})")

    await status.edit_text("\n".join(lines))


@app.on_message(filters.private & filters.command("help"))
async def handle_help(client: Client, message: Message):
    await message.reply_text(
        "**NovaFlix Media Router — commands**\n\n"
        "Send a video or document to start an upload. After it finishes:\n"
        "• `/confirm` — look the guessed title up on TMDB and publish\n"
        "• `/edit <title> | <episode> | <year> | <category>` — correct the guess first "
        "(only title is required, e.g. `/edit Parasite | | 2019 | movie`)\n"
        "• `/pick <number>` — choose from a TMDB shortlist if multiple matches came up\n\n"
        "Other commands:\n"
        "• `/status` or `/pending` — see what's currently awaiting confirmation\n"
        "• `/find <title>` — check if something's already published on the site\n"
        "• `/forceupload` — upload anyway after a duplicate warning\n"
        "• `/help` — this message"
    )


@app.on_message(filters.private & filters.command("confirm"))
async def handle_confirm(client: Client, message: Message):
    pending = PENDING_PUBLISHES.get(message.chat.id)
    if not pending:
        await message.reply_text("Nothing pending to confirm. Upload a file first.")
        return

    if not pending["title"]:
        await message.reply_text(
            "No title could be guessed for this upload, so it can't be confirmed "
            "as-is. Use `/edit <title> | <episode>` instead."
        )
        return

    # If a TMDB shortlist was already shown and the admin just says /confirm
    # again without picking, treat it as "publish without TMDB data" -- e.g.
    # for titles with no TMDB match, or if they just want the plain link up.
    if pending.get("tmdb_candidates") is not None:
        await _do_publish(message, pending, extra_fields=None)
        PENDING_PUBLISHES.pop(message.chat.id, None)
        return

    status = await message.reply_text(f"🔎 Searching TMDB for \"{pending['title']}\"...")
    try:
        search_result = await tmdb_fetch.search_tmdb(pending["title"], pending["category"])
    except Exception as e:
        logger.exception("TMDB search failed")
        await status.edit_text(
            f"⚠️ TMDB search failed ({type(e).__name__}). Publishing without TMDB data instead."
        )
        await _do_publish(message, pending, extra_fields=None)
        PENDING_PUBLISHES.pop(message.chat.id, None)
        return

    matches = search_result["matches"]
    tmdb_type = search_result["tmdb_type"]

    if not matches:
        await status.edit_text(
            f"No TMDB matches found for \"{pending['title']}\". Publishing without TMDB data.\n"
            "(It may be filed under an alternate/original title -- you can always fill in "
            "poster/synopsis/etc. by hand in admin.html afterwards.)"
        )
        await _do_publish(message, pending, extra_fields=None)
        PENDING_PUBLISHES.pop(message.chat.id, None)
        return

    ranked = tmdb_fetch.rank_matches_by_year(matches, pending.get("year"))

    if len(ranked) == 1:
        # Only one candidate -- fetch its details and publish directly,
        # no need to make the admin pick from a list of one.
        await status.edit_text("Found 1 match on TMDB — fetching details...")
        await _fetch_and_publish(message, pending, ranked[0], tmdb_type)
        PENDING_PUBLISHES.pop(message.chat.id, None)
        return

    # Multiple candidates -- show a shortlist and wait for /pick N.
    pending["tmdb_candidates"] = ranked
    pending["tmdb_type"] = tmdb_type
    PENDING_PUBLISHES[message.chat.id] = pending

    lines = [tmdb_fetch.format_candidate_line(m, i + 1) for i, m in enumerate(ranked)]
    year_note = f" (ranked using year hint {pending['year']})" if pending.get("year") else ""
    await status.edit_text(
        f"Found {len(ranked)} possible matches{year_note}:\n\n"
        + "\n".join(lines)
        + "\n\nReply `/pick <number>` to choose one, or `/confirm` again to "
          "publish without TMDB data."
    )


@app.on_message(filters.private & filters.command("pick"))
async def handle_pick(client: Client, message: Message):
    pending = PENDING_PUBLISHES.get(message.chat.id)
    if not pending or not pending.get("tmdb_candidates"):
        await message.reply_text("Nothing to pick from. Use `/confirm` first to search TMDB.")
        return

    raw = message.text.split(None, 1)
    arg = raw[1].strip() if len(raw) > 1 else ""
    if not arg.isdigit():
        await message.reply_text("Usage: `/pick <number>` -- e.g. `/pick 2`")
        return

    index = int(arg) - 1
    candidates = pending["tmdb_candidates"]
    if index < 0 or index >= len(candidates):
        await message.reply_text(f"Pick a number between 1 and {len(candidates)}.")
        return

    status = await message.reply_text("Fetching details from TMDB...")
    await _fetch_and_publish(message, pending, candidates[index], pending["tmdb_type"], status=status)
    PENDING_PUBLISHES.pop(message.chat.id, None)


async def _fetch_and_publish(message: Message, pending: dict, candidate: dict,
                              tmdb_type: str, status: Message = None):
    try:
        tmdb_id = candidate["id"]
        details = await tmdb_fetch.fetch_tmdb_details(tmdb_id, tmdb_type)
    except Exception as e:
        logger.exception("TMDB detail fetch failed")
        msg = f"⚠️ Couldn't fetch TMDB details ({type(e).__name__}). Publishing without TMDB data."
        if status:
            await status.edit_text(msg)
        else:
            await message.reply_text(msg)
        await _do_publish(message, pending, extra_fields=None)
        return

    await _do_publish(message, pending, extra_fields=details)


@app.on_message(filters.private & filters.command("edit"))
async def handle_edit(client: Client, message: Message):
    pending = PENDING_PUBLISHES.get(message.chat.id)
    if not pending:
        await message.reply_text("Nothing pending to edit. Upload a file first.")
        return

    # Usage: /edit <title> | <episode> | <year> | <category>
    # Every part after title is optional; leave a segment blank to keep
    # whatever's already set (e.g. "/edit The Scarecrow | | 2023" keeps
    # the existing episode and just sets the year).
    raw = message.text.split(None, 1)
    args = raw[1] if len(raw) > 1 else ""
    if not args.strip():
        await message.reply_text(
            "Usage: `/edit <title> | <episode> | <year> | <category>`\n"
            "Only title is required. Examples:\n"
            "`/edit The Scarecrow | S01E02`\n"
            "`/edit Parasite | | 2019 | movie`"
        )
        return

    parts = [p.strip() for p in args.split("|")]
    title = parts[0]
    episode = parts[1] if len(parts) > 1 and parts[1] else pending.get("episode")
    year = parts[2] if len(parts) > 2 and parts[2] else pending.get("year")
    category = parts[3] if len(parts) > 3 and parts[3] else pending.get("category")

    if not title:
        await message.reply_text("Title can't be empty.")
        return

    if category and category not in ("movie", "series"):
        await message.reply_text("Category must be `movie` or `series`.")
        return

    # A movie has no episode by definition -- if the admin explicitly sets
    # category to "movie" (whether just now or previously), don't let a
    # stale episode value from an earlier /edit or the caption guess leak
    # through just because this /edit call happened to leave that segment
    # blank.
    final_category = category or "series"
    if final_category == "movie":
        episode = None

    pending["title"] = title
    pending["episode"] = episode
    pending["year"] = year
    pending["category"] = final_category
    # Any manual edit invalidates a previous TMDB search/shortlist.
    pending["tmdb_candidates"] = None
    pending["tmdb_type"] = None
    PENDING_PUBLISHES[message.chat.id] = pending

    await message.reply_text(
        f"Updated. Title: `{title}` | Episode: `{episode or '(none)'}` | "
        f"Year: `{year or '(none)'}` | Category: `{pending['category']}`\n"
        "Reply `/confirm` to look this up on TMDB and publish, or `/edit` again to change it further."
    )


async def _do_publish(message: Message, pending: dict, extra_fields: dict = None):
    status = await message.reply_text("📡 Publishing to the site...")
    try:
        # firestore_publish does blocking network calls (google-cloud-firestore
        # is sync), so run it in a thread to avoid stalling the event loop.
        result = await asyncio.to_thread(
            firestore_publish.publish_doodstream_link,
            pending["title"],
            pending["episode"],
            pending["url"],
            "Doodstream",
            pending.get("category", "series"),
            extra_fields,
        )
        action = result["action"]
        if action == "created":
            tmdb_note = " (with TMDB data)" if extra_fields else ""
            text = f"✅ Published as a **new** entry{tmdb_note}: `{pending['title']}`"
        elif action == "appended":
            text = f"✅ Added **{pending['episode'] or 'link'}** to existing entry: `{pending['title']}`"
        else:
            text = f"ℹ️ This link was already published for `{pending['title']}` — skipped duplicate."
        await status.edit_text(text)
    except Exception as e:
        logger.exception("Failed to publish to Firestore")
        await status.edit_text(
            f"❌ Publish failed: {type(e).__name__}: {e}\n\n"
            "The Doodstream upload itself is fine -- you can add it manually "
            "in admin.html using the link above."
        )


if __name__ == "__main__":
    import http.server
    import socketserver
    import threading

    # Fail fast and loud at startup if Firebase isn't configured, rather
    # than only discovering it when the first admin tries to /confirm.
    try:
        firestore_publish.init_firebase()
    except Exception as e:
        logger.error("Firebase init failed: %s", e)
        logger.error("The bot will still run and uploads/Doodstream will "
                      "still work, but /confirm and /edit will fail until "
                      "FIREBASE_SERVICE_ACCOUNT_PATH is fixed.")

    def run_dummy_server():
        # Some hosting platforms (e.g. Render, Railway) require a bound port
        # to consider the service "healthy" even for non-HTTP bots like this one.
        port = int(os.getenv("PORT", "8000"))
        with socketserver.TCPServer(("", port), http.server.SimpleHTTPRequestHandler) as httpd:
            httpd.serve_forever()

    threading.Thread(target=run_dummy_server, daemon=True).start()
    app.run()

