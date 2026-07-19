import os
import re
import logging
import asyncio
import httpx
from pyrogram import Client, filters, idle
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

# Streamtape uses a different auth scheme (login+key pair, not a single
# API key) and a different upload protocol than the Doodstream-style
# hosters above, so it's handled by its own dedicated function
# (upload_to_streamtape) rather than folded into HOSTERS/upload_to_hoster.
STREAMTAPE_LOGIN = os.getenv("STREAMTAPE_LOGIN", "")
STREAMTAPE_KEY = os.getenv("STREAMTAPE_KEY", "")

# Upload network timeout. None (no timeout) risks the bot hanging forever
# on a stalled connection; use generous but finite limits instead.
UPLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)

# Warn (don't hard-block) on files at or above this size, since large
# files mean real bandwidth cost both downloading from Telegram and
# uploading to Doodstream -- worth surfacing given Render's per-GB
# bandwidth billing beyond the free plan's included amount. Override via
# the LARGE_FILE_WARNING_GB env var if this default doesn't fit your usage.
LARGE_FILE_WARNING_BYTES = int(float(os.getenv("LARGE_FILE_WARNING_GB", "1.5")) * 1024 ** 3)

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

# Holds a file (message + already-parsed title/episode/etc) that's
# downloaded-and-ready but waiting on the admin to pick which hoster to
# upload it to, keyed by chat_id. Populated by handle_media right before
# prompting for /host, consumed by handle_host_choice.
PENDING_HOST_CHOICE = {}

# Remembers the most recently published title per chat, so /feature,
# /trending, /new, /recommend can default to "whatever I just published"
# without requiring the admin to retype the title.
LAST_PUBLISHED_TITLE = {}

# Active batch/season-pack sessions, keyed by chat_id. While a batch is
# active, incoming files skip the individual confirm/edit prompt and are
# silently uploaded + recorded here instead; /batch done hands the whole
# collected list off to one shared confirm/edit/TMDB flow. See handle_media
# for how files get routed here vs the normal single-file path.
BATCH_SESSIONS = {}

# Holds a completed batch's episode list + title/TMDB state between
# /batch done and /batchconfirm, keyed by chat_id -- structurally the
# batch equivalent of PENDING_PUBLISHES, but with an "episodes" list
# instead of a single episode/url/quality set.
BATCH_PENDING_PUBLISH = {}

# Holds one just-uploaded batch entry that's waiting on the admin to
# confirm/correct its episode number via /setepisode, keyed by chat_id.
# Populated right after each batch-mode upload finishes; consumed by
# handle_set_episode, which appends the finalized entry to the batch
# session's "episodes" list. While this is set, new files can still come
# in (they'll queue behind it in Telegram's own message ordering -- the
# admin is expected to answer /setepisode before forwarding the next
# file, since there's only one pending slot per chat).
PENDING_BATCH_EPISODE = {}

# Holds a matched catalog doc (id + current field snapshot) that's
# awaiting an /edittitle or /deletetitle confirmation, keyed by chat_id.
# Populated by /lookup and /deletetitle (pre-confirm); consumed by
# /edittitle and the /deletetitle confirm step. See handle_lookup,
# handle_edit_title, handle_delete_title.
PENDING_CATALOG_EDIT = {}
PENDING_CATALOG_DELETE = {}

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


async def upload_to_streamtape(client: httpx.AsyncClient, file_path: str) -> str:
    """Upload a file to Streamtape.

    Streamtape's API (streamtape.com/api) differs from the Doodstream-style
    hosters above in several ways:
      - Auth is a login+key pair (not a single API key).
      - GET https://api.streamtape.com/file/ul?login=..&key=.. returns
        {"status":200,"msg":"OK","result":{"url":"https://<server>/xfsupload/<id>",
        "valid_until":"..."}} -- "result" is a dict with the upload URL
        inside it, not a bare string like Doodstream.
      - The multipart form field for the file is "file1", not "file".
      - The POST-upload response is {"status":200,"msg":"OK",
        "result":{"name":..., "size":..., "url":"https://tapecontent.net/<id-or-name>.mp4"}}.
        NOTE: Streamtape's own docs example for this response uses the
        original filename in the URL, not a stable file id, and there is no
        separate "id"/"linkid"/"filecode" field in this particular response.
        Rather than guess at constructing a streamtape.com/v/<id> share link
        (which could easily be wrong and produce a dead link), we return the
        content URL Streamtape actually gave us -- it's guaranteed correct,
        even if it isn't the prettiest possible link.
    """
    if not STREAMTAPE_LOGIN or not STREAMTAPE_KEY:
        return "Key Missing ⚠️"

    filename = os.path.basename(file_path)

    try:
        server_resp = await client.get(
            "https://api.streamtape.com/file/ul",
            params={"login": STREAMTAPE_LOGIN, "key": STREAMTAPE_KEY},
            headers=HEADERS,
            timeout=UPLOAD_TIMEOUT,
        )
    except httpx.TimeoutException:
        logger.warning("Streamtape: timed out fetching upload server")
        return "Error: request to fetch upload server timed out"
    except httpx.HTTPError as e:
        logger.warning("Streamtape: network error fetching upload server: %s", e)
        return f"Error: network issue contacting Streamtape ({type(e).__name__})"

    try:
        server_data = server_resp.json()
    except ValueError:
        logger.warning("Streamtape: non-JSON server response (HTTP %s): %r",
                        server_resp.status_code, server_resp.text[:200])
        return f"API Error (HTTP {server_resp.status_code})"

    result = server_data.get("result")
    upload_url = result.get("url") if isinstance(result, dict) else None
    if not upload_url:
        return f"Server Error: {server_data.get('msg', 'No Upload URL')}"

    try:
        async def _post():
            with open(file_path, "rb") as f:
                files = {"file1": (filename, f, "application/octet-stream")}
                return await client.post(
                    upload_url,
                    files=files,
                    headers=HEADERS,
                    timeout=UPLOAD_TIMEOUT,
                )

        upload_resp = await _post()
    except httpx.TimeoutException:
        logger.warning("Streamtape: upload timed out for %s", filename)
        return "Error: upload timed out"
    except httpx.HTTPError as e:
        logger.warning("Streamtape: network error during upload: %s", e)
        return f"Error: network issue during upload ({type(e).__name__})"
    except OSError as e:
        logger.error("Streamtape: could not read local file %s: %s", file_path, e)
        return "Error: could not read downloaded file"

    try:
        upload_data = upload_resp.json()
    except ValueError:
        logger.warning("Streamtape: invalid upload response: %r", upload_resp.text[:200])
        return f"Response Parse Error: {upload_resp.text[:100]}"

    result = upload_data.get("result")
    content_url = result.get("url") if isinstance(result, dict) else None
    if content_url:
        return content_url

    logger.warning("Streamtape: upload failed, response=%r", upload_data)
    return f"Upload Failed: no content URL in response ({upload_data})"


async def _parse_source_and_infer_episode(message: Message):
    """Shared by handle_media and handle_force_upload: parse title/episode
    from caption/filename, then fill in a missing episode via inference
    from existing Firestore data if possible. Returns
    (guess_title, guess_episode, source_text, episode_was_inferred).
    """
    filename = getattr(message.document, "file_name", None) or \
               getattr(message.video, "file_name", None) or ""
    source_text = (message.caption or "").strip() or filename
    guess_title, guess_episode = firestore_publish.parse_title_and_episode(source_text)
    episode_was_inferred = False

    if guess_title and not guess_episode:
        try:
            inferred = await asyncio.to_thread(firestore_publish.infer_next_episode, guess_title)
        except Exception as e:
            logger.warning("Episode inference failed, proceeding without it: %s", e)
            inferred = ""
        if inferred:
            guess_episode = inferred
            episode_was_inferred = True

    return guess_title, guess_episode, source_text, episode_was_inferred


