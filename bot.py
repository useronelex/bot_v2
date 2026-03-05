import os
import re
import logging
import asyncio
import tempfile
import random
import time
import json
import requests

from collections import deque
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import yt_dlp


# ──────────────────────────────────────────
# RATE LIMIT — per user
# ──────────────────────────────────────────
REQUEST_LIMIT = 20
REQUEST_WINDOW = 3600
COOLDOWN_TIME = 1800

user_timestamps: dict[int, deque] = {}
user_cooldowns: dict[int, float] = {}

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
TIKTOK_URL_PATTERN = re.compile(
    r'https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/(?:@[\w\.-]+/video/\d+|v/[\w\-]+|[\w\-]+)(?:/[^\s]*)?'
)
FACEBOOK_URL_PATTERN = re.compile(
    r'https?://(?:www\.|m\.|web\.)?facebook\.com/(?:watch/?\?v=|[\w\-\.]+/videos/|share/[vr]/)[\d\w\-]+'
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")


# ──────────────────────────────────────────
# USER AGENTS
# ──────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
    "Mozilla/5.0 (Linux; Android 12; SM-G998B) Chrome/108.0.0.0 Mobile Safari/537.36",
]


# ──────────────────────────────────────────
# Cookies
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
        logger.info("Instagram cookies loaded")


# ──────────────────────────────────────────
# TIKTOK DOWNLOADER (PHP method)
# ──────────────────────────────────────────
def download_tiktok_php_method(url: str, output_dir: str) -> tuple[str | None, str]:
    """
    Завантажує TikTok використовуючи метод з PHP скрипта:
    1. Завантажує HTML сторінки
    2. Витягує video_id
    3. Використовує TikTok API для отримання downloadAddr
    
    Returns: (filepath, media_type)
    """
    try:
        logger.info("TikTok: Using PHP-like API method")
        
        # Крок 1: Завантажуємо HTML сторінки TikTok
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": "https://www.tiktok.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        
        # Resolve short links (vm.tiktok.com)
        session = requests.Session()
        response = session.get(url, headers=headers, timeout=15, allow_redirects=True)
        final_url = response.url
        html = response.text
        
        logger.info(f"TikTok: Resolved to {final_url}")
        
        # Крок 2: Витягуємо video_id з HTML
        # Метод 1: itemStruct
        match = re.search(r'"itemStruct":\s*\{"id"\s*:\s*"(\d+)"', html)
        if not match:
            # Метод 2: aweme_id
            match = re.search(r'"aweme_id"\s*:\s*"(\d+)"', html)
        if not match:
            # Метод 3: video/ID з URL
            match = re.search(r'/video/(\d+)', final_url)
        
        if not match:
            logger.error("TikTok: Could not extract video_id")
            return None, 'video'
        
        video_id = match.group(1)
        logger.info(f"TikTok: Extracted video_id={video_id}")
        
        # Крок 3: Використовуємо TikTok API (як у PHP)
        api_url = (
            f"https://www.tiktok.com/api/related/item_list/"
            f"?WebIdLastTime=0"
            f"&aid=1988"
            f"&app_language=en"
            f"&app_name=tiktok_web"
            f"&browser_language=en-US"
            f"&browser_name=Mozilla"
            f"&browser_online=true"
            f"&browser_platform=Win32"
            f"&channel=tiktok_web"
            f"&cookie_enabled=true"
            f"&count=16"
            f"&device_platform=web_pc"
            f"&focus_state=false"
            f"&from_page=video"
            f"&history_len=4"
            f"&is_fullscreen=false"
            f"&is_page_visible=true"
            f"&itemID={video_id}"
            f"&language=en"
            f"&os=windows"
            f"&priority_region="
            f"&referer="
            f"&region=US"
            f"&screen_height=1080"
            f"&screen_width=1920"
            f"&tz_name=Europe/Kiev"
            f"&user_is_login=false"
            f"&webcast_language=en"
        )
        
        api_headers = {
            "User-Agent": headers["User-Agent"],
            "Referer": "https://www.tiktok.com/",
            "Accept": "application/json",
        }
        
        api_response = session.get(api_url, headers=api_headers, timeout=15)
        api_data = api_response.json()
        
        # Крок 4: Витягуємо downloadAddr
        if "itemList" not in api_data or not api_data["itemList"]:
            logger.error("TikTok: No itemList in API response")
            return None, 'video'
        
        for item in api_data["itemList"]:
            if str(item.get("id")) == str(video_id):
                video_data = item.get("video", {})
                download_url = video_data.get("downloadAddr")
                
                if not download_url:
                    # Fallback: playAddr
                    download_url = video_data.get("playAddr")
                
                if not download_url:
                    logger.error("TikTok: No downloadAddr in API response")
                    return None, 'video'
                
                logger.info(f"TikTok: Found downloadAddr")
                
                # Крок 5: Завантажуємо відео
                video_headers = {
                    "User-Agent": "okhttp",  # Як у PHP!
                    "Referer": "https://www.tiktok.com/",
                    "Range": "bytes=0-",
                }
                
                video_response = session.get(
                    download_url,
                    headers=video_headers,
                    timeout=60,
                    stream=True
                )
                
                if video_response.status_code not in [200, 206]:
                    logger.error(f"TikTok: Download failed with status {video_response.status_code}")
                    return None, 'video'
                
                # Зберігаємо файл
                output_path = os.path.join(output_dir, f"{video_id}.mp4")
                with open(output_path, 'wb') as f:
                    for chunk in video_response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                file_size = os.path.getsize(output_path) / 1024 / 1024
                logger.info(f"TikTok: Downloaded {file_size:.2f}MB via PHP method")
                
                return output_path, 'video'
        
        logger.error("TikTok: video_id not found in itemList")
        return None, 'video'
        
    except requests.RequestException as e:
        logger.error(f"TikTok: Network error - {e}")
        return None, 'video'
    except json.JSONDecodeError as e:
        logger.error(f"TikTok: JSON decode error - {e}")
        return None, 'video'
    except Exception as e:
        logger.error(f"TikTok: Unexpected error - {e}", exc_info=True)
        return None, 'video'


