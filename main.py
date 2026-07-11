import os
import asyncio
import httpx
from pyrogram import Client, filters
from pyrogram.types import Message

# Environment variables
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

DOODSTREAM_KEY = os.getenv("DOODSTREAM_API_KEY", "")
EARNVIDS_KEY = os.getenv("EARNVIDS_API_KEY", "")
STREAMHG_KEY = os.getenv("STREAMHG_API_KEY", "")

app = Client("multi_uploader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

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
        
        try:
            res_data = res.json()
        except Exception:
            return f"API Error (HTTP {res.status_code})"

        upload_url = res_data.get("result")
        if not upload_url:
            return f"Server Error: {res_data.get('msg', 'No Upload URL')}"

        with open(file_path, "rb") as f:
            upload_res = await client.post(
                upload_url, 
                files={"file": (filename, f, "application/octet-stream")}, 
                data={"api_key": DOODSTREAM_KEY},
                headers=HEADERS,
                timeout=None
            )
            
            try:
                upload_data = upload_res.json()
                result = upload_data.get("result", [])
                if isinstance(result, list) and len(result) > 0:
                    file_code = result[0].get("filecode") or result[0].get("file_code")
                    if file_code:
                        return f"https://dood.to/d/{file_code}"
                return f"Upload Failed: {upload_data.get('msg', 'Unknown Error')}"
            except Exception:
                return "Upload Response Invalid"
    except Exception as e:
        return f"Error: {type(e).__name__}"

async def upload_earnvids(client: httpx.AsyncClient, file_path: str):
    if not EARNVIDS_KEY:
        return "Key Missing ⚠️"
    try:
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        url = f"https://earnvids.com/api/upload/server?key={EARNVIDS_KEY}"
        res = await client.get(url, headers=HEADERS)
        
        try:
            res_data = res.json()
        except Exception:
            return f"API Error (HTTP {res.status_code})"

        upload_url = res_data.get("result")
        if not upload_url:
            return f"Server Error: {res_data.get('msg', 'No Upload URL')}"

        data = {
            "api_key": EARNVIDS_KEY,
            "key": EARNVIDS_KEY,
            "file_size": str(file_size)
        }

        with open(file_path, "rb") as f:
            files = {"file": (filename, f, "application/octet-stream")}
            upload_res = await client.post(
                f"{upload_url}?key={EARNVIDS_KEY}", 
                files=files, 
                data=data,
                headers=HEADERS,
                timeout=None
            )
            
            try:
                upload_data = upload_res.json()
                result = upload_data.get("result", [])
                if isinstance(result, list) and len(result) > 0:
                    file_code = result[0].get("filecode") or result[0].get("file_code")
                    if file_code:
                        return f"https://earnvids.com/d/{file_code}"
                elif isinstance(result, dict):
                    file_code = result.get("filecode") or result.get("file_code")
                    if file_code:
                        return f"https://earnvids.com/d/{file_code}"
                return f"Upload Error: {upload_data}"
            except Exception:
                return f"Response Parse Error: {upload_res.text[:100]}"
    except Exception as e:
        return f"Error: {type(e).__name__}"

async def upload_streamhg(client: httpx.AsyncClient, file_path: str):
    if not STREAMHG_KEY:
        return "Key Missing ⚠️"
    try:
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        url = f"https://streamwish.com/api/upload/server?key={STREAMHG_KEY}"
        res = await client.get(url, headers=HEADERS)
        
        try:
            res_data = res.json()
        except Exception:
            return f"API Error (HTTP {res.status_code})"

        upload_url = res_data.get("result")
        if not upload_url:
            return f"Server Error: {res_data.get('msg', 'No Upload URL')}"

        data = {
            "api_key": STREAMHG_KEY,
            "key": STREAMHG_KEY,
            "file_size": str(file_size)
        }

        with open(file_path, "rb") as f:
            files = {"file": (filename, f, "application/octet-stream")}
            upload_res = await client.post(
                f"{upload_url}?key={STREAMHG_KEY}", 
                files=files, 
                data=data,
                headers=HEADERS,
                timeout=None
            )
            
            try:
                upload_data = upload_res.json()
                files_res = upload_data.get("files", upload_data.get("result", []))
                if isinstance(files_res, list) and len(files_res) > 0:
                    file_code = files_res[0].get("filecode") or files_res[0].get("file_code")
                    if file_code:
                        return f"https://streamwish.com/f/{file_code}"
                return f"Upload Error: {upload_data}"
            except Exception:
                return f"Response Parse Error: {upload_res.text[:100]}"
    except Exception as e:
        return f"Error: {type(e).__name__}"

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
        "✅ **Download Links Ready!**\n\n"
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

