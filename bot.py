import os
import re
import logging
import asyncio
import tempfile
import random

from collections import deque
import time

from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import yt_dlp


request_timestamps = deque()
REQUEST_LIMIT = 60  # максимум запитів
REQUEST_WINDOW = 3600  # за 1 годину (секунди)
COOLDOWN_TIME = 1800  # відпочинок 30 хв (секунди)
cooldown_until = 0  # час до якого бот відпочиває

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# URL patterns для різних платформ
INSTAGRAM_URL_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:reels?|p|tv)/[A-Za-z0-9_\-]+(?:/[^\s]*)?'
)

TIKTOK_URL_PATTERN = re.compile(
    r'https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/(?:@[\w\.-]+/video/\d+|v/\d+\.html|[\w\-]+)(?:/[^\s]*)?'
)

YOUTUBE_URL_PATTERN = re.compile(
    r'https?://(?:www\.|music\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w\-]+'
)

FACEBOOK_URL_PATTERN = re.compile(
    r'https?://(?:www\.|m\.|web\.)?facebook\.com/(?:watch/?\?v=|[\w\-\.]+/videos/|share/v/)[\d\w\-]+'
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

# Ротація User-Agent для обходу блокувань
USER_AGENTS = [
    # Mobile
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 12; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Mobile Safari/537.36",
    # Desktop (для YouTube, Facebook)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def extract_video_url(text: str) -> tuple[str, str] | None:
    """
    Extract video URL from text.
    Returns: (url, platform) or None
    platform: 'instagram', 'tiktok', 'youtube', 'facebook'
    """
    # Instagram
    instagram_match = INSTAGRAM_URL_PATTERN.search(text)
    if instagram_match:
        return (instagram_match.group(0), 'instagram')
    
    # TikTok
    tiktok_match = TIKTOK_URL_PATTERN.search(text)
    if tiktok_match:
        return (tiktok_match.group(0), 'tiktok')
    
    # YouTube
    youtube_match = YOUTUBE_URL_PATTERN.search(text)
    if youtube_match:
        return (youtube_match.group(0), 'youtube')
    
    # Facebook
    facebook_match = FACEBOOK_URL_PATTERN.search(text)
    if facebook_match:
        return (facebook_match.group(0), 'facebook')
    
    return None


def download_video(url: str, output_dir: str, platform: str) -> str | None:
    """
    Download video using yt-dlp with optimized settings for each platform.
    
    Args:
        url: Video URL
        output_dir: Output directory
        platform: 'instagram', 'tiktok', 'youtube', 'facebook'
    
    Returns:
        Path to downloaded video or None
    """
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    
    # Випадковий User-Agent
    user_agent = random.choice(USER_AGENTS)
    
    # Базові опції для всіх платформ
    base_opts = {
        "outtmpl": output_template,
        "quiet": False,  # Показувати output для діагностики
        "no_warnings": False,
        "http_headers": {
            "User-Agent": user_agent,
        },
        "socket_timeout": 30,
        "retries": 5,  # Збільшено з 3 до 5
        "fragment_retries": 5,
        "max_filesize": 50 * 1024 * 1024,  # 50MB limit
    }
    
    # Специфічні налаштування для кожної платформи
    if platform == 'instagram':
        # Instagram
        cookies_file = None
        instagram_cookies = os.environ.get("INSTAGRAM_COOKIES", "")
        if instagram_cookies:
            cookies_path = os.path.join(output_dir, "cookies.txt")
            with open(cookies_path, "w") as f:
                f.write(instagram_cookies)
            cookies_file = cookies_path
        
        ydl_opts = {
            **base_opts,
            "format": (
                "bestvideo[ext=mp4][height<=1920]+bestaudio[ext=m4a]/"
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo+bestaudio/"
                "best[ext=mp4][height<=1920]/"
                "best[ext=mp4]/"
                "best"
            ),
            "merge_output_format": "mp4",
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                },
                {
                    "key": "FFmpegMetadata",
                },
            ],
        }
        if cookies_file:
            ydl_opts["cookiefile"] = cookies_file
            logger.info("Instagram: Using cookies")
    
    elif platform == 'tiktok':
        # TikTok - спеціальні налаштування для обходу блокувань
        ydl_opts = {
            **base_opts,
            "format": (
                "best[ext=mp4][height<=1920]/"
                "best[ext=mp4]/"
                "bestvideo[ext=mp4]+bestaudio/"
                "best"
            ),
            "merge_output_format": "mp4",
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                },
            ],
            # TikTok специфічні опції
            "http_headers": {
                "User-Agent": user_agent,
                "Referer": "https://www.tiktok.com/",
            },
        }
    
    elif platform == 'youtube':
        # YouTube - краща якість зі звуком (підтримка YouTube Music)
        ydl_opts = {
            **base_opts,
            "format": (
                "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/"
                "bestvideo[ext=mp4]+bestaudio/"
                "best[ext=mp4][height<=1080]/"
                "best[ext=mp4]/"
                "best"
            ),
            "merge_output_format": "mp4",
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                },
                {
                    "key": "FFmpegMetadata",
                },
                # Нормалізація звуку
                {
                    "key": "FFmpegFixupM3u8",
                },
            ],
            # YouTube може мати геоблокування
            "geo_bypass": True,
            "geo_bypass_country": "US",
            # YouTube Music специфічні налаштування
            "extract_flat": False,
            "extractor_args": {
                "youtube": {
                    "skip": ["hls", "dash"],  # Пропустити складні формати
                    "player_client": ["android", "web"],  # Спробувати різні клієнти
                }
            },
        }
    
    elif platform == 'facebook':
        # Facebook - підтримка різних форматів URL
        ydl_opts = {
            **base_opts,
            "format": (
                "best[ext=mp4][height<=1080]/"
                "best[ext=mp4]/"
                "bestvideo[ext=mp4]+bestaudio/"
                "best"
            ),
            "merge_output_format": "mp4",
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                },
            ],
            # Facebook специфічні налаштування
            "http_headers": {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Sec-Fetch-Mode": "navigate",
            },
        }
        logger.info("Facebook: Using web scraping mode")
    
    else:
        # Fallback для невідомих платформ
        ydl_opts = {
            **base_opts,
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
        }
    
    try:
        logger.info(f"Downloading {platform} video with yt-dlp...")
        logger.info(f"URL: {url}")
        logger.info(f"Format: {ydl_opts.get('format', 'best')}")
        
        # Затримка для уникнення rate limiting
        time.sleep(random.uniform(0.5, 2))
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Спроба завантажити
            info = ydl.extract_info(url, download=True)
            if info is None:
                logger.error(f"{platform}: yt-dlp returned None")
                return None
            
            logger.info(f"Video info: {info.get('title', 'Unknown')}")
            logger.info(f"Duration: {info.get('duration', 0)} seconds")
            logger.info(f"Has audio: {info.get('acodec', 'none') != 'none'}")
            logger.info(f"Has video: {info.get('vcodec', 'none') != 'none'}")
            logger.info(f"Format ID: {info.get('format_id', 'unknown')}")
            
            # Пошук завантаженого файлу
            filename = ydl.prepare_filename(info)
            base = Path(filename).stem
            
            # Шукаємо відео файл
            for f in Path(output_dir).iterdir():
                if f.stem == base and f.suffix in (".mp4", ".mov", ".mkv", ".webm"):
                    file_size = f.stat().st_size / 1024 / 1024  # MB
                    logger.info(f"Found video file: {f.name}, size: {file_size:.2f}MB")
                    return str(f)
            
            # Fallback: перший відео файл
            for f in Path(output_dir).iterdir():
                if f.suffix in (".mp4", ".mov", ".mkv", ".webm"):
                    logger.info(f"Fallback: Found video file: {f.name}")
                    return str(f)
            
            logger.error("No video file found in output directory")
            return None
            
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp DownloadError for {platform}: {e}")
        return None
    except Exception as e:
        logger.error(f"yt-dlp error for {platform}: {e}", exc_info=True)
        return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    video_info = extract_video_url(message.text)
    if not video_info:
        return

    # Rate limit перевірка
    global cooldown_until
    now = time.time()

    if now < cooldown_until:
        remaining = int((cooldown_until - now) / 60)
        error_msg = await message.reply_text(
            f"⏳ Бот відпочиває. Спробуй через {remaining} хв.",
            reply_to_message_id=message.message_id
        )
        await asyncio.sleep(10)
        try:
            await error_msg.delete()
        except:
            pass
        return

    while request_timestamps and request_timestamps[0] < now - REQUEST_WINDOW:
        request_timestamps.popleft()

    if len(request_timestamps) >= REQUEST_LIMIT:
        cooldown_until = now + COOLDOWN_TIME
        error_msg = await message.reply_text(
            "⏳ Досягнуто ліміт запитів. Бот відпочиває 30 хв.",
            reply_to_message_id=message.message_id
        )
        await asyncio.sleep(10)
        try:
            await error_msg.delete()
        except:
            pass
        return

    request_timestamps.append(now)
    
    video_url, platform = video_info
    logger.info(f"Processing {platform.upper()} URL: {video_url}")
    
    await context.bot.send_chat_action(
        chat_id=message.chat_id,
        action="upload_video"
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        video_path = await asyncio.get_event_loop().run_in_executor(
            None, download_video, video_url, tmp_dir, platform
        )

        if not video_path or not Path(video_path).exists():
            logger.warning(f"Failed to download {platform}: {video_url}")
            error_msg = await message.reply_text(
                f"❌ Не вдалося завантажити відео з {platform.title()}.\n"
                f"Можливо, воно приватне або недоступне.",
                reply_to_message_id=message.message_id
            )
            await asyncio.sleep(10)
            try:
                await error_msg.delete()
            except Exception as e:
                logger.warning(f"Could not delete error message: {e}")
            return

        file_size = Path(video_path).stat().st_size
        if file_size > 50 * 1024 * 1024:
            error_msg = await message.reply_text(
                f"❌ Відео з {platform.title()} завелике для відправки (понад 50 МБ).",
                reply_to_message_id=message.message_id
            )
            await asyncio.sleep(10)
            try:
                await error_msg.delete()
            except Exception as e:
                logger.warning(f"Could not delete error message: {e}")
            return

        try:
            with open(video_path, "rb") as video_file:
                await context.bot.send_video(
                    chat_id=message.chat_id,
                    video=video_file,
                    supports_streaming=True
                )
            
            # Delete original message
            try:
                await message.delete()
            except Exception as e:
                logger.warning(f"Could not delete original message: {e}")

        except Exception as e:
            logger.error(f"Failed to send video from {platform}: {e}")
            error_msg = await message.reply_text(
                f"❌ Помилка при відправці відео з {platform.title()}. Спробуйте пізніше.",
                reply_to_message_id=message.message_id
            )
            await asyncio.sleep(10)
            try:
                await error_msg.delete()
            except Exception as e:
                logger.warning(f"Could not delete error message: {e}")


def create_application() -> Application:
    """Create and configure the bot application."""
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Bot application created")
    logger.info("Supported platforms: Instagram, TikTok, YouTube, Facebook")
    
    return app