# ──────────────────────────────────────────
# URL EXTRACTOR
# ──────────────────────────────────────────
def extract_video_url(text: str) -> tuple[str, str] | None:
    for pattern, platform in [
        (INSTAGRAM_URL_PATTERN, "instagram"),
        (TIKTOK_URL_PATTERN, "tiktok"),
        (FACEBOOK_URL_PATTERN, "facebook"),
    ]:
        match = pattern.search(text)
        if match:
            return (match.group(0), platform)
    return None


# ──────────────────────────────────────────
# DOWNLOADER
# ──────────────────────────────────────────
def download_media(url: str, output_dir: str, platform: str) -> tuple[str | None, str]:
    """Завантажує відео або фото"""
    
    # TikTok: використовуємо PHP метод
    if platform == "tiktok":
        return download_tiktok_php_method(url, output_dir)
    
    # Instagram / Facebook: використовуємо yt-dlp
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    user_agent = random.choice(USER_AGENTS)

    base_opts = {
        "outtmpl": output_template,
        "quiet": False,
        "no_warnings": False,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "max_filesize": 50 * 1024 * 1024,
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

    elif platform == "facebook":
        ydl_opts = {
            **base_opts,
            "format": "best[ext=mp4][height<=1080]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "postprocessor_args": {"merger": ["-c", "copy"]},
        }

    else:
        ydl_opts = {**base_opts, "format": "best[ext=mp4]/best"}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None, 'unknown'

            media_type = 'video'
            if platform == "instagram":
                if info.get('vcodec') == 'none' or not info.get('vcodec'):
                    media_type = 'photo'

            filename = ydl.prepare_filename(info)
            base = Path(filename).stem

            for ext in [".mp4", ".jpg", ".jpeg", ".png", ".webp"]:
                for f in Path(output_dir).iterdir():
                    if f.stem == base and f.suffix == ext:
                        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                            media_type = 'photo'
                        return str(f), media_type

            return None, 'unknown'

    except Exception as e:
        logger.error(f"{platform} yt-dlp error: {e}")
        return None, 'unknown'


# ──────────────────────────────────────────
# TYPING INDICATOR
# ──────────────────────────────────────────
async def keep_uploading_action(chat_id: int, bot, media_type: str = 'video') -> None:
    action = "upload_video" if media_type == 'video' else "upload_photo"
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

    video_info = extract_video_url(message.text)
    if not video_info:
        return

    user_id = message.from_user.id
    allowed, cooldown_mins = check_rate_limit(user_id)

    if not allowed:
        err = await message.reply_text(
            f"⏳ Забагато запитів. Спробуй через {cooldown_mins} хв.",
            reply_to_message_id=message.message_id
        )
        await asyncio.sleep(10)
        try:
            await err.delete()
        except:
            pass
        return

    video_url, platform = video_info
    logger.info(f"Processing {platform.upper()}: {video_url} | user={user_id}")

    typing_task = asyncio.create_task(
        keep_uploading_action(message.chat_id, context.bot, 'video')
    )

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            media_path, media_type = await asyncio.get_event_loop().run_in_executor(
                None, download_media, video_url, tmp_dir, platform
            )

            if not media_path or not Path(media_path).exists():
                err = await message.reply_text(
                    f"❌ Не вдалося завантажити з {platform.title()}.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try:
                    await err.delete()
                except:
                    pass
                return

            file_size = Path(media_path).stat().st_size
            if file_size > 50 * 1024 * 1024:
                err = await message.reply_text(
                    "❌ Файл завеликий (>50MB).",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try:
                    await err.delete()
                except:
                    pass
                return

            typing_task.cancel()
            typing_task = asyncio.create_task(
                keep_uploading_action(message.chat_id, context.bot, media_type)
            )

            try:
                with open(media_path, "rb") as media_file:
                    if media_type == 'photo':
                        await context.bot.send_photo(chat_id=message.chat_id, photo=media_file)
                    else:
                        await context.bot.send_video(
                            chat_id=message.chat_id,
                            video=media_file,
                            supports_streaming=True
                        )
                
                try:
                    await message.delete()
                except:
                    pass

            except Exception as e:
                logger.error(f"Send failed: {e}")
                err = await message.reply_text("❌ Помилка відправки.")
                await asyncio.sleep(10)
                try:
                    await err.delete()
                except:
                    pass
    finally:
        typing_task.cancel()


# ──────────────────────────────────────────
# APP
# ──────────────────────────────────────────
def create_application() -> Application:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set!")

    _init_cookies()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    logger.info("Platforms: Instagram (photo+video), TikTok (PHP method), Facebook")
    logger.info(f"Instagram cookies: {_INSTAGRAM_COOKIES_FILE is not None}")

    return app
