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
        filename = os.path.basename(file_path)
        res = await client.get(f"https://doodapi.com/api/upload/server?key={DOODSTREAM_KEY}")
        upload_url = res.json().get("result")
        if upload_url:
            with open(file_path, "rb") as f:
                upload_res = await client.post(
                    upload_url, 
                    files={"file": (filename, f)}, 
                    data={"api_key": DOODSTREAM_KEY},
                    timeout=None
                )
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
        filename = os.path.basename(file_path)
        res = await client.get(f"https://earnvids.com/api/upload/server?key={EARNVIDS_KEY}")
        upload_url = res.json().get("result")
        if upload_url:
            with open(file_path, "rb") as f:
                upload_res = await client.post(
                    upload_url, 
                    files={"file": (filename, f)}, 
                    data={"api_key": EARNVIDS_KEY},
                    timeout=None
                )
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
        filename = os.path.basename(file_path)
        res = await client.get(f"https://streamwish.com/api/upload/server?key={STREAMHG_KEY}")
        upload_url = res.json().get("result")
        if upload_url:
            with open(file_path, "rb") as f:
                upload_res = await client.post(
                    upload_url, 
                    files={"file": (filename, f)}, 
                    data={"api_key": STREAMHG_KEY},
                    timeout=None
                )
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
    
    async with httpx.AsyncClient(timeout=None) as http_client:
        # Step 1: Doodstream
        await status_msg.edit_text("🚀 Uploading to Doodstream (1/3)...")
        dood_url = await upload_doodstream(http_client, file_path)
        
        # Step 2: EarnVids
        await status_msg.edit_text("🚀 Uploading to EarnVids (2/3)...")
        earn_url = await upload_earnvids(http_client, file_path)
        
        # Step 3: StreamHG
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
    # Bypass Render Port Health check
    import http.server
    import socketserver
    import threading
    
    def run_dummy_server():
        port = int(os.getenv("PORT", "8000"))
        with socketserver.TCPServer(("", port), http.server.SimpleHTTPRequestHandler) as httpd:
            httpd.serve_forever()
            
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app.run()
