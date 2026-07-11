import asyncio
import threading
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
import os
import asyncio
import httpx
from pyrogram import Client, filters
from pyrogram.types import Message

# Retrieve variables from environment
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

DOODSTREAM_KEY = os.getenv("DOODSTREAM_API_KEY", "")
EARNVIDS_KEY = os.getenv("EARNVIDS_API_KEY", "")
STREAMHG_KEY = os.getenv("STREAMHG_API_KEY", "")

app = Client("multi_uploader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

async def upload_doodstream(client: httpx.AsyncClient, file_path: str):
    if not DOODSTREAM_KEY:
        return "Key Missing ⚠️"
    try:
        res = await client.get(f"https://doodapi.com/api/upload/server?key={DOODSTREAM_KEY}")
        upload_url = res.json().get("result")
        with open(file_path, "rb") as f:
            upload_res = await client.post(upload_url, files={"file": f}, data={"api_key": DOODSTREAM_KEY})
            result = upload_res.json().get("result", [])
            if result:
                return f"https://dood.to/e/{result[0]['file_code']}"
    except Exception as e:
        print(f"Doodstream Error: {e}")
    return "Failed ❌"

async def upload_earnvids(client: httpx.AsyncClient, file_path: str):
    if not EARNVIDS_KEY:
        return "Key Missing ⚠️"
    try:
        res = await client.get(f"https://earnvids.com/api/upload/server?key={EARNVIDS_KEY}")
        upload_url = res.json().get("result")
        with open(file_path, "rb") as f:
            upload_res = await client.post(upload_url, files={"file": f}, data={"api_key": EARNVIDS_KEY})
            result = upload_res.json().get("result", [])
            if result:
                return f"https://earnvids.com/v/{result[0]['file_code']}"
    except Exception as e:
        print(f"EarnVids Error: {e}")
    return "Failed ❌"

async def upload_streamhg(client: httpx.AsyncClient, file_path: str):
    if not STREAMHG_KEY:
        return "Key Missing ⚠️"
    try:
        res = await client.get(f"https://streamwish.com/api/upload/server?key={STREAMHG_KEY}")
        upload_url = res.json().get("result")
        with open(file_path, "rb") as f:
            upload_res = await client.post(upload_url, files={"file": f}, data={"api_key": STREAMHG_KEY})
            result = upload_res.json().get("result", [])
            if result:
                return f"https://streamwish.com/e/{result[0]['file_code']}"
    except Exception as e:
        print(f"StreamHG Error: {e}")
    return "Failed ❌"

@app.on_message(filters.private & (filters.video | filters.document))
async def handle_media(client: Client, message: Message):
    status_msg = await message.reply_text("📥 Downloading file from Telegram...")
    file_path = await message.download()
    
    await status_msg.edit_text("🚀 Uploading to Doodstream, EarnVids, & StreamHG in parallel...")
    
    async with httpx.AsyncClient(timeout=None) as http_client:
        dood_url, earn_url, hg_url = await asyncio.gather(
            upload_doodstream(http_client, file_path),
            upload_earnvids(http_client, file_path),
            upload_streamhg(http_client, file_path)
        )
        
    if os.path.exists(file_path):
        os.remove(file_path)
        
    response = (
        "✅ **Multi-Platform Upload Complete!**\n\n"
        f"🍿 **Doodstream:** {dood_url}\n"
        f"⚡ **EarnVids:** {earn_url}\n"
        f"🎥 **StreamHG:** {hg_url}"
    )
    await status_msg.edit_text(response)

if __name__ == "__main__":
    # Fake a web server on a background thread to bypass Render's health check
    import http.server
    import socketserver
    
    def run_dummy_server():
        port = int(os.getenv("PORT", "8000"))
        handler = http.server.SimpleHTTPRequestHandler
        with socketserver.TCPServer(("", port), handler) as httpd:
            httpd.serve_forever()
            
    import threading
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    # Run your bot loop
    app.run()
