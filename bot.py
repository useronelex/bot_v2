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

# ──────────────────────────────────────────
# RATE LIMIT
# ──────────────────────────────────────────
REQUEST_LIMIT  = 50
REQUEST_WINDOW = 3600
COOLDOWN_TIME  = 1800
user_timestamps: dict[int, deque] = {}
user_cooldowns:  dict[int, float] = {}

# ──────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
INSTAGRAM_COOKIES_RAW = os.environ.get("INSTAGRAM_COOKIES", "")
_COOKIES_FILE: str | None = None

# ──────────────────────────────────────────
# URL PATTERNS
# ──────────────────────────────────────────
INSTAGRAM_POST_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:reels?|p|tv)/([A-Za-z0-9_\-]+)(?:/[^\s]*)?'
)
INSTAGRAM_STORY_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/stories/([A-Za-z0-9_\.]+)/(\d+)(?:/[^\s]*)?'
)
FACEBOOK_URL_PATTERN = re.compile(
    r'https?://(?:www\.|m\.|web\.)?facebook\.com/(?:watch/?\?v=|[\w\-\.]+/videos/|share/[vr]/)[\d\w\-]+'
)

# ──────────────────────────────────────────
# COOKIES INIT
# ──────────────────────────────────────────
def _init_cookies() -> None:
    global _COOKIES_FILE
    if not INSTAGRAM_COOKIES_RAW:
        logger.warning("INSTAGRAM_COOKIES не встановлено")
        return
    path = "/tmp/instagram_cookies.txt"
    with open(path, "w") as f:
        f.write(INSTAGRAM_COOKIES_RAW)
    _COOKIES_FILE = path

# ──────────────────────────────────────────
# URL EXTRACTOR
# ──────────────────────────────────────────
def extract_url(text: str) -> tuple[str, str, str] | None:
    match = INSTAGRAM_STORY_PATTERN.search(text)
    if match:
        return (match.group(0), "instagram", "story")
    match = INSTAGRAM_POST_PATTERN.search(text)
    if match:
        return (match.group(0), "instagram", "post")
    match = FACEBOOK_URL_PATTERN.search(text)
    if match:
        return (match.group(0), "facebook", "facebook")
    return None


# ──────────────────────────────────────────
#  FIX ДЛЯ iPHONE
# ──────────────────────────────────────────
def convert_to_ios_compatible(input_path: str) -> str:
    import subprocess

    output_path = str(Path(input_path).with_name(f"fixed_{int(time.time()*1000)}.mp4"))

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,

        # ❗ БЕЗ перекодування
        "-c", "copy",

        # 🔥 критично для iPhone / Telegram
        "-movflags", "+faststart",

        output_path
    ]

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if result.returncode != 0:
        logger.error(f"FFmpeg remux error: {result.stderr.decode()[:300]}")
        return input_path

    return output_path if Path(output_path).exists() else input_path


# ──────────────────────────────────────────
# INSTAGRAM
# ──────────────────────────────────────────
def _download_instagram(url: str, output_dir: str) -> str | None:
    from gallery_dl import config as gdl_config, job as gdl_job

    gdl_config.clear()
    gdl_config.set((), "base-directory", output_dir)
    gdl_config.set((), "directory", [])
    gdl_config.set((), "filename", "video.{extension}")

    if _COOKIES_FILE:
        gdl_config.set(("extractor",), "cookies", _COOKIES_FILE)

    try:
        gdl_job.DownloadJob(url).run()

        for f in Path(output_dir).rglob("*"):
            if f.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv") and f.stat().st_size > 0:
                return str(f)
        return None
    except Exception as e:
        logger.error(f"IG error: {e}")
        return None


# ──────────────────────────────────────────
# FACEBOOK
# ──────────────────────────────────────────
def _download_facebook(url: str, output_dir: str) -> str | None:
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({
            "outtmpl": os.path.join(output_dir, "video.%(ext)s"),
            "format": "best[ext=mp4]/best",
            "quiet": True,
        }) as ydl:
            ydl.download([url])

        for f in Path(output_dir).glob("video.*"):
            return str(f)
        return None
    except Exception as e:
        logger.error(f"FB error: {e}")
        return None


# ──────────────────────────────────────────
# DISPATCH
# ──────────────────────────────────────────
def download_media(url: str, output_dir: str, platform: str):
    if platform == "facebook":
        path = _download_facebook(url, output_dir)
    else:
        path = _download_instagram(url, output_dir)

    return (path, "video") if path else (None, "unknown")


# ──────────────────────────────────────────
# TYPING
# ──────────────────────────────────────────
async def keep_uploading_action(chat_id: int, bot):
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action="upload_video")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# ──────────────────────────────────────────
# RATE LIMIT
# ──────────────────────────────────────────
def check_rate_limit(user_id: int):
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


# ──────────────────────────────────────────
# HANDLER
# ──────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    url_info = extract_url(message.text)
    if not url_info:
        return

    user_id = message.from_user.id
    allowed, cooldown_mins = check_rate_limit(user_id)
    if not allowed:
        err = await message.reply_text(
            f"Забагато запитів. Спробуй через {cooldown_mins} хв.",
            reply_to_message_id=message.message_id
        )
        await asyncio.sleep(10)
        try: await err.delete()
        except Exception: pass
        return

    media_url, platform, content_type = url_info

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

            # 🔥 ЄДИНА НОВА ЛІНІЯ
            media_path = await asyncio.get_event_loop().run_in_executor(
                None, convert_to_ios_compatible, media_path
            )

            size_mb = Path(media_path).stat().st_size / 1024 / 1024
            if size_mb > 50:
                await message.reply_text("Файл >50MB")
                return

            sent = False
            for _ in range(3):
                try:
                    with open(media_path, "rb") as f:
                        await context.bot.send_video(
                            chat_id=message.chat_id,
                            video=f,
                            supports_streaming=True,
                        )
                    sent = True
                    break
                except:
                    await asyncio.sleep(3)

            if sent:
                try: await message.delete()
                except Exception: pass

    finally:
        typing_task.cancel()


def create_application():
    _init_cookies()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
