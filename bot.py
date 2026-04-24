import os
import re
import logging
import asyncio
import tempfile
import time
from collections import deque
from pathlib import Path
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

REQUEST_LIMIT  = 50
REQUEST_WINDOW = 3600
COOLDOWN_TIME  = 1800
user_timestamps: dict[int, deque] = {}
user_cooldowns:  dict[int, float] = {}

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
INSTAGRAM_COOKIES_RAW = os.environ.get("INSTAGRAM_COOKIES", "")
_COOKIES_FILE: str | None = None

INSTAGRAM_POST_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:reels?|p|tv)/([A-Za-z0-9_\-]+)'
)
INSTAGRAM_STORY_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/stories/([A-Za-z0-9_\.]+)/(\d+)'
)
FACEBOOK_URL_PATTERN = re.compile(
    r'https?://(?:www\.|m\.|web\.)?facebook\.com/(?:watch/?\?v=|[\w\-\.]+/videos/|share/[vr]/)[\d\w\-]+'
)

def _init_cookies() -> None:
    global _COOKIES_FILE
    if not INSTAGRAM_COOKIES_RAW:
        logger.warning("INSTAGRAM_COOKIES не встановлено")
        return
    path = "/tmp/instagram_cookies.txt"
    with open(path, "w") as f:
        f.write(INSTAGRAM_COOKIES_RAW)
    _COOKIES_FILE = path

def extract_url(text: str):
    for pattern, p, t in [
        (INSTAGRAM_STORY_PATTERN, "instagram", "story"),
        (INSTAGRAM_POST_PATTERN, "instagram", "post"),
        (FACEBOOK_URL_PATTERN, "facebook", "facebook")
    ]:
        m = pattern.search(text)
        if m:
            return (m.group(0), p, t)
    return None


# 🔥 ОНОВЛЕНА КОНВЕРТАЦІЯ
def convert_to_ios_compatible(input_path: str) -> str:
    import subprocess

    # ❗ НЕ конвертуємо mp4
    if input_path.endswith(".mp4"):
        return input_path

    output_path = str(
        Path(input_path).with_name(f"converted_{int(time.time()*1000)}.mp4")
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,

        "-c:v", "libx264",
        "-profile:v", "high",
        "-level", "4.0",
        "-pix_fmt", "yuv420p",
        "-preset", "fast",
        "-crf", "26",

        "-vf", "scale='min(1280,iw)':-2",

        "-c:a", "aac",
        "-b:a", "128k",

        "-movflags", "+faststart",

        output_path
    ]

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if result.returncode != 0:
        logger.error(f"FFmpeg error: {result.stderr.decode()[:300]}")
        return input_path

    if Path(output_path).exists():
        return output_path

    return input_path


def _download_instagram(url: str, output_dir: str):
    from gallery_dl import config as gdl_config, job as gdl_job

    gdl_config.clear()
    gdl_config.set((), "base-directory", output_dir)
    gdl_config.set((), "filename", "video.{extension}")

    if _COOKIES_FILE:
        gdl_config.set(("extractor",), "cookies", _COOKIES_FILE)

    try:
        gdl_job.DownloadJob(url).run()

        for f in Path(output_dir).rglob("*"):
            if f.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv"):
                return str(f)
        return None
    except Exception as e:
        logger.error(f"IG error: {e}")
        return None


def _download_facebook(url: str, output_dir: str):
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({
            "outtmpl": os.path.join(output_dir, "video.%(ext)s"),
            "format": "best[ext=mp4]/best",
            "quiet": True
        }) as ydl:
            ydl.download([url])

        for f in Path(output_dir).glob("video.*"):
            return str(f)
        return None
    except Exception as e:
        logger.error(f"FB error: {e}")
        return None


def download_media(url, output_dir, platform):
    if platform == "facebook":
        path = _download_facebook(url, output_dir)
    else:
        path = _download_instagram(url, output_dir)

    return (path, "video") if path else (None, "unknown")


async def keep_uploading_action(chat_id, bot):
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action="upload_video")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


def check_rate_limit(user_id):
    now = time.time()
    if user_id not in user_timestamps:
        user_timestamps[user_id] = deque()

    ts = user_timestamps[user_id]
    while ts and ts[0] < now - REQUEST_WINDOW:
        ts.popleft()

    if len(ts) >= REQUEST_LIMIT:
        return False, COOLDOWN_TIME // 60

    ts.append(now)
    return True, 0


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    url_info = extract_url(message.text)
    if not url_info:
        return

    media_url, platform, _ = url_info

    typing_task = asyncio.create_task(
        keep_uploading_action(message.chat_id, context.bot)
    )

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            media_path, _ = await asyncio.get_event_loop().run_in_executor(
                None, download_media, media_url, tmp_dir, platform
            )

            if not media_path or not Path(media_path).exists():
                await message.reply_text("Не вдалося завантажити.")
                return

            # 🔥 конвертація
            media_path = await asyncio.get_event_loop().run_in_executor(
                None, convert_to_ios_compatible, media_path
            )

            if not media_path or not Path(media_path).exists():
                await message.reply_text("Помилка обробки відео.")
                return

            size_mb = Path(media_path).stat().st_size / 1024 / 1024
            if size_mb > 50:
                await message.reply_text("Файл >50MB")
                return

            with open(media_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=message.chat_id,
                    video=f,
                    supports_streaming=True
                )

    finally:
        typing_task.cancel()


def create_application():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не встановлено!")

    _init_cookies()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app
