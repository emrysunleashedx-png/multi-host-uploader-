import os
import logging
import asyncio
import httpx
from pyrogram import Client, filters
from pyrogram.types import Message

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
    "EarnVids": {
        "api_key": os.getenv("EARNVIDS_API_KEY", ""),
        "server_url": "https://earnvids.com/api/upload/server",
        "download_url_fmt": "https://earnvids.com/d/{code}",
    },
}

# Upload network timeout. None (no timeout) risks the bot hanging forever
# on a stalled connection; use generous but finite limits instead.
UPLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)

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
    """Extract the filecode from an upload response.

    Per EarnVids' documented API (earnvids.com/api.html), a successful file
    upload response looks like:
        {"msg": "OK", "status": 200,
         "files": [{"filecode": "...", "filename": "...", "status": "OK"}]}
    i.e. the list lives under the top-level "files" key, not "result".

    Doodstream (and possibly EarnVids servers that drift from their own
    docs) have been observed returning the filecode under "result" instead,
    as either a list or a single dict. We check the documented shape first,
    then fall back to the "result"-based shape for compatibility.
    """
    # Documented shape: top-level "files" list.
    files = upload_data.get("files")
    if isinstance(files, list) and files:
        code = _extract_filecode_from_entry(files[0])
        if code:
            return code

    # Fallback shape: top-level "result", either list or dict.
    result = upload_data.get("result")
    if isinstance(result, list) and result:
        result = result[0]
    code = _extract_filecode_from_entry(result)
    if code:
        return code

    return None


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

    # Per EarnVids' documented API, the upload POST only needs "key" (not
    # "api_key", no repeated query-string key, no file_size param).
    # Doodstream's docs use "api_key" as the form field name instead.
    key_field = "key" if hoster_name == "EarnVids" else "api_key"
    extra_data = {key_field: api_key}

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

    # upload_data.get('msg') is often just "OK" even on failure (EarnVids
    # sets top-level msg/status independently of whether files/result
    # actually contains a usable filecode), so surface the raw payload
    # rather than a misleadingly reassuring "OK".
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
        await status_msg.edit_text("🚀 Uploading to Doodstream & EarnVids in parallel...")

        async with httpx.AsyncClient(follow_redirects=True) as http_client:
            # Run both uploads concurrently instead of sequentially.
            dood_task = upload_to_hoster(http_client, "Doodstream", file_path)
            earn_task = upload_to_hoster(http_client, "EarnVids", file_path)
            dood_url, earn_url = await asyncio.gather(dood_task, earn_task)

        response = (
            "✅ **Download Links Ready!**\n\n"
            f"🍿 **Doodstream:** {dood_url}\n"
            f"⚡ **EarnVids:** {earn_url}"
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


if __name__ == "__main__":
    import http.server
    import socketserver
    import threading

    def run_dummy_server():
        # Some hosting platforms (e.g. Render, Railway) require a bound port
        # to consider the service "healthy" even for non-HTTP bots like this one.
        port = int(os.getenv("PORT", "8000"))
        with socketserver.TCPServer(("", port), http.server.SimpleHTTPRequestHandler) as httpd:
            httpd.serve_forever()

    threading.Thread(target=run_dummy_server, daemon=True).start()
    app.run()
