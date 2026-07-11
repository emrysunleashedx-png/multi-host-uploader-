import os
import logging
import asyncio
import httpx
from pyrogram import Client, filters
from pyrogram.types import Message

import firestore_publish

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


async def upload_to_hoster(client: httpx.AsyncClient, hoster_name: str, file_path: str) -> str:
    """Generic uploader used for any hoster following the doodapi-style
    upload-server protocol (fetch server -> POST file -> parse filecode)."""
    config = HOSTERS[hoster_name]
    api_key = config["api_key"]

    if not api_key:
        return "Key Missing ⚠️"

    filename = os.path.basename(file_path)

    try:
        server_resp = await client.get(
            config["server_url"],
            params={"key": api_key},
            headers=HEADERS,
            timeout=UPLOAD_TIMEOUT,
        )
    except httpx.TimeoutException:
        logger.warning("%s: timed out fetching upload server", hoster_name)
        return "Error: request to fetch upload server timed out"
    except httpx.HTTPError as e:
        logger.warning("%s: network error fetching upload server: %s", hoster_name, e)
        return f"Error: network issue contacting {hoster_name} ({type(e).__name__})"

    try:
        server_data = server_resp.json()
    except ValueError:
        logger.warning("%s: non-JSON server response (HTTP %s): %r",
                        hoster_name, server_resp.status_code, server_resp.text[:200])
        return f"API Error (HTTP {server_resp.status_code})"

    upload_url = server_data.get("result")
    if not upload_url:
        return f"Server Error: {server_data.get('msg', 'No Upload URL')}"

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
        return "Error: upload timed out"
    except httpx.HTTPError as e:
        logger.warning("%s: network error during upload: %s", hoster_name, e)
        return f"Error: network issue during upload ({type(e).__name__})"
    except OSError as e:
        logger.error("%s: could not read local file %s: %s", hoster_name, file_path, e)
        return "Error: could not read downloaded file"

    try:
        upload_data = upload_resp.json()
    except ValueError:
        logger.warning("%s: invalid upload response: %r", hoster_name, upload_resp.text[:200])
        return f"Response Parse Error: {upload_resp.text[:100]}"

    file_code = _extract_filecode(upload_data)
    if file_code:
        return config["download_url_fmt"].format(code=file_code)

    # upload_data.get('msg') can be a reassuring "OK" even when result/files
    # doesn't actually contain a usable filecode, so surface the raw payload
    # rather than that misleading top-level message.
    logger.warning("%s: upload failed, response=%r", hoster_name, upload_data)
    return f"Upload Failed: no filecode in response ({upload_data})"


@app.on_message(filters.private & (filters.video | filters.document))
async def handle_media(client: Client, message: Message):
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
        # report the failure as before and stop (no point asking the admin
        # to confirm a title for a link that doesn't exist).
        if not dood_url.startswith("http"):
            await status_msg.edit_text(f"❌ Upload failed: {dood_url}")
            return

        caption = message.caption or ""
        guess_title, guess_episode = firestore_publish.parse_title_and_episode(caption)

        PENDING_PUBLISHES[message.chat.id] = {
            "title": guess_title,
            "episode": guess_episode,
            "url": dood_url,
        }

        title_line = guess_title or "(couldn't guess a title)"
        episode_line = guess_episode or "(none detected)"
        response = (
            "✅ **Upload complete!**\n\n"
            f"🍿 **Doodstream:** {dood_url}\n\n"
            "**Ready to publish to the site.** Best guess from the caption:\n"
            f"• Title: `{title_line}`\n"
            f"• Episode: `{episode_line}`\n\n"
            "Reply `/confirm` to publish as-is, or `/edit <title> | <episode>` "
            "to correct it first (episode is optional, e.g. `/edit The Scarecrow | S01E02`)."
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


@app.on_message(filters.private & filters.command("confirm"))
async def handle_confirm(client: Client, message: Message):
    pending = PENDING_PUBLISHES.pop(message.chat.id, None)
    if not pending:
        await message.reply_text("Nothing pending to confirm. Upload a file first.")
        return

    if not pending["title"]:
        await message.reply_text(
            "No title could be guessed for this upload, so it can't be confirmed "
            "as-is. Use `/edit <title> | <episode>` instead."
        )
        # Put it back so the admin doesn't lose the finished upload link.
        PENDING_PUBLISHES[message.chat.id] = pending
        return

    await _do_publish(message, pending)


@app.on_message(filters.private & filters.command("edit"))
async def handle_edit(client: Client, message: Message):
    pending = PENDING_PUBLISHES.get(message.chat.id)
    if not pending:
        await message.reply_text("Nothing pending to edit. Upload a file first.")
        return

    # Usage: /edit <title> | <episode>   (episode part is optional)
    raw = message.text.split(None, 1)
    args = raw[1] if len(raw) > 1 else ""
    if not args.strip():
        await message.reply_text(
            "Usage: `/edit <title> | <episode>` -- e.g. `/edit The Scarecrow | S01E02` "
            "(you can omit `| <episode>` if there isn't one)."
        )
        return

    if "|" in args:
        title_part, episode_part = args.split("|", 1)
        title = title_part.strip()
        episode = episode_part.strip() or None
    else:
        title = args.strip()
        episode = pending.get("episode")

    if not title:
        await message.reply_text("Title can't be empty.")
        return

    pending["title"] = title
    pending["episode"] = episode
    PENDING_PUBLISHES[message.chat.id] = pending

    await message.reply_text(
        f"Updated. Title: `{title}` | Episode: `{episode or '(none)'}`\n"
        "Reply `/confirm` to publish, or `/edit` again to change it further."
    )


async def _do_publish(message: Message, pending: dict):
    status = await message.reply_text("📡 Publishing to the site...")
    try:
        # firestore_publish does blocking network calls (google-cloud-firestore
        # is sync), so run it in a thread to avoid stalling the event loop.
        result = await asyncio.to_thread(
            firestore_publish.publish_doodstream_link,
            pending["title"],
            pending["episode"],
            pending["url"],
        )
        action = result["action"]
        if action == "created":
            text = f"✅ Published as a **new** entry: `{pending['title']}`"
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
