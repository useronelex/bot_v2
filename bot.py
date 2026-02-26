import os
import re
import logging
import asyncio
import tempfile
import requests  # Додано для API fallback методу

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

INSTAGRAM_URL_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_\-]+(?:/[^\s]*)?'
)

TIKTOK_URL_PATTERN = re.compile(
    r'https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/(?:@[\w\.-]+/video/\d+|v/\d+\.html|[\w\-]+)(?:/[^\s]*)?'
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # Наприклад: https://your-app.onrender.com


def extract_video_url(text: str) -> tuple[str, str] | None:
    """
    Extract Instagram or TikTok URL from text.
    Returns: (url, platform) or None
    platform: 'instagram' or 'tiktok'
    """
    # Спочатку перевіряємо Instagram
    instagram_match = INSTAGRAM_URL_PATTERN.search(text)
    if instagram_match:
        return (instagram_match.group(0), 'instagram')
    
    # Потім перевіряємо TikTok
    tiktok_match = TIKTOK_URL_PATTERN.search(text)
    if tiktok_match:
        return (tiktok_match.group(0), 'tiktok')
    
    return None


def download_instagram_via_api(instagram_url: str, output_path: str) -> bool:
    """
    Завантажити Instagram через публічні API (без cookies).
    
    Args:
        instagram_url: URL Instagram відео
        output_path: Шлях для збереження файлу
    
    Returns:
        True якщо успішно, False якщо помилка
    """
    # Список безкоштовних публічних API
    api_endpoints = [
        "https://v3.saveig.app/api/ajaxSearch",
        "https://snapinsta.app/api/ajaxSearch",
    ]
    
    for api_url in api_endpoints:
        try:
            logger.info(f"Trying Instagram API: {api_url}")
            
            # Запит до API
            response = requests.post(
                api_url,
                data={"q": instagram_url, "t": "media", "lang": "en"},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                },
                timeout=30
            )
            
            if response.status_code != 200:
                logger.warning(f"API returned status {response.status_code}")
                continue
            
            # Парсинг відповіді
            data = response.json()
            video_url = data.get('data') or data.get('url') or data.get('download_url')
            
            if not video_url:
                logger.warning("No video URL in API response")
                continue
            
            # Завантаження відео
            logger.info("Downloading video from API...")
            video_response = requests.get(video_url, stream=True, timeout=60)
            
            if video_response.status_code != 200:
                logger.warning(f"Failed to download video: {video_response.status_code}")
                continue
            
            # Збереження файлу
            with open(output_path, 'wb') as f:
                for chunk in video_response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            logger.info(f"✅ Successfully downloaded via API (no cookies needed!)")
            return True
            
        except Exception as e:
            logger.error(f"API error ({api_url}): {e}")
            continue
    
    logger.warning("All API methods failed, will try yt-dlp fallback")
    return False


