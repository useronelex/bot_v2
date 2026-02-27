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


# ──────────────────────────────────────────
# RATE LIMIT — per user (не глобальний!)
# ──────────────────────────────────────────
REQUEST_LIMIT = 50       # максимум запитів на юзера
REQUEST_WINDOW = 3600    # за 1 годину (секунди)
COOLDOWN_TIME  = 1800    # відпочинок 30 хв (секунди)

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
# Cookies — тільки Instagram
# ──────────────────────────────────────────
_INSTAGRAM_COOKIES_FILE: str | None = None

def _init_cookies() -> None:
    """Записує cookies у файл один раз при старті."""
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
def extract_video_url(text: str) -> tuple[str, str] | None:
    for pattern, platform in [
        (INSTAGRAM_URL_PATTERN, "instagram"),
        (FACEBOOK_URL_PATTERN,  "facebook"),
    ]:
        match = pattern.search(text)
        if match:
            return (match.group(0), platform)
    return None


# ──────────────────────────────────────────
# DOWNLOADER - підтримує відео та фото
# ──────────────────────────────────────────
def download_media(url: str, output_dir: str, platform: str) -> tuple[str | None, str]:
    """
    Завантажує відео або фото.
    Returns: (filepath, media_type) де media_type = 'video' або 'photo'
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
        "http_headers": {
            "User-Agent": user_agent,
        },
    }

    if platform == "instagram":
        ydl_opts = {
            **base_opts,
            # ✅ ФІКС: беремо готовий mp4 без злиття потоків
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
            # Дозволяємо завантажувати фото
            "writethumbnail": False,  # Не треба окремо thumbnail
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
            "postprocessor_args": {
                "merger": ["-c", "copy"],
            },
            "http_headers": {
                "User-Agent":    user_agent,
                "Accept":        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Sec-Fetch-Mode": "navigate",
            },
        }
        logger.info("Facebook: Using web scraping mode")

    else:
        ydl_opts = {
            **base_opts,
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
        }

    try:
        logger.info(f"Downloading {platform} media with yt-dlp...")
        logger.info(f"URL: {url}")

        time.sleep(random.uniform(0.5, 2))

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                logger.error(f"{platform}: yt-dlp returned None")
                return None, 'unknown'

            logger.info(f"Title: {info.get('title', 'Unknown')}")
            
            # Визначаємо тип медіа
            media_type = 'video'
            
            # Перевірка чи це фото (Instagram posts можуть бути фото)
            if platform == "instagram":
                # Якщо немає відео кодека - це фото
                if info.get('vcodec') == 'none' or not info.get('vcodec'):
                    media_type = 'photo'
                    logger.info("Detected: Photo/Image")
                # Якщо дуже коротке і немає звуку - теж може бути фото
                elif info.get('duration', 0) == 0 and info.get('acodec') == 'none':
                    media_type = 'photo'
                    logger.info("Detected: Static image")
                else:
                    logger.info(f"Detected: Video (duration: {info.get('duration', 0)}s)")

            filename = ydl.prepare_filename(info)
            base = Path(filename).stem

            # Шукаємо файл (відео або фото)
            valid_video_exts = (".mp4", ".mov", ".mkv", ".webm")
            valid_photo_exts = (".jpg", ".jpeg", ".png", ".webp")
            valid_exts = valid_video_exts + valid_photo_exts

            for f in Path(output_dir).iterdir():
                if f.stem == base and f.suffix in valid_exts:
                    size_mb = f.stat().st_size / 1024 / 1024
                    logger.info(f"Found: {f.name} ({size_mb:.2f} MB)")
                    
                    # Уточнюємо тип по розширенню
                    if f.suffix in valid_photo_exts:
                        media_type = 'photo'
                    
                    return str(f), media_type

            # Fallback: перший медіа файл
            for f in Path(output_dir).iterdir():
                if f.suffix in valid_exts:
                    logger.info(f"Fallback file: {f.name}")
                    if f.suffix in valid_photo_exts:
                        media_type = 'photo'
                    return str(f), media_type

            logger.error("No media file found in output directory")
            return None, 'unknown'

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp DownloadError [{platform}]: {e}")
        return None, 'unknown'
    except Exception as e:
        logger.error(f"yt-dlp error [{platform}]: {e}", exc_info=True)
        return None, 'unknown'


# ──────────────────────────────────────────
# TYPING INDICATOR — постійний під час завантаження
# ──────────────────────────────────────────
async def keep_uploading_action(chat_id: int, bot, media_type: str = 'video') -> None:
    """Надсилає typing action кожні 4 секунди поки не скасовано."""
    action = "upload_video" if media_type == 'video' else "upload_photo"
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=action)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# ──────────────────────────────────────────
# RATE LIMIT — per user
# ──────────────────────────────────────────
def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """
    Перевіряє ліміт для конкретного юзера.
    Returns: (allowed, cooldown_remaining_minutes)
    """
    now = time.time()

    # Перевірка cooldown
    cooldown = user_cooldowns.get(user_id, 0)
    if now < cooldown:
        remaining = int((cooldown - now) / 60)
        return False, remaining

    # Очищаємо старі записи
    if user_id not in user_timestamps:
        user_timestamps[user_id] = deque()
    timestamps = user_timestamps[user_id]
    while timestamps and timestamps[0] < now - REQUEST_WINDOW:
        timestamps.popleft()

    # Перевірка ліміту
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

    video_info = extract_video_url(message.text)
    if not video_info:
        return

    user_id = message.from_user.id
    allowed, cooldown_mins = check_rate_limit(user_id)

    if not allowed:
        err = await message.reply_text(
            f"⏳ Ти надіслав забагато запитів. Спробуй через {cooldown_mins} хв.",
            reply_to_message_id=message.message_id
        )
        await asyncio.sleep(10)
        try:
            await err.delete()
        except Exception:
            pass
        return

    video_url, platform = video_info
    logger.info(f"Processing {platform.upper()} URL: {video_url} | user_id={user_id}")

    # Запускаємо typing indicator (поки не знаємо тип медіа, ставимо video)
    typing_task = asyncio.create_task(
        keep_uploading_action(message.chat_id, context.bot, 'video')
    )

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            media_path, media_type = await asyncio.get_event_loop().run_in_executor(
                None, download_media, video_url, tmp_dir, platform
            )

            if not media_path or not Path(media_path).exists():
                logger.warning(f"Failed to download {platform}: {video_url}")
                err = await message.reply_text(
                    f"❌ Не вдалося завантажити з {platform.title()}.\n"
                    f"Можливо, контент приватний або недоступний.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try:
                    await err.delete()
                except Exception:
                    pass
                return

            file_size = Path(media_path).stat().st_size
            if file_size > 50 * 1024 * 1024:
                err = await message.reply_text(
                    f"❌ Файл завеликий для відправки (понад 50 МБ).",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try:
                    await err.delete()
                except Exception:
                    pass
                return

            # Оновлюємо typing action відповідно до типу медіа
            typing_task.cancel()
            typing_task = asyncio.create_task(
                keep_uploading_action(message.chat_id, context.bot, media_type)
            )

            try:
                with open(media_path, "rb") as media_file:
                    if media_type == 'photo':
                        # Надсилаємо як фото
                        await context.bot.send_photo(
                            chat_id=message.chat_id,
                            photo=media_file
                        )
                        logger.info(f"Sent as photo: {Path(media_path).name}")
                    else:
                        # Надсилаємо як відео
                        await context.bot.send_video(
                            chat_id=message.chat_id,
                            video=media_file,
                            supports_streaming=True
                        )
                        logger.info(f"Sent as video: {Path(media_path).name}")
                
                try:
                    await message.delete()
                except Exception as e:
                    logger.warning(f"Could not delete original message: {e}")

            except Exception as e:
                logger.error(f"Failed to send media [{platform}]: {e}")
                err = await message.reply_text(
                    f"❌ Помилка при відправці. Спробуйте пізніше.",
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

    # Ініціалізуємо cookies один раз при старті
    _init_cookies()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Bot application created")
    logger.info("Supported platforms: Instagram (video + photo), Facebook")
    logger.info(f"Instagram cookies: {_INSTAGRAM_COOKIES_FILE is not None}")

    return app
