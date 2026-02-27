import os
import re
import logging
import asyncio
import tempfile
import random
import time

from collections import deque
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import yt_dlp

from instagram_client import download_instagram_media, is_available as instagrapi_available


# ──────────────────────────────────────────
# RATE LIMIT — per user
# ──────────────────────────────────────────
REQUEST_LIMIT = 50
REQUEST_WINDOW = 3600   # 1 година
COOLDOWN_TIME  = 1800   # 30 хв

user_timestamps: dict[int, deque] = {}
user_cooldowns:  dict[int, float] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────
# URL PATTERNS
# ──────────────────────────────────────────
INSTAGRAM_URL_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:reels?|p|tv)/[A-Za-z0-9_\-]+(?:/[^\s]*)?'
)
FACEBOOK_URL_PATTERN = re.compile(
    r'https?://(?:www\.|m\.|web\.)?facebook\.com/(?:watch/?\?v=|[\w\-\.]+/videos/|share/[vr]/)[\d\w\-]+'
)

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")


# ──────────────────────────────────────────
# USER AGENTS
# ──────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 12; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


# ──────────────────────────────────────────
# COOKIES — один раз при старті
# ──────────────────────────────────────────
_INSTAGRAM_COOKIES_FILE: str | None = None

def _init_cookies() -> None:
    global _INSTAGRAM_COOKIES_FILE
    instagram_cookies = os.environ.get("INSTAGRAM_COOKIES", "")
    if instagram_cookies:
        path = "/tmp/instagram_cookies.txt"
        with open(path, "w") as f:
            f.write(instagram_cookies)
        _INSTAGRAM_COOKIES_FILE = path
        logger.info("Instagram cookies loaded from environment")
    else:
        logger.warning("INSTAGRAM_COOKIES not set")


# ──────────────────────────────────────────
# URL EXTRACTOR
# ──────────────────────────────────────────
def extract_url(text: str) -> tuple[str, str] | None:
    for pattern, platform in [
        (INSTAGRAM_URL_PATTERN, "instagram"),
        (FACEBOOK_URL_PATTERN,  "facebook"),
    ]:
        match = pattern.search(text)
        if match:
            return (match.group(0), platform)
    return None


# ──────────────────────────────────────────
# DOWNLOADER
# ──────────────────────────────────────────
def download_media(url: str, output_dir: str, platform: str) -> tuple[str | None, str]:
    """
    Завантажує медіа через yt-dlp.
    Якщо Instagram повертає 'There is no video' — fallback на instagrapi.
    Returns: (filepath, 'video'|'photo'|'unknown')
    """
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    user_agent = random.choice(USER_AGENTS)

    base_opts = {
        "outtmpl":          output_template,
        "quiet":            False,
        "no_warnings":      False,
        "socket_timeout":   30,
        "retries":          10,
        "fragment_retries": 10,
        "max_filesize":     50 * 1024 * 1024,
        "http_headers": {"User-Agent": user_agent},
    }

    if platform == "instagram":
        ydl_opts = {
            **base_opts,
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
        }
        if _INSTAGRAM_COOKIES_FILE:
            ydl_opts["cookiefile"] = _INSTAGRAM_COOKIES_FILE
            logger.info("Instagram: Using cookies")

    elif platform == "facebook":
        ydl_opts = {
            **base_opts,
            "format": (
                "best[ext=mp4][height<=1080]/"
                "best[ext=mp4]/"
                "bestvideo[ext=mp4]+bestaudio/"
                "best"
            ),
            "merge_output_format": "mp4",
            "postprocessor_args": {"merger": ["-c", "copy"]},
            "http_headers": {
                "User-Agent":      user_agent,
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Sec-Fetch-Mode":  "navigate",
            },
        }
        logger.info("Facebook: Using web scraping mode")

    else:
        logger.error(f"Unknown platform: {platform}")
        return None, "unknown"

    try:
        logger.info(f"Downloading {platform} | URL: {url}")
        time.sleep(random.uniform(0.5, 2))

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                logger.error(f"{platform}: yt-dlp returned None")
                return None, "unknown"

            logger.info(f"Title: {info.get('title', '?')} | Duration: {info.get('duration', 0)}s")

            filename = ydl.prepare_filename(info)
            base = Path(filename).stem

            valid_video_exts = (".mp4", ".mov", ".mkv", ".webm")
            valid_photo_exts = (".jpg", ".jpeg", ".png", ".webp")
            all_exts = valid_video_exts + valid_photo_exts

            for f in Path(output_dir).iterdir():
                if f.stem == base and f.suffix.lower() in all_exts:
                    media_type = "photo" if f.suffix.lower() in valid_photo_exts else "video"
                    logger.info(f"Found [{media_type}]: {f.name} ({f.stat().st_size / 1024 / 1024:.2f} MB)")
                    return str(f), media_type

            # Fallback: перший медіа файл
            for f in Path(output_dir).iterdir():
                if f.suffix.lower() in all_exts:
                    media_type = "photo" if f.suffix.lower() in valid_photo_exts else "video"
                    logger.info(f"Fallback [{media_type}]: {f.name}")
                    return str(f), media_type

            logger.error("No media file found in output directory")
            return None, "unknown"

    except yt_dlp.utils.DownloadError as e:
        # yt-dlp не вміє качати фото пости/каруселі — передаємо instagrapi
        if "There is no video in this post" in str(e):
            logger.info("yt-dlp: photo/album post — switching to instagrapi fallback")
            return _instagrapi_fallback(url, output_dir)

        logger.error(f"DownloadError [{platform}]: {e}")
        return None, "unknown"

    except Exception as e:
        logger.error(f"Unexpected error [{platform}]: {e}", exc_info=True)
        return None, "unknown"