def download_tiktok_via_api(tiktok_url: str, output_path: str) -> bool:
    """
    Завантажити TikTok через публічні API (без авторизації).
    
    Args:
        tiktok_url: URL TikTok відео
        output_path: Шлях для збереження файлу
    
    Returns:
        True якщо успішно, False якщо помилка
    """
    # Список безкоштовних публічних TikTok API
    api_configs = [
        {
            "name": "SnapTik",
            "url": "https://snaptik.app/abc2.php",
            "method": "post",
            "data_key": "url"
        },
        {
            "name": "TikMate",
            "url": "https://tikmate.app/api/ajaxSearch",
            "method": "post",
            "data_key": "q"
        }
    ]
    
    for api_config in api_configs:
        try:
            logger.info(f"Trying TikTok API: {api_config['name']}")
            
            # Запит до API
            response = requests.post(
                api_config['url'],
                data={api_config['data_key']: tiktok_url, "lang": "en"},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Origin": api_config['url'].split('/api')[0] if '/api' in api_config['url'] else api_config['url'].rsplit('/', 1)[0],
                },
                timeout=30
            )
            
            if response.status_code != 200:
                logger.warning(f"{api_config['name']} returned status {response.status_code}")
                continue
            
            # Парсинг відповіді (різні API мають різну структуру)
            try:
                data = response.json()
            except:
                # Деякі API повертають HTML з JSON всередині
                import json
                json_match = re.search(r'\{.*"url".*\}', response.text, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group(0))
                else:
                    logger.warning(f"{api_config['name']}: Could not parse response")
                    continue
            
            # Пошук URL відео в різних можливих полях
            video_url = None
            possible_keys = ['data', 'url', 'download_url', 'video', 'videoUrl', 'video_url', 'play']
            
            for key in possible_keys:
                if key in data:
                    video_url = data[key]
                    if isinstance(video_url, dict):
                        # Якщо це dict, шукаємо URL всередині
                        video_url = video_url.get('url') or video_url.get('video') or video_url.get('download')
                    if video_url and isinstance(video_url, str) and video_url.startswith('http'):
                        break
            
            if not video_url:
                logger.warning(f"{api_config['name']}: No video URL in response")
                continue
            
            # Завантаження відео
            logger.info(f"Downloading TikTok video from {api_config['name']}...")
            video_response = requests.get(
                video_url, 
                stream=True, 
                timeout=60,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            
            if video_response.status_code != 200:
                logger.warning(f"Failed to download video: {video_response.status_code}")
                continue
            
            # Збереження файлу
            with open(output_path, 'wb') as f:
                for chunk in video_response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # Перевірка що файл не порожній
            if os.path.getsize(output_path) < 1024:  # < 1KB
                logger.warning(f"{api_config['name']}: Downloaded file too small")
                continue
            
            logger.info(f"✅ TikTok successfully downloaded via {api_config['name']} API!")
            return True
            
        except Exception as e:
            logger.error(f"TikTok API error ({api_config['name']}): {e}")
            continue
    
    logger.warning("All TikTok API methods failed, will try yt-dlp fallback")
    return False


def download_video(url: str, output_dir: str) -> str | None:
    """Download video from Instagram or TikTok. Returns filepath or None."""
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    output_path = os.path.join(output_dir, "video.mp4")
    
    # ✅ ДЛЯ INSTAGRAM: Спочатку спробувати API (БЕЗ cookies!)
    if 'instagram' in url.lower():
        try:
            if download_instagram_via_api(url, output_path):
                logger.info("Instagram: Downloaded via API successfully")
                return output_path
        except Exception as e:
            logger.warning(f"Instagram API method failed: {e}, trying yt-dlp fallback...")
    
    # ✅ ДЛЯ TIKTOK: Спочатку спробувати API (БЕЗ авторизації!)
    if 'tiktok' in url.lower():
        try:
            if download_tiktok_via_api(url, output_path):
                logger.info("TikTok: Downloaded via API successfully")
                return output_path
        except Exception as e:
            logger.warning(f"TikTok API method failed: {e}, trying yt-dlp fallback...")
    
    # ⚠️ FALLBACK: yt-dlp (для Instagram з cookies, для TikTok без cookies)
    # Записуємо cookies у тимчасовий файл (тільки для Instagram)
    cookies_file = None
    instagram_cookies = os.environ.get("INSTAGRAM_COOKIES", "")
    if instagram_cookies and 'instagram' in url:
        cookies_path = os.path.join(output_dir, "cookies.txt")
        with open(cookies_path, "w") as f:
            f.write(instagram_cookies)
        cookies_file = cookies_path
       
    ydl_opts = {
        "outtmpl": output_template,
        # Format string: пріоритет комбінованим форматам
        "format": (
            "bestvideo[ext=mp4][height<=1920]+bestaudio[ext=m4a]/"  # Відео+аудіо окремо (до 1080p)
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"                 # Відео+аудіо окремо (будь-яка якість)
            "bestvideo+bestaudio/"                                    # Будь-які формати
            "best[ext=mp4][height<=1920]/"                           # Комбінований mp4 до 1080p
            "best[ext=mp4]/"                                          # Комбінований mp4
            "best"                                                     # Будь-який найкращий
        ),
        # Об'єднати відео+аудіо в один файл
        "merge_output_format": "mp4",
        # Постобробка через ffmpeg
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
            ),
        },
        # Limit file size to 50MB (Telegram Bot API limit)
        "max_filesize": 50 * 1024 * 1024,
    }
    
    # Додаємо cookies якщо є
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
        logger.info("Using yt-dlp fallback with cookies (Instagram)")
    else:
        logger.info("Using yt-dlp fallback (TikTok)")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return None
            
            # Find the downloaded file
            filename = ydl.prepare_filename(info)
            # yt-dlp might change extension, so search for the file
            base = Path(filename).stem
            for f in Path(output_dir).iterdir():
                if f.stem == base and f.suffix in (".mp4", ".mov", ".mkv", ".webm"):
                    return str(f)
            
            # Fallback: return first video file in dir
            for f in Path(output_dir).iterdir():
                if f.suffix in (".mp4", ".mov", ".mkv", ".webm"):
                    return str(f)
            
            return None
    except Exception as e:
        logger.error(f"yt-dlp error for {url}: {e}")
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
        await message.reply_text(f"⏳ Бот відпочиває. Спробуй через {remaining} хв.")
        return

    # Видаляємо старі запити (старші за 1 годину)
    while request_timestamps and request_timestamps[0] < now - REQUEST_WINDOW:
        request_timestamps.popleft()

    if len(request_timestamps) >= REQUEST_LIMIT:
        cooldown_until = now + COOLDOWN_TIME
        await message.reply_text("⏳ Досягнуто ліміт запитів. Бот відпочиває 30 хв.")
        return

    request_timestamps.append(now)
    
    video_url, platform = video_info
    logger.info(f"Processing {platform.title()} URL: {video_url}")
    
    # Show "uploading video" action
    await context.bot.send_chat_action(
        chat_id=message.chat_id,
        action="upload_video"
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        video_path = await asyncio.get_event_loop().run_in_executor(
            None, download_video, video_url, tmp_dir
        )

        if not video_path or not Path(video_path).exists():
            logger.warning(f"Failed to download: {video_url}")
            error_msg = await message.reply_text(
                "❌ Не вдалося завантажити відео. Можливо, воно приватне або недоступне.",
                reply_to_message_id=message.message_id
            )
            # Видалити повідомлення про помилку через 10 секунд
            await asyncio.sleep(10)
            try:
                await error_msg.delete()
            except Exception as e:
                logger.warning(f"Could not delete error message: {e}")
            return

        file_size = Path(video_path).stat().st_size
        if file_size > 50 * 1024 * 1024:
            error_msg = await message.reply_text(
                "❌ Відео завелике для відправки (понад 50 МБ).",
                reply_to_message_id=message.message_id
            )
            # Видалити повідомлення про помилку через 10 секунд
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
            
            # Delete original message with the link
            try:
                await message.delete()
            except Exception as e:
                logger.warning(f"Could not delete original message: {e}")

        except Exception as e:
            logger.error(f"Failed to send video: {e}")
            error_msg = await message.reply_text(
                "❌ Помилка при відправці відео. Спробуйте пізніше.",
                reply_to_message_id=message.message_id
            )
            # Видалити повідомлення про помилку через 10 секунд
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
    return app