def _resolve_admin_chat_id():
    """The private chat ID to route torrent handoffs into. Set explicitly
    via the ADMIN_CHAT_ID env var -- there's no reliable way to derive
    "the admin's private chat with this bot" automatically from a group
    message, since group membership doesn't tell us which private chat
    ID corresponds to the human admin behind it.
    """
    raw = os.getenv("ADMIN_CHAT_ID", "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.error("ADMIN_CHAT_ID env var is set but not a valid integer: %r", raw)
        return None


def _get_file_size(message: Message) -> int:
    """Telegram-reported file size in bytes for a video/document message,
    or 0 if unavailable. Shared by the large-file warning check and the
    file-size fallback used when publishing (see firestore_publish.
    publish_doodstream_link's file_size_bytes param).
    """
    return getattr(message.document, "file_size", None) or \
           getattr(message.video, "file_size", None) or 0


@app.on_message(filters.private & (filters.video | filters.document))
async def handle_media(client: Client, message: Message):
    # Parse title/episode from caption/filename BEFORE downloading anything,
    # so an already-published episode can be caught without spending any
    # Telegram download time or Doodstream upload bandwidth on it.
    guess_title, guess_episode, source_text, episode_was_inferred = \
        await _parse_source_and_infer_episode(message)

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

    file_size = _get_file_size(message)
    if file_size >= LARGE_FILE_WARNING_BYTES:
        size_gb = file_size / (1024 ** 3)
        await message.reply_text(
            f"⚠️ **Large file** — {size_gb:.2f} GB. This will use a good chunk of "
            "bandwidth both downloading from Telegram and uploading to Doodstream "
            "(worth knowing if you're watching your Render bandwidth quota).\n\n"
            "Reply `/forceupload` to proceed anyway, or just ignore this to skip it."
        )
        PENDING_FORCE_UPLOAD[message.chat.id] = message
        return

    if message.chat.id in BATCH_SESSIONS and BATCH_SESSIONS[message.chat.id]["active"]:
        # Batch mode intentionally always uses Doodstream, not a per-file
        # prompt -- asking "which host?" for every single episode in a
        # season pack would defeat the point of batch mode's "no per-file
        # interaction" design. Pick a host once via /edit or a future
        # /batch host command if this needs to be configurable later.
        await _download_and_collect_for_batch(message, guess_title, guess_episode, source_text)
        return

    PENDING_HOST_CHOICE[message.chat.id] = {
        "message": message,
        "guess_title": guess_title,
        "guess_episode": guess_episode,
        "source_text": source_text,
        "episode_was_inferred": episode_was_inferred,
    }
    await message.reply_text(
        "📡 Where should this upload to?\n\n"
        "Reply `/host doodstream` or `/host streamtape`."
    )


@app.on_message(filters.private & filters.command("host"))
async def handle_host_choice(client: Client, message: Message):
    pending = PENDING_HOST_CHOICE.pop(message.chat.id, None)
    if not pending:
        await message.reply_text(
            "Nothing waiting on a host choice. Send a file first, then reply "
            "`/host doodstream` or `/host streamtape` when asked."
        )
        return

    raw = message.text.split(None, 1)
    choice = raw[1].strip().lower() if len(raw) > 1 else ""
    host_map = {"doodstream": "Doodstream", "streamtape": "Streamtape"}
    host = host_map.get(choice)

    if not host:
        await message.reply_text("Usage: `/host doodstream` or `/host streamtape`.")
        # Put it back so the admin doesn't have to re-send the file after a typo.
        PENDING_HOST_CHOICE[message.chat.id] = pending
        return

    await _download_and_upload(
        client, pending["message"], pending["guess_title"], pending["guess_episode"],
        pending["source_text"], pending["episode_was_inferred"], host,
    )


@app.on_message(filters.private & filters.command("forceupload"))
async def handle_force_upload(client: Client, message: Message):
    original_message = PENDING_FORCE_UPLOAD.pop(message.chat.id, None)
    if not original_message:
        await message.reply_text(
            "Nothing to force-upload. This only works right after a "
            "duplicate or large-file warning, for that same file."
        )
        return

    guess_title, guess_episode, source_text, episode_was_inferred = \
        await _parse_source_and_infer_episode(original_message)

    # /forceupload skips duplicate/size warnings but still asks which host
    # to use, same as the normal flow -- it's overriding the warning, not
    # the host choice.
    PENDING_HOST_CHOICE[message.chat.id] = {
        "message": original_message,
        "guess_title": guess_title,
        "guess_episode": guess_episode,
        "source_text": source_text,
        "episode_was_inferred": episode_was_inferred,
    }
    await message.reply_text(
        "Proceeding despite the warning. Where should this upload to?\n\n"
        "Reply `/host doodstream` or `/host streamtape`."
    )


def _sanitize_filename_part(text: str) -> str:
    """Strip characters that are unsafe/awkward in filenames (path
    separators, quotes, control chars) and collapse whitespace, without
    being as aggressive as slugify() -- we want "The Scarecrow S01E02",
    not "the-scarecrow-s01e02", since this is a display filename the
    hoster shows to viewers, not a URL slug.
    """
    if not text:
        return ""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .-")
    return cleaned


def _build_upload_filename(original_path: str, guess_title: str, guess_episode: str) -> str:
    """Build a clean "<Title> <Episode><ext>" (or just "<Title><ext>" for
    a movie with no episode) filename to upload as, instead of whatever
    noisy name/watermark the source channel used. Falls back to the
    original filename untouched if no title could be guessed at all --
    a wrong guess is worse than no rename, but no guess means there's
    nothing safe to rename to.
    """
    ext = os.path.splitext(original_path)[1]
    title_part = _sanitize_filename_part(guess_title or "")
    if not title_part:
        return os.path.basename(original_path)

    episode_part = _sanitize_filename_part(guess_episode or "")
    base = f"{title_part} {episode_part}".strip() if episode_part else title_part
    return f"{base}{ext}"


async def _download_and_upload_core(message: Message, status_msg: Message, host: str = "Doodstream",
                                     guess_title: str = None, guess_episode: str = None):
    """Shared by both the single-file flow and batch mode: download from
    Telegram, upload to the chosen hoster, clean up the local file.
    Returns (url_or_error_string, succeeded: bool). The caller decides
    what to do next (prompt for confirm, or silently append to a batch).

    host must be "Doodstream" or "Streamtape".

    If guess_title is given, the downloaded file is renamed to
    "<Title> <Episode>.<ext>" (source channel watermarks/junk stripped)
    before it's handed to the uploader, so that's the name the hoster
    -- and anyone downloading from it -- sees, rather than the original
    source filename. Renaming happens on the local temp copy only; if no
    title was guessed, the original filename is kept as-is.
    """
    try:
        # Pyrogram's download() is already async-native (runs its own I/O off
        # the main loop internally), so no to_thread wrapper needed here.
        file_path = await message.download()
    except Exception as e:
        logger.error("Download failed: %s", e)
        return "Download failed.", False

    if not file_path or not os.path.exists(file_path):
        return "Download failed.", False

    if guess_title:
        new_filename = _build_upload_filename(file_path, guess_title, guess_episode)
        new_path = os.path.join(os.path.dirname(file_path), new_filename)
        try:
            if new_path != file_path:
                # If a file with the target name already exists (e.g. two
                # episodes downloaded to the same temp dir before either
                # was renamed), fall back to the original name rather than
                # silently overwriting/colliding.
                if not os.path.exists(new_path):
                    os.rename(file_path, new_path)
                    file_path = new_path
                else:
                    logger.warning("Rename target %s already exists, keeping original filename", new_path)
        except OSError as e:
            logger.warning("Could not rename %s to %s, uploading with original name: %s",
                            file_path, new_path, e)

    try:
        await status_msg.edit_text(f"🚀 Uploading to {host}...")
        async with httpx.AsyncClient(follow_redirects=True) as http_client:
            if host == "Streamtape":
                result_url = await upload_to_streamtape(http_client, file_path)
            else:
                result_url = await upload_to_hoster(http_client, "Doodstream", file_path)

        if not result_url.startswith("http"):
            return _format_upload_failure(result_url, host), False

        return result_url, True

    except Exception as e:
        logger.exception("Unexpected error while processing %s", file_path)
        return f"Unexpected error: {type(e).__name__}", False

    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning("Failed to remove temp file %s: %s", file_path, e)


async def _download_and_upload(client: Client, message: Message, guess_title: str,
                                guess_episode: str, source_text: str,
                                episode_was_inferred: bool = False, host: str = "Doodstream"):
    status_msg = await message.reply_text("📥 Downloading file from Telegram...")

    result_url, succeeded = await _download_and_upload_core(
        message, status_msg, host, guess_title=guess_title, guess_episode=guess_episode
    )
    if not succeeded:
        await status_msg.edit_text(f"❌ {result_url}")
        return

    logger.info("Parsing title/episode from source_text=%r (caption=%r, filename=%r)",
                source_text, message.caption, getattr(message.document, "file_name", None))
    guess_quality = firestore_publish.detect_quality(source_text)
    file_size_bytes = _get_file_size(message)

    PENDING_PUBLISHES[message.chat.id] = {
        "title": guess_title,
        "episode": guess_episode,
        "url": result_url,
        "server": host,
        "year": None,
        "category": "series" if guess_episode else "movies",
        "quality": guess_quality,
        "episode_was_inferred": episode_was_inferred,
        "tmdb_candidates": None,  # populated once /confirm triggers a TMDB search
        "tmdb_type": None,
        "file_size_bytes": file_size_bytes,
    }

    title_line = guess_title or "(couldn't guess a title)"
    if guess_episode and episode_was_inferred:
        episode_line = f"{guess_episode} (inferred — not in filename, double check this!)"
    else:
        episode_line = guess_episode or "(none detected)"
    category_line = PENDING_PUBLISHES[message.chat.id]["category"]
    quality_line = guess_quality or "(not detected)"
    response = (
        "✅ **Upload complete!**\n\n"
        f"🍿 **{host}:** {result_url}\n\n"
        "**Ready to publish to the site.** Best guess from the caption:\n"
        f"• Title: `{title_line}`\n"
        f"• Episode: {episode_line}\n"
        f"• Category: `{category_line}` (guessed from whether an episode was detected)\n"
        f"• Quality: `{quality_line}`\n\n"
        "Reply `/confirm` to look this up on TMDB and publish, or "
        "`/edit <title> | <episode> | <year> | <category> | <quality>` to correct it first.\n"
        "All parts after title are optional, e.g.:\n"
        "`/edit The Scarecrow | S01E02`\n"
        "`/edit Parasite | | 2019 | movies`"
    )
    await status_msg.edit_text(response)


async def _download_and_collect_for_batch(message: Message, guess_title: str,
                                           guess_episode: str, source_text: str):
    """Batch-mode equivalent of _download_and_upload: downloads and
    uploads the file exactly the same way, but instead of prompting for
    confirm/edit, stops after the upload to ask the admin to confirm the
    episode number for this file (via /setepisode) before it's added to
    the batch. This always asks, even when an episode was successfully
    parsed or inferred, since caption/filename guesses (and especially
    inferred "next episode" guesses) are exactly the kind of thing worth
    a human glance before it's baked into a batch of possibly a dozen
    files. The actual Firestore publish for everything collected happens
    once, in bulk, when the admin runs /batch done.
    """
    session = BATCH_SESSIONS[message.chat.id]
    status_msg = await message.reply_text("📥 Downloading (batch)...")

    dood_url, succeeded = await _download_and_upload_core(
        message, status_msg, guess_title=guess_title, guess_episode=guess_episode
    )
    if not succeeded:
        await status_msg.edit_text(
            f"❌ {dood_url}\n\n(This file was skipped -- the rest of the batch is unaffected.)"
        )
        return

    guess_quality = firestore_publish.detect_quality(source_text)

    # Batches are for a single series -- capture the first file's title
    # guess as the batch's title if we don't have one yet, but don't let
    # a later file's (possibly worse) guess overwrite it.
    if not session.get("title_guess") and guess_title:
        session["title_guess"] = guess_title

    PENDING_BATCH_EPISODE[message.chat.id] = {
        "quality": guess_quality,
        "url": dood_url,
        "source_text": source_text,
        "guess_episode": guess_episode or "",
        "file_size_bytes": _get_file_size(message),
    }

    guess_note = f" — best guess: `{guess_episode}`" if guess_episode else " (nothing detected)"
    await status_msg.edit_text(
        f"✅ Uploaded{f' [{guess_quality}]' if guess_quality else ''}. What episode is this{guess_note}?\n\n"
        + (f"Reply `/setepisode {guess_episode}` to accept the guess, or `/setepisode <value>` "
           if guess_episode else "Reply `/setepisode <value>` ")
        + "to set it (e.g. `/setepisode S01E03`)."
    )


@app.on_message(filters.private & filters.command("setepisode"))
async def handle_set_episode(client: Client, message: Message):
    pending = PENDING_BATCH_EPISODE.pop(message.chat.id, None)
    if not pending:
        await message.reply_text(
            "Nothing waiting on an episode number. This only applies right after "
            "a batch-mode upload finishes."
        )
        return

    raw = message.text.split(None, 1)
    episode = raw[1].strip() if len(raw) > 1 else ""
    if not episode:
        await message.reply_text("Usage: `/setepisode <value>` -- e.g. `/setepisode S01E03`.")
        # Put it back so the admin doesn't lose the pending entry over a
        # bare `/setepisode` with no argument.
        PENDING_BATCH_EPISODE[message.chat.id] = pending
        return

    session = BATCH_SESSIONS.get(message.chat.id)
    if not session or not session["active"]:
        # The batch was cancelled/finished while this upload's episode
        # prompt was still outstanding -- nothing sensible to append to.
        await message.reply_text(
            "⚠️ No active batch to add this episode to (it may have been finished or "
            "cancelled while this was pending). This upload's link wasn't lost -- "
            "it's on the hoster -- but it won't appear in the batch."
        )
        return

    entry = {
        "episode": episode,
        "quality": pending["quality"],
        "url": pending["url"],
        "source_text": pending["source_text"],
        "file_size_bytes": pending.get("file_size_bytes", 0),
    }
    session["episodes"].append(entry)

    await message.reply_text(
        f"✅ Added to batch: `{episode}`"
        + (f" [{pending['quality']}]" if pending["quality"] else "")
        + f"\n{len(session['episodes'])} file(s) collected so far. Send more, or `/batch done` to finish."
    )


@app.on_message(filters.private & filters.command("batch"))
async def handle_batch(client: Client, message: Message):
    args = message.command[1:]  # e.g. ["start"], ["done"], ["status"], ["cancel"]
    sub = args[0].lower() if args else ""
    chat_id = message.chat.id

    if sub == "start":
        if chat_id in BATCH_SESSIONS and BATCH_SESSIONS[chat_id]["active"]:
            await message.reply_text(
                f"A batch is already active with {len(BATCH_SESSIONS[chat_id]['episodes'])} "
                "file(s) collected. Use `/batch done` to finish it, or `/batch cancel` to discard it."
            )
            return
        BATCH_SESSIONS[chat_id] = {"active": True, "episodes": [], "title_guess": None}
        await message.reply_text(
            "📦 **Batch mode started.** Forward all the episodes now — each one uploads "
            "immediately but won't ask you to confirm individually. "
            "When you're done, send `/batch done`."
        )
        return

    if sub == "status":
        session = BATCH_SESSIONS.get(chat_id)
        if not session or not session["active"]:
            await message.reply_text("No active batch. Use `/batch start` to begin one.")
            return
        if not session["episodes"]:
            await message.reply_text("Batch active, but no files collected yet.")
            return
        lines = [f"📦 **Batch in progress** — {len(session['episodes'])} file(s):"]
        for e in session["episodes"]:
            ep = e["episode"] or "(no episode detected)"
            q = f" [{e['quality']}]" if e["quality"] else ""
            lines.append(f"• {ep}{q}")
        await message.reply_text("\n".join(lines))
        return

    if sub == "cancel":
        session = BATCH_SESSIONS.pop(chat_id, None)
        pending_publish = BATCH_PENDING_PUBLISH.pop(chat_id, None)
        if not session and not pending_publish:
            await message.reply_text("No active batch to cancel.")
            return
        count = len(session["episodes"]) if session else len(pending_publish["episodes"])
        await message.reply_text(
            f"🗑️ Batch cancelled. {count} file(s) had already been uploaded to Doodstream "
            "(those links still exist, just not published to the site) -- nothing was "
            "published to Firestore."
        )
        return

    if sub == "done":
        session = BATCH_SESSIONS.get(chat_id)
        if not session or not session["active"]:
            await message.reply_text("No active batch. Use `/batch start` to begin one.")
            return
        if not session["episodes"]:
            await message.reply_text("No files were collected in this batch. `/batch cancel` to discard it.")
            return

        # Remove the collection session entirely (not just mark inactive)
        # so a later /batch cancel doesn't find this stale entry instead
        # of the real BATCH_PENDING_PUBLISH state that /batchconfirm acts on.
        del BATCH_SESSIONS[chat_id]

        title_guess = session.get("title_guess") or ""
        episodes_missing = sum(1 for e in session["episodes"] if not e["episode"])

        BATCH_PENDING_PUBLISH[chat_id] = {
            "title": title_guess,
            "episodes": session["episodes"],
            "year": None,
            "category": "series",
            "tmdb_candidates": None,
            "tmdb_type": None,
        }

        lines = [
            f"📦 **Batch collection done** — {len(session['episodes'])} file(s):",
            "",
        ]
        for e in session["episodes"]:
            ep = e["episode"] or "❓(no episode detected)"
            q = f" [{e['quality']}]" if e["quality"] else ""
            lines.append(f"• {ep}{q}")
        lines.append("")
        lines.append(f"Guessed title: `{title_guess or '(none — use /batchedit to set one)'}`")
        if episodes_missing:
            lines.append(
                f"\n⚠️ {episodes_missing} file(s) have no detected episode number -- "
                "these need fixing before publishing (see `/batchedit`)."
            )
        lines.append(
            "\nReply `/batchconfirm` to look this up on TMDB and publish all episodes, "
            "or `/batchedit <title> | <year> | <category>` to correct the title first."
        )
        await message.reply_text("\n".join(lines))
        return

    await message.reply_text(
        "Usage: `/batch start`, `/batch status`, `/batch done`, or `/batch cancel`."
    )


def _format_upload_failure(error_text: str, host: str = "Doodstream") -> str:
    """Give the admin a plain-language read on WHY an upload failed,
    distinguishing '<host> itself is having problems' (nothing you can
    do but wait/retry later) from other failure categories, rather than
    always showing the same generic wall of text.

    host names whichever hoster actually produced this error, so the
    message doesn't misattribute a Streamtape failure to Doodstream (or
    vice versa) -- see the bug this fixed: a missing STREAMTAPE_KEY was
    being reported as "Doodstream API key isn't configured".
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
            f"❌ **Upload failed — looks like a {host} outage or slowdown**, not a problem with your file.\n\n"
            f"Details: {error_text}\n\n"
            "This was retried automatically and still failed. Worth trying again "
            "in a few minutes, or checking the hoster's status if it keeps happening."
        )
    if "key missing" in lower:
        return f"❌ **{host} API key isn't configured** on the bot. Details: {error_text}"
    return f"❌ Upload failed: {error_text}"


@app.on_message(filters.private & filters.command(["status", "pending"]))
async def handle_status(client: Client, message: Message):
    chat_id = message.chat.id

    batch_session = BATCH_SESSIONS.get(chat_id)
    if batch_session and batch_session["active"]:
        await message.reply_text(
            f"📦 Batch mode is active — {len(batch_session['episodes'])} file(s) collected so far. "
            "Send more files, or `/batch done` to finish."
        )
        return

    batch_pending = BATCH_PENDING_PUBLISH.get(chat_id)
    if batch_pending:
        await message.reply_text(
            f"📦 A completed batch is awaiting confirmation — {len(batch_pending['episodes'])} "
            f"episode(s), title guess `{batch_pending.get('title') or '(none)'}`.\n"
            "Reply `/batchconfirm` to publish, or `/batchedit` to correct the title first."
        )
        return

    pending = PENDING_PUBLISHES.get(chat_id)
    if not pending:
        await message.reply_text("Nothing pending right now. Send a video/file to get started.")
        return

    title_line = pending.get("title") or "(couldn't guess a title)"
    episode_line = pending.get("episode") or "(none)"
    year_line = pending.get("year") or "(none)"
    category_line = pending.get("category", "series")
    quality_line = pending.get("quality") or "(not detected)"

    lines = [
        "📋 **Pending upload:**",
        f"• Title: `{title_line}`",
        f"• Episode: `{episode_line}`",
        f"• Year: `{year_line}`",
        f"• Category: `{category_line}`",
        f"• Quality: `{quality_line}`",
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


@app.on_message(filters.private & filters.command(["feature", "trending", "new", "recommend"]))
async def handle_toggle_flag(client: Client, message: Message):
    # message.command[0] is the command word actually used (without the
    # slash), e.g. "feature" for /feature -- map it to toggle_flag's key
    # names, noting /recommend maps to the "recommended" flag since the
    # command reads better as a verb than the noun the flag uses.
    command_word = message.command[0]
    flag_name = {
        "feature": "featured",
        "trending": "trending",
        "new": "new",
        "recommend": "recommended",
    }[command_word]

    raw = message.text.split(None, 1)
    explicit_title = raw[1].strip() if len(raw) > 1 else ""
    title = explicit_title or LAST_PUBLISHED_TITLE.get(message.chat.id)

    if not title:
        await message.reply_text(
            f"Usage: `/{command_word} <title>` -- or just `/{command_word}` right after "
            "publishing something to apply it to that title."
        )
        return

    try:
        result = await asyncio.to_thread(firestore_publish.toggle_flag, title, flag_name)
    except Exception as e:
        logger.exception("Flag toggle failed")
        await message.reply_text(f"❌ Couldn't update `{title}`: {type(e).__name__}: {e}")
        return

    if not result.get("found"):
        await message.reply_text(
            f"Couldn't find a published title matching `{title}`. "
            "Try `/find <title>` to check the exact title on the site."
        )
        return

    _, emoji = firestore_publish.TOGGLEABLE_FLAGS[flag_name]
    state_word = "ON" if result["new_value"] else "OFF"
    await message.reply_text(f"{emoji} `{result['title']}` — {command_word} is now **{state_word}**.")


@app.on_message(filters.private & filters.command("help"))
async def handle_help(client: Client, message: Message):
    await message.reply_text(
        "**NovaFlix Media Router — commands**\n\n"
        "Send a video or document to start an upload. It'll ask:\n"
        "• `/host doodstream` or `/host streamtape` — which hoster to upload to\n\n"
        "Then after it finishes uploading:\n"
        "• `/confirm` — look the guessed title up on TMDB and publish\n"
        "• `/edit <title> | <episode> | <year> | <category> | <quality>` — correct the guess first "
        "(only title is required, e.g. `/edit Parasite | | 2019 | movies`)\n"
        "• `/pick <number>` — choose from a TMDB shortlist if multiple matches came up\n\n"
        "**Batch mode** (for season packs / many episodes at once):\n"
        "• `/batch start` — begin collecting; forward all episodes, each uploads "
        "immediately, then asks you to confirm the episode number with `/setepisode <value>`\n"
        "• `/setepisode <value>` — answer the episode prompt after a batch upload "
        "(e.g. `/setepisode S01E03`)\n"
        "• `/batch status` — see what's been collected so far\n"
        "• `/batch done` — finish collecting and move to one shared confirm step\n"
        "• `/batchconfirm`, `/batchpick <number>`, `/batchedit <title> | <year> | <category>` "
        "— same as /confirm, /pick, /edit but apply to the whole batch at once\n"
        "• `/batch cancel` — discard a batch (already-uploaded Doodstream links aren't deleted, "
        "just not published)\n\n"
        "**Catalog management** (editing/removing things already on the site):\n"
        "• `/lookup <title>` — show the current field values for a published title\n"
        "• `/edittitle <title> | field=value | field=value | ...` — update specific fields "
        "on an existing title (e.g. `/edittitle The Scarecrow | year=2024 | genre=Drama`)\n"
        "• `/deletetitle <title>` — permanently delete a title (asks for confirmation first)\n\n"
        "**Announcements** (site-wide dismissible banner):\n"
        "• `/announce <message>` — set the banner message (keeps current on/off state)\n"
        "• `/announce <message> | <title> | <tone> | <linkUrl> | <linkLabel>` — full form "
        "(tone: info/success/warning/alert)\n"
        "• `/announceon`, `/announceoff` — show/hide the banner without retyping it\n"
        "• `/announcestatus` — see the current banner text and on/off state\n\n"
        "Other commands:\n"
        "• `/status` or `/pending` — see what's currently awaiting confirmation\n"
        "• `/find <title>` — check if something's already published on the site\n"
        "• `/forceupload` — upload anyway after a duplicate warning\n"
        "• `/feature`, `/trending`, `/new`, `/recommend` — toggle that flag on the site "
        "(defaults to whatever you just published, or pass a title: `/feature The Scarecrow`)\n"
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

    # Usage: /edit <title> | <episode> | <year> | <category> | <quality>
    # Every part after title is optional; leave a segment blank to keep
    # whatever's already set (e.g. "/edit The Scarecrow | | 2023" keeps
    # the existing episode and just sets the year).
    raw = message.text.split(None, 1)
    args = raw[1] if len(raw) > 1 else ""
    if not args.strip():
        await message.reply_text(
            "Usage: `/edit <title> | <episode> | <year> | <category> | <quality>`\n"
            "Only title is required. Examples:\n"
            "`/edit The Scarecrow | S01E02`\n"
            "`/edit Parasite | | 2019 | movies`\n"
            "`/edit The Scarecrow | S01E02 | | | 1080p`"
        )
        return

    parts = [p.strip() for p in args.split("|")]
    title = parts[0]
    episode = parts[1] if len(parts) > 1 and parts[1] else pending.get("episode")
    year = parts[2] if len(parts) > 2 and parts[2] else pending.get("year")
    category = parts[3] if len(parts) > 3 and parts[3] else pending.get("category")
    quality = parts[4] if len(parts) > 4 and parts[4] else pending.get("quality")

    if not title:
        await message.reply_text("Title can't be empty.")
        return

    if category and category not in ("movies", "series"):
        await message.reply_text("Category must be `movies` or `series`.")
        return

    if quality and quality.lower() not in ("2160p", "4k", "1080p", "720p", "480p"):
        await message.reply_text("Quality must be one of: 2160p, 1080p, 720p, 480p.")
        return
    if quality:
        quality = "2160p" if quality.lower() == "4k" else quality.lower()

    # A movie has no episode by definition -- if the admin explicitly sets
    # category to "movies" (whether just now or previously), don't let a
    # stale episode value from an earlier /edit or the caption guess leak
    # through just because this /edit call happened to leave that segment
    # blank.
    final_category = category or "series"
    if final_category == "movies":
        episode = None

    pending["title"] = title
    pending["episode"] = episode
    pending["year"] = year
    pending["category"] = final_category
    pending["quality"] = quality or ""
    # Any manual edit invalidates a previous TMDB search/shortlist.
    pending["tmdb_candidates"] = None
    pending["tmdb_type"] = None
    PENDING_PUBLISHES[message.chat.id] = pending

    await message.reply_text(
        f"Updated. Title: `{title}` | Episode: `{episode or '(none)'}` | "
        f"Year: `{year or '(none)'}` | Category: `{pending['category']}` | "
        f"Quality: `{pending['quality'] or '(none)'}`\n"
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
            pending.get("server", "Doodstream"),
            pending.get("category", "series"),
            extra_fields,
            pending.get("quality", ""),
            pending.get("file_size_bytes", 0),
        )
        action = result["action"]
        if action == "created":
            tmdb_note = " (with TMDB data)" if extra_fields else ""
            text = f"✅ Published as a **new** entry{tmdb_note}: `{pending['title']}`"
        elif action == "appended":
            text = f"✅ Added **{pending['episode'] or 'link'}** to existing entry: `{pending['title']}`"
        else:
            text = f"ℹ️ This link was already published for `{pending['title']}` — skipped duplicate."

        # Remember this as "the last thing published in this chat", so
        # /feature, /trending, /new, /recommend can default to it without
        # requiring the admin to retype the title right after confirming.
        LAST_PUBLISHED_TITLE[message.chat.id] = pending["title"]

        await status.edit_text(text)
    except Exception as e:
        logger.exception("Failed to publish to Firestore")
        await status.edit_text(
            f"❌ Publish failed: {type(e).__name__}: {e}\n\n"
            "The Doodstream upload itself is fine -- you can add it manually "
            "in admin.html using the link above."
        )


@app.on_message(filters.private & filters.command("batchedit"))
async def handle_batch_edit(client: Client, message: Message):
    pending = BATCH_PENDING_PUBLISH.get(message.chat.id)
    if not pending:
        await message.reply_text("No completed batch waiting. Use `/batch done` first.")
        return

    raw = message.text.split(None, 1)
    args = raw[1] if len(raw) > 1 else ""
    if not args.strip():
        await message.reply_text(
            "Usage: `/batchedit <title> | <year> | <category>`\n"
            "Only title is required, e.g. `/batchedit The Scarecrow | 2023`\n"
            "(There's no per-episode field here -- fix individual episode numbers "
            "by re-running `/batch` if something was misdetected.)"
        )
        return

    parts = [p.strip() for p in args.split("|")]
    title = parts[0]
    year = parts[1] if len(parts) > 1 and parts[1] else pending.get("year")
    category = parts[2] if len(parts) > 2 and parts[2] else pending.get("category")

    if not title:
        await message.reply_text("Title can't be empty.")
        return
    if category and category not in ("movies", "series"):
        await message.reply_text("Category must be `movies` or `series`.")
        return

    pending["title"] = title
    pending["year"] = year
    pending["category"] = category or "series"
    pending["tmdb_candidates"] = None
    pending["tmdb_type"] = None
    BATCH_PENDING_PUBLISH[message.chat.id] = pending

    await message.reply_text(
        f"Updated. Title: `{title}` | Year: `{year or '(none)'}` | Category: `{pending['category']}`\n"
        "Reply `/batchconfirm` to look this up on TMDB and publish all episodes."
    )


@app.on_message(filters.private & filters.command("batchconfirm"))
async def handle_batch_confirm(client: Client, message: Message):
    pending = BATCH_PENDING_PUBLISH.get(message.chat.id)
    if not pending:
        await message.reply_text("No completed batch waiting. Use `/batch done` first.")
        return

    if not pending["title"]:
        await message.reply_text(
            "No title could be guessed for this batch, so it can't be confirmed as-is. "
            "Use `/batchedit <title>` instead."
        )
        return

    if pending.get("tmdb_candidates") is not None:
        await _do_batch_publish(message, pending, extra_fields=None)
        BATCH_PENDING_PUBLISH.pop(message.chat.id, None)
        return

    status = await message.reply_text(f"🔎 Searching TMDB for \"{pending['title']}\"...")
    try:
        search_result = await tmdb_fetch.search_tmdb(pending["title"], pending["category"])
    except Exception as e:
        logger.exception("TMDB search failed for batch")
        await status.edit_text(
            f"⚠️ TMDB search failed ({type(e).__name__}). Publishing all episodes without TMDB data."
        )
        await _do_batch_publish(message, pending, extra_fields=None)
        BATCH_PENDING_PUBLISH.pop(message.chat.id, None)
        return

    matches = search_result["matches"]
    tmdb_type = search_result["tmdb_type"]

    if not matches:
        await status.edit_text(
            f"No TMDB matches found for \"{pending['title']}\". Publishing all episodes without TMDB data."
        )
        await _do_batch_publish(message, pending, extra_fields=None)
        BATCH_PENDING_PUBLISH.pop(message.chat.id, None)
        return

    ranked = tmdb_fetch.rank_matches_by_year(matches, pending.get("year"))

    if len(ranked) == 1:
        await status.edit_text("Found 1 match on TMDB — fetching details...")
        await _batch_fetch_and_publish(message, pending, ranked[0], tmdb_type)
        BATCH_PENDING_PUBLISH.pop(message.chat.id, None)
        return

    pending["tmdb_candidates"] = ranked
    pending["tmdb_type"] = tmdb_type
    BATCH_PENDING_PUBLISH[message.chat.id] = pending

    lines = [tmdb_fetch.format_candidate_line(m, i + 1) for i, m in enumerate(ranked)]
    year_note = f" (ranked using year hint {pending['year']})" if pending.get("year") else ""
    await status.edit_text(
        f"Found {len(ranked)} possible matches{year_note}:\n\n"
        + "\n".join(lines)
        + "\n\nReply `/batchpick <number>` to choose one, or `/batchconfirm` again to "
          "publish all episodes without TMDB data."
    )


@app.on_message(filters.private & filters.command("batchpick"))
async def handle_batch_pick(client: Client, message: Message):
    pending = BATCH_PENDING_PUBLISH.get(message.chat.id)
    if not pending or not pending.get("tmdb_candidates"):
        await message.reply_text("Nothing to pick from. Use `/batchconfirm` first to search TMDB.")
        return

    raw = message.text.split(None, 1)
    arg = raw[1].strip() if len(raw) > 1 else ""
    if not arg.isdigit():
        await message.reply_text("Usage: `/batchpick <number>` -- e.g. `/batchpick 2`")
        return

    index = int(arg) - 1
    candidates = pending["tmdb_candidates"]
    if index < 0 or index >= len(candidates):
        await message.reply_text(f"Pick a number between 1 and {len(candidates)}.")
        return

    status = await message.reply_text("Fetching details from TMDB...")
    await _batch_fetch_and_publish(message, pending, candidates[index], pending["tmdb_type"], status=status)
    BATCH_PENDING_PUBLISH.pop(message.chat.id, None)


async def _batch_fetch_and_publish(message: Message, pending: dict, candidate: dict,
                                    tmdb_type: str, status: Message = None):
    try:
        tmdb_id = candidate["id"]
        details = await tmdb_fetch.fetch_tmdb_details(tmdb_id, tmdb_type)
    except Exception as e:
        logger.exception("TMDB detail fetch failed for batch")
        msg = f"⚠️ Couldn't fetch TMDB details ({type(e).__name__}). Publishing all episodes without TMDB data."
        if status:
            await status.edit_text(msg)
        else:
            await message.reply_text(msg)
        await _do_batch_publish(message, pending, extra_fields=None)
        return

    await _do_batch_publish(message, pending, extra_fields=details)


async def _do_batch_publish(message: Message, pending: dict, extra_fields: dict = None):
    """Publish every episode collected in a batch, in order, reusing the
    same shared title/category/TMDB data for all of them. TMDB extra_fields
    is only actually used for the FIRST episode published (which is
    whichever one ends up creating the doc, if it doesn't already exist);
    publish_doodstream_link itself already ignores extra_fields on append,
    so passing it through unconditionally for every call is safe and
    simpler than tracking "was this the creating call" here too.

    Episodes with no detected episode number are skipped with a clear
    warning rather than silently publishing them as blank/duplicate
    entries -- those need a manual fix (re-upload with a clearer filename,
    or handled individually outside batch mode).
    """
    status = await message.reply_text(
        f"📡 Publishing {len(pending['episodes'])} episode(s) to the site..."
    )

    published, skipped, failed = [], [], []

    for ep in pending["episodes"]:
        if not ep["episode"]:
            skipped.append(ep)
            continue
        try:
            result = await asyncio.to_thread(
                firestore_publish.publish_doodstream_link,
                pending["title"],
                ep["episode"],
                ep["url"],
                "Doodstream",
                pending.get("category", "series"),
                extra_fields,
                ep.get("quality", ""),
                ep.get("file_size_bytes", 0),
            )
            published.append((ep, result["action"]))
        except Exception as e:
            logger.exception("Failed to publish batch episode %s", ep.get("episode"))
            failed.append((ep, str(e)))

    LAST_PUBLISHED_TITLE[message.chat.id] = pending["title"]

    lines = [f"✅ **Batch publish complete** for `{pending['title']}`:", ""]
    if published:
        lines.append(f"Published {len(published)} episode(s):")
        for ep, action in published:
            lines.append(f"• {ep['episode']} ({action})")
    if skipped:
        lines.append("")
        lines.append(f"⚠️ Skipped {len(skipped)} file(s) with no detected episode number.")
    if failed:
        lines.append("")
        lines.append(f"❌ Failed to publish {len(failed)} episode(s):")
        for ep, err in failed:
            lines.append(f"• {ep.get('episode', '?')}: {err}")

    await status.edit_text("\n".join(lines))


# ── Catalog editing / deletion ─────────────────────────────────────────────
# Mirrors admin.html's "Edit" (pencil) and "Delete" (trash) buttons in the
# Catalog Ledger (see js/admin.js: triggerAdminEdit / triggerAdminDelete).
# The web admin edits via a full form re-render; here the equivalent is a
# `/lookup` to see current values, then `/edittitle` with just the fields
# you want to change (unset fields are left exactly as they are -- this is
# a partial update, not a full overwrite, since there's no form UI to
# pre-populate from in a chat).

EDITABLE_FIELDS_HELP = (
    "Editable fields: title, year, size, genre, category (movies/series), "
    "description, image (poster URL), backdrop, trailer, duration, language, "
    "director, contentRating, voteAverage, p1080, isFeatured, isTrending, "
    "isNewRelease, isRecommended"
)


@app.on_message(filters.private & filters.command("lookup"))
async def handle_lookup(client: Client, message: Message):
    raw = message.text.split(None, 1)
    title = raw[1].strip() if len(raw) > 1 else ""
    if not title:
        await message.reply_text("Usage: `/lookup <title>` -- e.g. `/lookup The Scarecrow`")
        return

    status = await message.reply_text(f"🔎 Looking up \"{title}\"...")
    try:
        doc = await asyncio.to_thread(firestore_publish.get_title_snapshot, title)
    except Exception as e:
        logger.exception("Lookup failed")
        await status.edit_text(f"❌ Lookup failed: {type(e).__name__}: {e}")
        return

    if not doc:
        await status.edit_text(
            f"No published title matching `{title}` found. Try `/find <title>` "
            "if you're not sure of the exact title."
        )
        return

    # Stash so /edittitle and /deletetitle don't need to re-look-up by
    # title text (avoids acting on a different doc if two titles are
    # similar) -- both commands still take a title argument for clarity,
    # but this snapshot is what confirms which exact doc was meant.
    PENDING_CATALOG_EDIT[message.chat.id] = {"doc_id": doc["id"], "title": doc.get("title")}

    lines = [f"📋 **{doc.get('title', 'Untitled')}** (`{doc['id']}`)", ""]
    for key in ("category", "year", "size", "genre", "p1080",
                "isFeatured", "isTrending", "isNewRelease", "isRecommended",
                "director", "contentRating", "voteAverage", "duration", "language",
                "image", "backdrop", "trailer"):
        val = doc.get(key)
        if val in (None, "", []):
            continue
        lines.append(f"• {key}: `{val}`")
    desc = (doc.get("description") or "").strip()
    if desc:
        preview = desc if len(desc) <= 200 else desc[:200] + "…"
        lines.append(f"• description: {preview}")
    links = doc.get("directLinks") or []
    lines.append(f"• directLinks: {len(links)} link(s) (use the batch/edit flow to add more, not `/edittitle`)")

    lines.append("")
    lines.append(EDITABLE_FIELDS_HELP)
    lines.append(
        f"\nReply `/edittitle {doc.get('title')} | field=value | field=value` to update, "
        f"or `/deletetitle {doc.get('title')}` to delete."
    )
    await status.edit_text("\n".join(lines))


@app.on_message(filters.private & filters.command("edittitle"))
async def handle_edit_title(client: Client, message: Message):
    raw = message.text.split(None, 1)
    args = raw[1] if len(raw) > 1 else ""
    if not args.strip() or "|" not in args:
        await message.reply_text(
            "Usage: `/edittitle <title> | field=value | field=value | ...`\n"
            f"{EDITABLE_FIELDS_HELP}\n\n"
            "Examples:\n"
            "`/edittitle The Scarecrow | year=2024 | genre=Drama`\n"
            "`/edittitle Parasite | isFeatured=true | p1080=true`\n\n"
            "Tip: run `/lookup <title>` first to see current values and the exact stored title."
        )
        return

    parts = [p.strip() for p in args.split("|")]
    title = parts[0]
    field_args = parts[1:]

    if not title:
        await message.reply_text("Title can't be empty.")
        return
    if not field_args:
        await message.reply_text("Give at least one `field=value` to update.")
        return

    updates = {}
    errors = []
    for part in field_args:
        if "=" not in part:
            errors.append(f"`{part}` -- expected `field=value`")
            continue
        field, _, value = part.partition("=")
        field = field.strip()
        value = value.strip()
        try:
            parsed = firestore_publish.parse_edit_field(field, value)
        except ValueError as e:
            errors.append(str(e))
            continue
        updates[field] = parsed

    if errors:
        await message.reply_text(
            "❌ Couldn't parse some fields:\n" + "\n".join(f"• {e}" for e in errors) +
            f"\n\n{EDITABLE_FIELDS_HELP}"
        )
        return

    status = await message.reply_text(f"📡 Updating \"{title}\"...")
    try:
        result = await asyncio.to_thread(firestore_publish.update_title_fields, title, updates)
    except Exception as e:
        logger.exception("Edit failed")
        await status.edit_text(f"❌ Update failed: {type(e).__name__}: {e}")
        return

    if not result.get("found"):
        await status.edit_text(
            f"Couldn't find a published title matching `{title}`. "
            "Try `/find <title>` or `/lookup <title>` to check the exact title first."
        )
        return

    field_summary = ", ".join(f"{k}={v}" for k, v in updates.items())
    await status.edit_text(f"✅ Updated `{result['title']}` — {field_summary}")


@app.on_message(filters.private & filters.command("deletetitle"))
async def handle_delete_title(client: Client, message: Message):
    raw = message.text.split(None, 1)
    title = raw[1].strip() if len(raw) > 1 else ""
    if not title:
        await message.reply_text("Usage: `/deletetitle <title>` -- e.g. `/deletetitle The Scarecrow`")
        return

    # First call: look the title up, stash it, ask for confirmation.
    # Second call (admin replies /deletetitle again with the SAME title,
    # or /confirmdelete): actually deletes. This mirrors admin.html's
    # `confirm("Permanently delete this movie?")` native dialog -- a chat
    # command has no equivalent modal, so a second explicit step stands in
    # for it. Requiring the title to match again (not just any /deletetitle)
    # guards against a stray second tap deleting something else if the
    # admin moved on to looking up a different title in between.
    pending = PENDING_CATALOG_DELETE.get(message.chat.id)
    if pending and pending["title_query"].lower() == title.lower():
        status = await message.reply_text(f"🗑️ Deleting \"{pending['title']}\"...")
        try:
            result = await asyncio.to_thread(firestore_publish.delete_title, pending["doc_id"])
        except Exception as e:
            logger.exception("Delete failed")
            await status.edit_text(f"❌ Delete failed: {type(e).__name__}: {e}")
            return
        PENDING_CATALOG_DELETE.pop(message.chat.id, None)
        if not result.get("found"):
            await status.edit_text("That entry no longer exists (may have already been deleted).")
            return
        await status.edit_text(f"✅ Deleted `{result['title']}` permanently.")
        return

    status = await message.reply_text(f"🔎 Looking up \"{title}\"...")
    try:
        doc = await asyncio.to_thread(firestore_publish.get_title_snapshot, title)
    except Exception as e:
        logger.exception("Lookup for delete failed")
        await status.edit_text(f"❌ Lookup failed: {type(e).__name__}: {e}")
        return

    if not doc:
        await status.edit_text(
            f"No published title matching `{title}` found. Try `/find <title>` "
            "if you're not sure of the exact title."
        )
        return

    links_count = len(doc.get("directLinks") or [])
    PENDING_CATALOG_DELETE[message.chat.id] = {
        "doc_id": doc["id"], "title": doc.get("title"), "title_query": title,
    }
    await status.edit_text(
        f"⚠️ **Confirm delete**\n\n"
        f"`{doc.get('title', 'Untitled')}` — {doc.get('category', '?')}, "
        f"{links_count} link(s), year {doc.get('year') or '?'}\n\n"
        "This permanently deletes the entire entry (all episodes/links included) "
        "from the site. This can't be undone.\n\n"
        f"Reply `/deletetitle {title}` again to confirm."
    )


# ── Announcements ─────────────────────────────────────────────────────────
# Mirrors admin.html's Announcement tab -- a single site-wide dismissible
# banner (see js/admin.js: executeAdminAnnouncementUpload). Unlike catalog
# entries there's only ever one of these, so no title/lookup step is
# needed -- just set it, or flip it on/off.

ANNOUNCEMENT_USAGE = (
    "Usage:\n"
    "`/announce <message>` — set the message (keeps current on/off state)\n"
    "`/announce <message> | <title> | <tone> | <linkUrl> | <linkLabel>` — full form\n\n"
    "tone must be one of: info, success, warning, alert (default: info)\n"
    "title, linkUrl, linkLabel are all optional. Examples:\n"
    "`/announce Servers under maintenance tonight`\n"
    "`/announce New feature is live! | Heads up: | success | https://example.com | Learn more`"
)


@app.on_message(filters.private & filters.command("announce"))
async def handle_announce(client: Client, message: Message):
    raw = message.text.split(None, 1)
    args = raw[1].strip() if len(raw) > 1 else ""
    if not args:
        await message.reply_text(ANNOUNCEMENT_USAGE)
        return

    parts = [p.strip() for p in args.split("|")]
    msg_text = parts[0]
    title = parts[1] if len(parts) > 1 else ""
    tone = parts[2] if len(parts) > 2 and parts[2] else "info"
    link_url = parts[3] if len(parts) > 3 else ""
    link_label = parts[4] if len(parts) > 4 else ""

    if not msg_text:
        await message.reply_text("Message can't be empty.\n\n" + ANNOUNCEMENT_USAGE)
        return

    status = await message.reply_text("📡 Updating announcement...")
    try:
        result = await asyncio.to_thread(
            firestore_publish.set_announcement,
            msg_text, title, tone, link_url, link_label, None,
        )
    except ValueError as e:
        await status.edit_text(f"❌ {e}\n\n{ANNOUNCEMENT_USAGE}")
        return
    except Exception as e:
        logger.exception("Announcement update failed")
        await status.edit_text(f"❌ Update failed: {type(e).__name__}: {e}")
        return

    state_word = "LIVE for all visitors" if result["isActive"] else "saved but hidden (use `/announceon` to show it)"
    await status.edit_text(f"✅ Announcement updated — {state_word}.")


@app.on_message(filters.private & filters.command("announceon"))
async def handle_announce_on(client: Client, message: Message):
    status = await message.reply_text("📡 Turning announcement on...")
    try:
        await asyncio.to_thread(firestore_publish.set_announcement_active, True)
    except ValueError as e:
        await status.edit_text(f"❌ {e}")
        return
    except Exception as e:
        logger.exception("Announcement toggle failed")
        await status.edit_text(f"❌ Failed: {type(e).__name__}: {e}")
        return
    await status.edit_text("✅ Announcement is now **LIVE** for all visitors.")


@app.on_message(filters.private & filters.command("announceoff"))
async def handle_announce_off(client: Client, message: Message):
    status = await message.reply_text("📡 Turning announcement off...")
    try:
        await asyncio.to_thread(firestore_publish.set_announcement_active, False)
    except ValueError as e:
        await status.edit_text(f"❌ {e}")
        return
    except Exception as e:
        logger.exception("Announcement toggle failed")
        await status.edit_text(f"❌ Failed: {type(e).__name__}: {e}")
        return
    await status.edit_text("✅ Announcement is now **hidden** from visitors.")


@app.on_message(filters.private & filters.command(["announcestatus", "announcement"]))
async def handle_announce_status(client: Client, message: Message):
    status = await message.reply_text("🔎 Checking current announcement...")
    try:
        current = await asyncio.to_thread(firestore_publish.get_announcement)
    except Exception as e:
        logger.exception("Announcement status check failed")
        await status.edit_text(f"❌ Failed: {type(e).__name__}: {e}")
        return

    if not current["message"]:
        await status.edit_text(
            "No announcement has been set yet.\n\n" + ANNOUNCEMENT_USAGE
        )
        return

    state_word = "🟢 LIVE" if current["isActive"] else "⚪ Hidden"
    lines = [
        f"📢 **Current announcement** — {state_word}",
        "",
        f"• Title: `{current['title'] or '(none)'}`",
        f"• Message: {current['message']}",
        f"• Tone: `{current['tone']}`",
    ]
    if current["linkUrl"]:
        lines.append(f"• Link: {current['linkUrl']} (\"{current['linkLabel'] or 'no label'}\")")
    lines.append("")
    lines.append("`/announce <message>` to change it, `/announceon`/`/announceoff` to toggle visibility.")
    await status.edit_text("\n".join(lines))


POLL_INTERVAL_SECONDS = int(os.getenv("TORRENT_POLL_INTERVAL_SECONDS", "30"))


async def poll_pending_torrent_uploads(client: Client):
    """Background loop: periodically checks Firestore for finished
    torrent uploads Torrent Fetcher has queued, and feeds each one
    through the same parse -> dup-check -> confirm-prompt flow a normal
    Telegram file upload goes through -- just triggered by a Firestore
    doc appearing instead of a Telegram message arriving.

    Runs for the lifetime of the process (started once in main(), never
    awaited/joined). Any single iteration's exceptions are caught and
    logged so one bad poll doesn't kill the whole loop.
    """
    logger.info("Torrent upload polling started (interval=%ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            pending = await asyncio.to_thread(firestore_publish.fetch_unprocessed_torrent_uploads)
        except Exception as e:
            logger.warning("Polling for pending torrent uploads failed (will retry): %s", e)
            pending = []

        for item in pending:
            try:
                await _handle_polled_torrent_upload(client, item)
            except Exception:
                logger.exception("Failed to process pending torrent upload doc %s", item.get("doc_id"))
                # Don't mark as processed on failure -- leave it for the
                # next poll to retry, rather than silently losing it.
                continue

            try:
                await asyncio.to_thread(firestore_publish.mark_torrent_upload_processed, item["doc_id"])
            except Exception:
                logger.exception("Failed to mark doc %s as processed (will likely reprocess next poll)",
                                  item.get("doc_id"))

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _handle_polled_torrent_upload(client: Client, item: dict):
    """Turn one pending_torrent_uploads doc into the same "ready to
    /confirm" state and message a normal Telegram upload would produce --
    see _download_and_upload for the message this mirrors.
    """
    dood_url = item.get("doodstreamUrl", "")
    source_text = item.get("originalFilename", "") or ""
    target_chat_id = item.get("requestedByChatId") or _resolve_admin_chat_id()

    if not dood_url.startswith("http"):
        logger.warning("Skipping malformed pending torrent upload doc %s: url=%r",
                        item.get("doc_id"), dood_url)
        return

    if target_chat_id is None:
        logger.error(
            "Pending torrent upload doc %s has no requestedByChatId and "
            "ADMIN_CHAT_ID isn't configured -- can't route this anywhere.",
            item.get("doc_id"),
        )
        return

    guess_title, guess_episode = firestore_publish.parse_title_and_episode(source_text)
    episode_was_inferred = False
    if guess_title and not guess_episode:
        try:
            inferred = await asyncio.to_thread(firestore_publish.infer_next_episode, guess_title)
        except Exception as e:
            logger.warning("Episode inference failed for polled upload: %s", e)
            inferred = ""
        if inferred:
            guess_episode = inferred
            episode_was_inferred = True

    guess_quality = firestore_publish.detect_quality(source_text)

    PENDING_PUBLISHES[target_chat_id] = {
        "title": guess_title,
        "episode": guess_episode,
        "url": dood_url,
        "year": None,
        "category": "series" if guess_episode else "movies",
        "quality": guess_quality,
        "episode_was_inferred": episode_was_inferred,
        "tmdb_candidates": None,
        "tmdb_type": None,
    }

    title_line = guess_title or "(couldn't guess a title)"
    if guess_episode and episode_was_inferred:
        episode_line = f"{guess_episode} (inferred — not in filename, double check this!)"
    else:
        episode_line = guess_episode or "(none detected)"
    category_line = PENDING_PUBLISHES[target_chat_id]["category"]
    quality_line = guess_quality or "(not detected)"

    await client.send_message(
        target_chat_id,
        "📦 **Torrent upload received!**\n\n"
        f"🍿 **Doodstream:** {dood_url}\n\n"
        "**Ready to publish to the site.** Best guess from the torrent name:\n"
        f"• Title: `{title_line}`\n"
        f"• Episode: {episode_line}\n"
        f"• Category: `{category_line}`\n"
        f"• Quality: `{quality_line}`\n\n"
        "Reply `/confirm` to look this up on TMDB and publish, or "
        "`/edit <title> | <episode> | <year> | <category> | <quality>` to correct it first.",
    )
    logger.info("Delivered polled torrent upload (doc=%s) to chat_id=%s",
                item.get("doc_id"), target_chat_id)


async def main():
    await app.start()

    # Launch the background task that polls Firestore for finished torrent
    # uploads (see poll_pending_torrent_uploads below). This replaces the
    # original Telegram-group-message handoff design, which never reliably
    # worked in production for reasons that stayed unresolved despite
    # checking every individually-verifiable cause (group membership,
    # Privacy Mode, peer resolution, dispatch priority all came back fine,
    # yet the group message still silently never reached an on_message
    # handler). Polling a data store both bots already talk to reliably
    # sidesteps that whole layer.
    asyncio.create_task(poll_pending_torrent_uploads(app))

    await idle()
    await app.stop()


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
    app.run(main())