def _instagrapi_fallback(url: str, output_dir: str) -> tuple[str | None, str]:
    """Fallback на instagrapi для фото постів і каруселей."""
    if not instagrapi_available():
        logger.error(
            "instagrapi fallback not available. "
            "Set INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD in environment variables."
        )
        return None, "unknown"

    logger.info("Switching to instagrapi for photo/album download...")
    return download_instagram_media(url, output_dir)


# ──────────────────────────────────────────
# TYPING INDICATOR
# ──────────────────────────────────────────
async def keep_uploading_action(chat_id: int, bot, media_type: str = "video") -> None:
    action = "upload_photo" if media_type == "photo" else "upload_video"
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=action)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# ──────────────────────────────────────────
# RATE LIMIT
# ──────────────────────────────────────────
def check_rate_limit(user_id: int) -> tuple[bool, int]:
    now = time.time()

    cooldown = user_cooldowns.get(user_id, 0)
    if now < cooldown:
        return False, int((cooldown - now) / 60)

    if user_id not in user_timestamps:
        user_timestamps[user_id] = deque()

    timestamps = user_timestamps[user_id]
    while timestamps and timestamps[0] < now - REQUEST_WINDOW:
        timestamps.popleft()

    if len(timestamps) >= REQUEST_LIMIT:
        user_cooldowns[user_id] = now + COOLDOWN_TIME
        return False, COOLDOWN_TIME // 60

    timestamps.append(now)
    return True, 0


# ──────────────────────────────────────────
# HANDLER
# ──────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        try:
            await err.delete()
        except Exception:
            pass
        return

    media_url, platform = url_info
    logger.info(f"Processing {platform.upper()} | user_id={user_id} | {media_url}")

    typing_task = asyncio.create_task(
        keep_uploading_action(message.chat_id, context.bot, "video")
    )

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            media_path, media_type = await asyncio.get_event_loop().run_in_executor(
                None, download_media, media_url, tmp_dir, platform
            )

            # Не вдалось завантажити
            if not media_path or not Path(media_path).exists():
                logger.warning(f"Download failed [{platform}]: {media_url}")
                err = await message.reply_text(
                    f"Не вдалося завантажити з {platform.title()}.\n"
                    f"Можливо, контент приватний або недоступний.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try:
                    await err.delete()
                except Exception:
                    pass
                return

            # Файл завеликий
            if Path(media_path).stat().st_size > 50 * 1024 * 1024:
                err = await message.reply_text(
                    "Файл завеликий для відправки (понад 50 МБ).",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try:
                    await err.delete()
                except Exception:
                    pass
                return

            # Оновлюємо typing під реальний тип
            typing_task.cancel()
            typing_task = asyncio.create_task(
                keep_uploading_action(message.chat_id, context.bot, media_type)
            )

            # Відправка
            try:
                with open(media_path, "rb") as f:
                    if media_type == "photo":
                        await context.bot.send_photo(chat_id=message.chat_id, photo=f)
                        logger.info(f"Sent photo: {Path(media_path).name}")
                    else:
                        await context.bot.send_video(
                            chat_id=message.chat_id,
                            video=f,
                            supports_streaming=True
                        )
                        logger.info(f"Sent video: {Path(media_path).name}")

                try:
                    await message.delete()
                except Exception as e:
                    logger.warning(f"Could not delete original message: {e}")

            except Exception as e:
                logger.error(f"Send failed [{platform}]: {e}")
                err = await message.reply_text(
                    "Помилка при відправці. Спробуйте пізніше.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try:
                    await err.delete()
                except Exception:
                    pass
    finally:
        typing_task.cancel()


# ──────────────────────────────────────────
# APP FACTORY
# ──────────────────────────────────────────
def create_application() -> Application:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set!")

    _init_cookies()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Bot started | Platforms: Instagram (video + photo + album), Facebook")
    logger.info(f"yt-dlp cookies: {_INSTAGRAM_COOKIES_FILE is not None}")
    logger.info(f"instagrapi fallback: {instagrapi_available()}")

    return app
