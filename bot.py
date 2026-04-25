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
    """
    Зберігає cookies з env-змінної у тимчасовий файл.
    gallery-dl читає cookies у форматі Netscape.

    Як отримати cookies:
      1. Встанови розширення "Cookie-Editor" в Chrome
      2. Відкрий instagram.com будучи залогіненим
      3. Cookie-Editor -> Export -> Netscape format
      4. Вміст файлу -> INSTAGRAM_COOKIES на Render
    """
    global _COOKIES_FILE
    if not INSTAGRAM_COOKIES_RAW:
        logger.warning("INSTAGRAM_COOKIES не встановлено")
        return
    path = "/tmp/instagram_cookies.txt"
    with open(path, "w") as f:
        f.write(INSTAGRAM_COOKIES_RAW)
    _COOKIES_FILE = path
    count = sum(1 for l in INSTAGRAM_COOKIES_RAW.splitlines() if l.strip() and not l.startswith("#"))
    logger.info(f"Instagram cookies: {count} шт.")

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
# INSTAGRAM — gallery-dl
# ──────────────────────────────────────────
def _download_instagram(url: str, output_dir: str) -> str | None:
    """
    gallery-dl — найстабільніший інструмент для Instagram.
    Підтримує: пости, рілси, сторіз, 18+, приватний контент (з cookies).
    Оновлюється кожні 1-2 тижні на GitHub.
    Не логіниться через код — тільки використовує cookies існуючої сесії браузера.
    """
    from gallery_dl import config as gdl_config, job as gdl_job

    gdl_config.clear()
    gdl_config.set((), "base-directory", output_dir)
    gdl_config.set((), "directory",      [])
    gdl_config.set((), "filename",       "video.{extension}")
    gdl_config.set((), "sleep-request",  1.5)

    gdl_config.set(("extractor", "instagram"), "videos",  True)
    gdl_config.set(("extractor", "instagram"), "reels",   True)
    gdl_config.set(("extractor", "instagram"), "stories", True)
    # Пріоритет mp4 — gallery-dl обере H.264 якщо доступний
    gdl_config.set(("extractor", "instagram"), "filename", "{id}.{extension}")
    gdl_config.set(("downloader", "ytdl"), "format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best")

    gdl_config.set(
        ("extractor",), "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    if _COOKIES_FILE:
        gdl_config.set(("extractor",), "cookies", _COOKIES_FILE)
        logger.info("gallery-dl: cookies активні")
    else:
        logger.warning("gallery-dl: без cookies — тільки публічний контент")

    try:
        gdl_job.DownloadJob(url).run()

        for f in Path(output_dir).rglob("*"):
            if f.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv") and f.stat().st_size > 0:
                logger.info(f"gallery-dl OK: {f.name} ({f.stat().st_size/1024/1024:.1f} MB)")
                return str(f)

        logger.info("gallery-dl: відео не знайдено (фото?)")
        return None

    except Exception as e:
        msg = str(e).lower()
        if any(w in msg for w in ("private", "login", "restricted", "age")):
            logger.warning(f"gallery-dl: потрібна авторизація — {e}")
        elif any(w in msg for w in ("not found", "404", "deleted")):
            logger.warning(f"gallery-dl: контент не знайдено — {e}")
        else:
            logger.error(f"gallery-dl: {e}", exc_info=True)
        return None

# ──────────────────────────────────────────
# FACEBOOK — yt-dlp
# ──────────────────────────────────────────
def _download_facebook(url: str, output_dir: str) -> str | None:
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({
            "outtmpl":     os.path.join(output_dir, "video.%(ext)s"),
            "format":      "best[ext=mp4]/best",
            "quiet":       True,
            "max_filesize": 50 * 1024 * 1024,
        }) as ydl:
            ydl.download([url])
        for f in Path(output_dir).glob("video.*"):
            if f.stat().st_size > 0:
                return str(f)
        return None
    except Exception as e:
        logger.error(f"Facebook: {e}")
        return None

# ──────────────────────────────────────────
# FFMPEG КОНВЕРТАЦІЯ
# ──────────────────────────────────────────
def _get_video_info(input_path: str) -> dict:
    """Отримує інфо про відео через ffprobe — кодек, піксельний формат, тривалість."""
    import subprocess, json as _json
    probe = subprocess.run(
        ["ffprobe", "-v", "error",
         "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt:format=duration,size",
         "-of", "json", input_path],
        capture_output=True, text=True, timeout=20
    )
    try:
        data = _json.loads(probe.stdout)
        stream = data.get("streams", [{}])[0]
        fmt    = data.get("format", {})
        return {
            "codec":    stream.get("codec_name", "unknown"),
            "pix_fmt":  stream.get("pix_fmt", "unknown"),
            "duration": float(fmt.get("duration", 60)),
            "size_mb":  int(fmt.get("size", 0)) / 1024 / 1024,
        }
    except Exception:
        return {"codec": "unknown", "pix_fmt": "unknown", "duration": 60.0, "size_mb": 0}


def _convert_for_ios(input_path: str, output_dir: str) -> str:
    """
    Єдина мета: сумісність з iOS/Telegram.

    Два сценарії:
    1. H.264 + yuv420p (8-bit) → тільки faststart, без перекодування (1-2 сек)
    2. Будь-який інший кодек (VP9, H.265, 10-bit) → перекодування у H.264

    Розмір НЕ обмежуємо — конвертація повільніша ніж завантаження великого файлу.
    Швидкість важливіша за розмір: preset=ultrafast.
    """
    import subprocess

    info = _get_video_info(input_path)
    output_path = Path(output_dir) / "out.mp4"
    faststart_path = Path(output_dir) / "final.mp4"

    needs_reencode = (
        info["codec"] not in ("h264", "avc1") or
        "10" in info["pix_fmt"] or
        info["pix_fmt"] == "unknown"
    )

    logger.info(f"ffmpeg: codec={info['codec']} pix={info['pix_fmt']} "
                f"size={info['size_mb']:.1f}MB reencode={needs_reencode}")

    if not needs_reencode:
        # Вже H.264 8-bit — тільки faststart (миттєво, без перекодування)
        result = subprocess.run([
            "ffmpeg", "-y", "-i", input_path,
            "-c", "copy",
            "-movflags", "+faststart",
            "-threads", "1",
            str(faststart_path)
        ], capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            logger.info("ffmpeg: faststart OK (no reencode)")
            return str(faststart_path)
        return input_path

    # Перекодування: ultrafast = максимальна швидкість, мінімум RAM
    # Якість трохи нижча ніж veryfast, але для мобільного перегляду непомітно
    # Визначаємо розумний maxrate щоб ultrafast не роздував файл
    # ultrafast має поганий компресор, тому без maxrate VP9 5MB → H264 16MB
    info_size = info["size_mb"]
    if info_size < 10:
        maxrate = "1500k"
    elif info_size < 30:
        maxrate = "2500k"
    else:
        maxrate = "4000k"

    result = subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",
        "-crf", "28",               # 28 замість 23 — менший файл, прийнятна якість
        "-maxrate", maxrate,        # обмеження пікового бітрейту
        "-bufsize", f"{int(maxrate[:-1]) * 2}k",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        "-threads", "1",
        str(output_path)
    ], capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        logger.error(f"ffmpeg failed: {result.stderr[-200:]}")
        return input_path

    out_mb = output_path.stat().st_size / 1024 / 1024
    logger.info(f"ffmpeg reencode OK: {info['size_mb']:.1f}MB -> {out_mb:.1f}MB")
    return str(output_path)


# ──────────────────────────────────────────
# DEDUPLICATION — запобігає повторній обробці одного URL
# ──────────────────────────────────────────
_processing: set[str] = set()

# ──────────────────────────────────────────
# DISPATCH
# ──────────────────────────────────────────
def download_media(url: str, output_dir: str, platform: str) -> tuple[str | None, str]:
    if platform == "facebook":
        path = _download_facebook(url, output_dir)
    else:
        path = _download_instagram(url, output_dir)

    if not path:
        return None, "unknown"

    path = _convert_for_ios(path, output_dir)
    return path, "video"

# ──────────────────────────────────────────
# TYPING
# ──────────────────────────────────────────
async def keep_uploading_action(chat_id: int, bot) -> None:
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action="upload_video")
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
    ts = user_timestamps[user_id]
    while ts and ts[0] < now - REQUEST_WINDOW:
        ts.popleft()
    if len(ts) >= REQUEST_LIMIT:
        user_cooldowns[user_id] = now + COOLDOWN_TIME
        return False, COOLDOWN_TIME // 60
    ts.append(now)
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
        try: await err.delete()
        except Exception: pass
        return

    media_url, platform, content_type = url_info

    # Дедуплікація — якщо цей URL вже обробляється, ігноруємо
    dedup_key = f"{media_url}:{message.chat_id}"
    if dedup_key in _processing:
        logger.info(f"Duplicate skipped: {media_url}")
        return
    _processing.add(dedup_key)

    logger.info(f"[{platform.upper()}/{content_type}] user={user_id} | {media_url}")
    typing_task = asyncio.create_task(keep_uploading_action(message.chat_id, context.bot))

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            media_path, _ = await asyncio.get_event_loop().run_in_executor(
                None, download_media, media_url, tmp_dir, platform
            )

            if not media_path or not Path(media_path).exists():
                err = await message.reply_text(
                    "Не вдалося завантажити.\n"
                    "Можливо контент приватний, видалено або недоступний.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try: await err.delete()
                except Exception: pass
                return

            size_mb = Path(media_path).stat().st_size / 1024 / 1024
            if size_mb > 50:
                err = await message.reply_text(
                    f"Файл завеликий ({size_mb:.0f} MB). Максимум 50 MB.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try: await err.delete()
                except Exception: pass
                return

            sent = False
            last_error = None
            for attempt in range(3):
                try:
                    with open(media_path, "rb") as f:
                        await context.bot.send_video(
                            chat_id=message.chat_id,
                            video=f,
                            supports_streaming=True,
                            write_timeout=120,
                            read_timeout=60,
                            connect_timeout=30,
                        )
                    logger.info(f"Sent {size_mb:.1f}MB (attempt {attempt+1})")
                    sent = True
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(f"Send attempt {attempt+1} failed: {e}")
                    if attempt < 2:
                        await asyncio.sleep(3)

            if sent:
                try: await message.delete()
                except Exception: pass
            else:
                logger.error(f"Send failed after 3 attempts: {last_error}")
                err = await message.reply_text(
                    "Помилка при відправці. Спробуйте пізніше.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try: await err.delete()
                except Exception: pass
    finally:
        typing_task.cancel()
        _processing.discard(dedup_key)

# ──────────────────────────────────────────
# APP FACTORY
# ──────────────────────────────────────────
def create_application() -> Application:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не встановлено!")
    _init_cookies()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущено | Instagram (gallery-dl) + Facebook (yt-dlp)")
    logger.info(f"Cookies: {'OK' if _COOKIES_FILE else 'НЕ ВСТАНОВЛЕНО'}")
    return app
