import os
import yt_dlp
import requests
import asyncio
import logging
import re
import base64
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackContext
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image
from io import BytesIO

# ======================== LOGGING ======================== #
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ======================== TOKEN ======================== #
TOKEN = os.getenv('TELEGRAM_TOKEN')

# ======================== COOKIES ======================== #
def save_cookies_to_file():
    """Simpan cookies dari environment variable (base64) ke file sementara"""
    cookies_base64 = os.getenv('YOUTUBE_COOKIES_BASE64')
    cookies_path = "/tmp/cookies.txt"

    if cookies_base64:
        try:
            with open(cookies_path, "wb") as f:
                f.write(base64.b64decode(cookies_base64))
            if os.path.exists(cookies_path):
                logger.info("✅ Cookies berhasil disimpan ke /tmp/cookies.txt")
                return cookies_path
        except Exception as e:
            logger.error(f"❌ Gagal menyimpan cookies: {str(e)}")
    else:
        logger.warning("⚠️ Environment variable 'YOUTUBE_COOKIES_BASE64' tidak ditemukan!")

    return None


# ======================== DOWNLOAD SONG ======================== #
def download_song(url_or_query):
    """Download lagu dari YouTube dalam format M4A menggunakan yt-dlp"""
    cookies_path = save_cookies_to_file()

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": "/tmp/%(title)s.%(ext)s",
        "quiet": False,    # Set ke False untuk debugging
        "noplaylist": False,
        "socket_timeout": 600,
    }

    # Gunakan cookies jika tersedia
    if cookies_path and os.path.exists(cookies_path):
        logger.info(f"✅ Menggunakan cookies dari: {cookies_path}")
        ydl_opts["cookiefile"] = cookies_path
    else:
        logger.warning("⚠️ Download tanpa cookies!")

    filenames = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url_or_query, download=True)
        except yt_dlp.utils.DownloadError as e:
            logger.warning(f"❌ yt-dlp Error: {str(e)}")
            return []

        if "entries" in info:
            for entry in info["entries"]:
                filename = ydl.prepare_filename(entry).replace(".webm", ".m4a").replace(".opus", ".m4a")
                filenames.append((filename, entry))
        else:
            filename = ydl.prepare_filename(info).replace(".webm", ".m4a").replace(".opus", ".m4a")
            filenames.append((filename, info))

    return filenames


# ======================== CROP COVER ======================== #
def get_cropped_cover(cover_url):
    """Ambil cover album yang dipotong menjadi rasio 1:1"""
    response = requests.get(cover_url)
    if response.status_code == 200:
        img = Image.open(BytesIO(response.content))

        # Konversi WEBP ke JPEG jika perlu
        if img.format == "WEBP":
            img = img.convert("RGB")

        # Crop ke rasio 1:1
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


# ======================== ADD METADATA ======================== #
def add_metadata(filename, info):
    """Tambahkan metadata ke file audio dalam format M4A"""
    title = info.get("title", "Unknown Title")
    artist = info.get("uploader", "Unknown Artist")

    # Format judul "Artis『Judul』"
    if "『" in title and "』" in title:
        split_title = title.split("『")
        if len(split_title) == 2:
            artist = split_title[0].strip()
            title = split_title[1].replace("』", "").strip()

    # Bersihkan nama artis
    artist = re.sub(r"\b(Official|YouTube Channel|VEVO)\b", "", artist, flags=re.IGNORECASE).strip()

    album = info.get("album", "Unknown Album")
    cover_url = info.get("thumbnail", None)

    if os.path.exists(filename):
        audio_file = MP4(filename)
        audio_file.tags["\xa9ART"] = artist  # Artis
        audio_file.tags["\xa9nam"] = title   # Judul
        audio_file.tags["\xa9alb"] = album   # Album

        # Tambahkan cover jika tersedia
        if cover_url:
            cover_data = get_cropped_cover(cover_url)
            if cover_data:
                audio_file.tags["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

        audio_file.save()

    return filename


# ======================== TELEGRAM HANDLER ======================== #
async def handle_message(update: Update, context: CallbackContext):
    """Handler untuk pesan yang berisi link YouTube"""
    text = update.message.text

    if "youtube.com" in text or "youtu.be" in text:
        await update.message.reply_text("🔽 Mengunduh lagu dari YouTube...")
        asyncio.create_task(process_download(update, text))
    else:
        await update.message.reply_text("⚠️ Kirimkan link lagu dari YouTube.")


async def process_download(update: Update, url):
    """Proses download lagu tanpa memblokir pengguna lain"""
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

        ignored_errors = [
            r"unable to rename file",
            r"postprocessing: error opening output files",
            r"timed out",
            r"no such file or directory",
            r"error opening output files",
            r"socket timeout",
        ]

        if any(re.search(err, error_message) for err in ignored_errors):
            logger.warning(f"Ignored Error: {error_message}")
        else:
            logger.error(f"❌ Terjadi kesalahan: {error_message}")
            await update.message.reply_text(f"❌ Terjadi kesalahan: {error_message}")

    finally:
        if files_info:
            for filename, _ in files_info:
                if os.path.exists(filename):
                    os.remove(filename)


async def send_audio(update: Update, filename, info):
    """Kirim file audio ke pengguna Telegram"""
    with open(filename, "rb") as audio:
        await update.message.reply_audio(audio=audio, title=info.get("title"), performer=info.get("uploader"))


# ======================== MAIN ======================== #
def main():
    """Fungsi utama untuk menjalankan bot Telegram"""
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", lambda update, context: update.message.reply_text("Halo! Saya bot downloader YouTube.")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ Bot sedang berjalan...")
    app.run_polling()


if __name__ == "__main__":
    main()