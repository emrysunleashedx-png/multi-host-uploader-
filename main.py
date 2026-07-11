import os
import asyncio
import httpx
from pyrogram import Client, filters
from pyrogram.types import Message

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

DOODSTREAM_KEY = os.getenv("DOODSTREAM_API_KEY", "")
EARNVIDS_KEY = os.getenv("EARNVIDS_API_KEY", "")
STREAMHG_KEY = os.getenv("STREAMHG_API_KEY", "")

app = Client("multi_uploader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Browser headers to pass Cloudflare/security checks
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

async def upload_doodstream(client: httpx.AsyncClient, file_path: str):
    if not DOODSTREAM_KEY:
        return "Key Missing ⚠️"
    try:
        filename = os.path.basename(file_path)
        url = f"https://doodapi.com/api/upload/server?key={DOODSTREAM_KEY}"
        res = await client.get(url, headers=HEADERS)
        
        if res.status_code != 200 or not res.text.startswith("{"):
            return f"API Blocked (HTTP {res.status_code})"
            
        res_data = res.json()
        upload_url = res_data.get("result")
        if upload_url:
            with open(file_path, "rb") as f:
                upload_res = await client.post(
                    upload_url, 
                    files={"file": (filename, f)}, 
                    data={"api_key": DOODSTREAM_KEY},
                    headers=HEADERS,
                    timeout=None
                )
                result = upload_res.json().get("result", [])
                if result:
                    return f"https://dood.to/e/{result[0]['file_code']}"
    except Exception as e:
        return f"Error: {type(e).__name__}"
    return "Failed ❌"

async def upload_earnvids(client: httpx.AsyncClient, file_path: str):
    if not EARNVIDS_KEY:
        return "Key Missing ⚠️"
    try:
        filename = os.path.basename(file_path)
        url = f"https://earnvids.com/api/upload/server?key={EARNVIDS_KEY}"
        res = await client.get(url, headers=HEADERS)
        
        if res.status_code != 200 or not res.text.startswith("{"):
            return f"API Blocked (HTTP {res.status_code})"
            
        res_data = res.json()
        upload_url = res_data.get("result")
        if upload_url:
            with open(file_path, "rb") as f:
                upload_res = await client.post(
                    upload_url, 
                    files={"file": (filename, f)}, 
                    data={"api_key": EARNVIDS_KEY},
                    headers=HEADERS,
                    timeout=None
                )
                result = upload_res.json().get("result", [])
                if result:
                    return f"https://earnvids.com/v/{result[0]['file_code']}"
    except Exception as e:
        return f"Error: {type(e).__name__}"
    return "Failed ❌"

async def upload_streamhg(client: httpx.AsyncClient, file_path: str):
    if not STREAMHG_KEY:
        return "Key Missing ⚠️"
    try:
        filename = os.path.basename(file_path)
        url = f"https://streamwish.com/api/upload/server?key={STREAMHG_KEY}"
        res = await client.get(url, headers=HEADERS)
        
        if res.status_code != 200 or not res.text.startswith("{"):
            return f"API Blocked (HTTP {res.status_code})"
            
        res_data = res.json()
        upload_url = res_data.get("result")
        if upload_url:
            with open(file_path, "rb") as f:
                upload_res = await client.post(
                    upload_url, 
                    files={"file": (filename, f)}, 
                    data={"api_key": STREAMHG_KEY},
                    headers=HEADERS,
                    timeout=None
                )
                result = upload_res.json().get("result", [])
                if result:
                    return f"https://streamwish.com/e/{result[0]['file_code']}"
    except Exception as e:
        return f"Error: {type(e).__name__}"
    return "Failed ❌"

@app.on_message(filters.private & (filters.video | filters.document))
async def handle_media(client: Client, message: Message):
    status_msg = await message.reply_text("📥 Downloading file from Telegram...")
    file_path = await message.download()
    
    if not file_path or not os.path.exists(file_path):
        await status_msg.edit_text("❌ Download failed.")
        return

    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as http_client:
        await status_msg.edit_text("🚀 Uploading to Doodstream (1/3)...")
        dood_url = await upload_doodstream(http_client, file_path)
        
        await status_msg.edit_text("🚀 Uploading to EarnVids (2/3)...")
        earn_url = await upload_earnvids(http_client, file_path)
        
        await status_msg.edit_text("🚀 Uploading to StreamHG (3/3)...")
        hg_url = await upload_streamhg(http_client, file_path)
        
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
    import http.server
    import socketserver
    import threading
    
    def run_dummy_server():
        port = int(os.getenv("PORT", "8000"))
        with socketserver.TCPServer(("", port), http.server.SimpleHTTPRequestHandler) as httpd:
            httpd.serve_forever()
            
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app.run()
