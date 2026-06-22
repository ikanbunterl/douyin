import os
import sqlite3
import hashlib
import asyncio
import httpx
import re
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import yt_dlp
import json

# --- Konfigurasi ---
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
DB_NAME = "douyin_history.db"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "") # Isi dengan URL Webhook/Telegram jika ada

app = FastAPI(title="Douyin Downloader Web App")

# --- Database SQLite (Fitur Deduplikasi) ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS downloaded
                 (url_hash TEXT PRIMARY KEY, url TEXT, type TEXT, creator TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

def is_downloaded(url: str) -> bool:
    url_hash = hashlib.md5(url.encode()).hexdigest()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT 1 FROM downloaded WHERE url_hash=?", (url_hash,))
    exists = c.fetchone() is not None
    conn.close()
    return exists

def mark_downloaded(url: str, type: str, creator: str):
    url_hash = hashlib.md5(url.encode()).hexdigest()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO downloaded VALUES (?, ?, ?, ?, ?)",
              (url_hash, url, type, creator, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# --- Notifikasi Webhook ---
async def send_notification(message: str):
    if not WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(WEBHOOK_URL, json={"text": message}, timeout=5.0)
    except Exception as e:
        print(f"Gagal kirim notifikasi: {e}")

# --- Ekstraktor URL Pintar ---
def extract_url_from_text(text: str) -> str:
    """
    Otomatis mengekstrak URL murni dari teks share Douyin/TikTok.
    Contoh input: "4.33 复制打开抖音... https://v.douyin.com/xxxxx/ 08/27..."
    Output: "https://v.douyin.com/xxxxx/"
    """
    url_regex = r'(https?://[^\s]+)'
    match = re.search(url_regex, text)
    if match:
        return match.group(1)
    return text.strip()

# --- Logic Download (yt-dlp) ---
def get_safe_filename(text):
    return "".join([c for c in text if c.isalpha() or c.isdigit() or c in ' _-']).rstrip()

async def download_task(url: str, download_type: str, cookies: str = None):
    print(f"Memulai download: {url}")
    
    # Persiapan folder (Auto-Organize)
    creator_folder = DOWNLOAD_DIR / "Unknown_Creator"
    creator_folder.mkdir(parents=True, exist_ok=True)
    
    # Konfigurasi yt-dlp
    ydl_opts = {
        'outtmpl': str(creator_folder / '%(uploader)s/%(title).50s_[%(id)s].%(ext)s'),
        'format': 'best',
        'noplaylist': True,
        'quiet': False,
    }

    if download_type == 'audio':
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })

    if cookies:
        cookie_file = "temp_cookies.txt"
        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write(cookies)
        ydl_opts['cookiefile'] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            creator = get_safe_filename(info.get('uploader', 'Unknown'))
            title = info.get('title', 'Unknown')
            
            # Pindahkan ke folder kreator yang benar (Auto-Organize)
            final_dir = DOWNLOAD_DIR / creator
            final_dir.mkdir(exist_ok=True)
            
            mark_downloaded(url, download_type, creator)
            
            msg = f"✅ Download Selesai!\nKreator: {creator}\nJudul: {title}\nTipe: {download_type}"
            await send_notification(msg)
            print(msg)
            
    except Exception as e:
        err_msg = f"❌ Gagal Download {url}\nError: {str(e)}"
        await send_notification(err_msg)
        print(err_msg)
    finally:
        if cookies and os.path.exists("temp_cookies.txt"):
            os.remove("temp_cookies.txt")

# --- API Endpoints ---
class DownloadRequest(BaseModel):
    url: str
    type: str = "video" # video, audio, images
    cookies: str = ""

@app.post("/api/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    # Ekstrak URL murni dari teks share
    clean_url = extract_url_from_text(req.url)
    
    if is_downloaded(clean_url):
        raise HTTPException(status_code=400, detail="URL ini sudah pernah didownload sebelumnya (Deduplikasi aktif).")
    
    # Jalankan di background agar web tidak nge-freeze
    background_tasks.add_task(download_task, clean_url, req.type, req.cookies)
    return {"status": "success", "message": f"Download dimulai di background untuk: {clean_url}"}

@app.get("/api/status")
async def get_status():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM downloaded")
    total = c.fetchone()[0]
    conn.close()
    return {"total_downloaded": total, "status": "running"}

@app.get("/", response_class=HTMLResponse)
async def read_index():
    return FileResponse("index.html")

if __name__ == "__main__":
    init_db()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5500)