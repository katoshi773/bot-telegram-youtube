import os
import yt_dlp
import requests
import asyncio
import logging
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackContext
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image
from io import BytesIO

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ambil token bot dari environment variable
TOKEN = os.getenv('TELEGRAM_TOKEN')

# Fungsi untuk mengunduh lagu dari YouTube dalam format M4A
def download_song(url_or_query):
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": "downloads/%(title)s.%(ext)s",
        "quiet": True,
        "noplaylist": False,
        "socket_timeout": 600,
    }

    filenames = []
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url_or_query, download=True)
        except yt_dlp.utils.DownloadError as e:
            logger.warning(f"Ignored yt-dlp Error: {str(e)}")
            return []

        if "entries" in info:
            for entry in info["entries"]:
                filename = ydl.prepare_filename(entry).replace(".webm", ".m4a").replace(".opus", ".m4a")
                filenames.append((filename, entry))
        else:
            filename = ydl.prepare_filename(info).replace(".webm", ".m4a").replace(".opus", ".m4a")
            filenames.append((filename, info))

    return filenames

# Fungsi untuk mendapatkan cover album yang dipotong jadi rasio 1:1 (tanpa resize)
def get_cropped_cover(cover_url):
    response = requests.get(cover_url)
    if response.status_code == 200:
        img = Image.open(BytesIO(response.content))

        # Konversi WebP ke JPEG jika diperlukan
        if img.format == "WEBP":
            img = img.convert("RGB")

        # Crop agar menjadi rasio 1:1 (square)
        width, height = img.size
        min_dim = min(width, height)
        left = (width - min_dim) // 2
        top = (height - min_dim) // 2
        right = (width + min_dim) // 2
        bottom = (height + min_dim) // 2
        img = img.crop((left, top, right, bottom))

        # Simpan ke buffer sebagai JPEG
        jpeg_buffer = BytesIO()
        img.save(jpeg_buffer, format="JPEG")

        return jpeg_buffer.getvalue()
    return None

# Fungsi untuk menambahkan metadata dengan format "Artis『Judul』"
def add_metadata(filename, info):
    title = info.get("title", "Unknown Title")
    artist = info.get("uploader", "Unknown Artist")

    # Jika judul dalam format "Artis『Judul』", pisahkan
    if "『" in title and "』" in title:
        split_title = title.split("『")
        if len(split_title) == 2:
            artist = split_title[0].strip()
            title = split_title[1].replace("』", "").strip()

    # Hapus kata-kata tertentu dari nama artis
    artist = re.sub(r"\b(Official|YouTube Channel|VEVO)\b", "", artist, flags=re.IGNORECASE).strip()

    album = info.get("album", "Unknown Album")
    cover_url = info.get("thumbnail", None)

    if os.path.exists(filename):
        audio_file = MP4(filename)
        audio_file.tags["\xa9ART"] = artist  # Artis
        audio_file.tags["\xa9nam"] = title  # Judul
        audio_file.tags["\xa9alb"] = album  # Album

        if cover_url:
            cover_data = get_cropped_cover(cover_url)
            if cover_data:
                audio_file.tags["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

        audio_file.save()

    return filename

# Fungsi untuk menangani pesan dengan link YouTube
async def handle_message(update: Update, context: CallbackContext):
    text = update.message.text

    if "youtube.com" in text or "youtu.be" in text:
        await update.message.reply_text("🔽 Mengunduh lagu dari YouTube...")
        asyncio.create_task(process_download(update, text))
    else:
        await update.message.reply_text("⚠️ Kirimkan link lagu dari YouTube.")

# Fungsi untuk memproses unduhan tanpa memblokir pengguna lain
async def process_download(update: Update, url):
    files_info = []

    try:
        files_info = download_song(url)

        if not files_info:
            raise Exception("File yang diunduh tidak ditemukan!")

        tasks = []
        for filename, info in files_info:
            final_filename = add_metadata(filename, info)
            tasks.append(send_audio(update, final_filename, info))

        await asyncio.gather(*tasks)

    except (asyncio.TimeoutError, Exception) as e:
        error_message = str(e).lower().strip()

        # List error yang akan diabaikan
        ignored_errors = [
            r"unable to rename file",
            r"postprocessing: error opening output files",
            r"timed out",
            r"no such file or directory",
            r"error opening output files",
            r"socket timeout",
        ]

        # Jika error cocok dengan daftar di atas, hanya log tanpa mengirim ke Telegram
        if any(re.search(err, error_message) for err in ignored_errors):
            logger.warning(f"Ignored Error: {error_message}")
        else:
            logger.error(f"Terjadi kesalahan: {error_message}")
            await update.message.reply_text(f"❌ Terjadi kesalahan: {error_message}")

    finally:
        if files_info:
            for filename, _ in files_info:
                if os.path.exists(filename):
                    os.remove(filename)

# Fungsi untuk mengirim file audio
async def send_audio(update: Update, filename, info):
    with open(filename, "rb") as audio:
        await update.message.reply_audio(audio=audio, title=info["title"], performer=info["uploader"])

# Fungsi utama untuk menjalankan bot
def main():
    # Ambil token bot dari environment variable
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", lambda update, context: update.message.reply_text("Halo! Saya bot downloader YouTube.")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ Bot sedang berjalan...")
    app.run_polling()

if __name__ == "__main__":
    main()