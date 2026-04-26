import os
import re
import logging
import asyncio
import tempfile
import time
from collections import deque, defaultdict
from pathlib import Path
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL   = os.environ.get("WEBHOOK_URL", "")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID") or "0")

INSTAGRAM_COOKIES_RAW = os.environ.get("INSTAGRAM_COOKIES", "")
_COOKIES_FILE: str | None = None

# Rate limit
REQUEST_LIMIT  = 50
REQUEST_WINDOW = 3600
COOLDOWN_TIME  = 1800
user_timestamps: dict[int, deque] = {}
user_cooldowns:  dict[int, float] = {}

# Дедуплікація
_processing: set[str] = set()

# Зберігаємо message_id відео бота для команди /clean
_sent_messages: dict[int, deque] = defaultdict(lambda: deque(maxlen=200))

# ──────────────────────────────────────────
# URL PATTERNS
# Покриває всі варіанти Instagram і Facebook посилань
# ──────────────────────────────────────────
INSTAGRAM_PATTERNS = [
    # Стандартні пости, reels, tv
    re.compile(r'https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_\-]+)/?'),
    # Сторіз
    re.compile(r'https?://(?:www\.)?instagram\.com/stories/([A-Za-z0-9_\.]+)/(\d+)/?'),
    # Короткі посилання
    re.compile(r'https?://instagr\.am/(?:p|reel)/([A-Za-z0-9_\-]+)/?'),
    # Share посилання з параметрами
    re.compile(r'https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_\-]+)/\?'),
]

FACEBOOK_PATTERNS = [
    re.compile(r'https?://(?:www\.|m\.|web\.)?facebook\.com/watch/?\?v=[\d]+'),
    re.compile(r'https?://(?:www\.|m\.|web\.)?facebook\.com/[\w\.\-]+/videos/[\d\w\-]+'),
    re.compile(r'https?://(?:www\.|m\.|web\.)?facebook\.com/share/[vr]/[\w\-]+'),
    re.compile(r'https?://fb\.watch/[\w\-]+'),
]

def extract_url(text: str) -> tuple[str, str] | None:
    """Повертає (url, platform) або None."""
    for pattern in INSTAGRAM_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).split('?')[0].rstrip('/'), "instagram"
    for pattern in FACEBOOK_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0), "facebook"
    return None

# ──────────────────────────────────────────
# COOKIES
# ──────────────────────────────────────────
def _init_cookies() -> None:
    global _COOKIES_FILE
    if not INSTAGRAM_COOKIES_RAW:
        logger.warning("INSTAGRAM_COOKIES не встановлено — тільки публічний контент")
        return
    path = "/tmp/instagram_cookies.txt"
    with open(path, "w") as f:
        f.write(INSTAGRAM_COOKIES_RAW)
    _COOKIES_FILE = path
    count = sum(
        1 for line in INSTAGRAM_COOKIES_RAW.splitlines()
        if line.strip() and not line.startswith("#")
    )
    logger.info(f"Cookies завантажено: {count} шт.")

# ──────────────────────────────────────────
# DOWNLOADER — yt-dlp з cookies
# H264 через format selector — без конвертації
# ──────────────────────────────────────────
def download_media(url: str, output_dir: str, platform: str) -> str | None:
    import yt_dlp

    output_template = os.path.join(output_dir, "video.%(ext)s")

    # Format selector:
    # 1. H264 відео + m4a аудіо (найкраща якість сумісна з iOS)
    # 2. Будь-який mp4
    # 3. Найкраще доступне
    fmt = (
        "bestvideo[vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[vcodec^=avc]+bestaudio/"
        "best[ext=mp4]/"
        "best"
    )

    ydl_opts = {
        "outtmpl":          output_template,
        "format":           fmt,
        "merge_output_format": "mp4",
        "quiet":            True,
        "no_warnings":      True,
        "socket_timeout":   30,
        "retries":          3,
        "fragment_retries": 3,
        # Мобільний User-Agent — Instagram віддає H264 мобільним клієнтам
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            ),
        },
        # postprocessor для faststart — moov atom на початку файлу
        # це критично для Telegram iOS щоб відео грало без повного завантаження
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
        "postprocessor_args": {
            "ffmpegvideoconvertor": ["-movflags", "+faststart"],
        },
    }

    # Додаємо cookies якщо є — для приватного контенту і 18+
    if _COOKIES_FILE:
        ydl_opts["cookiefile"] = _COOKIES_FILE
        logger.info(f"yt-dlp: cookies активні | {platform} | {url}")
    else:
        logger.info(f"yt-dlp: без cookies | {platform} | {url}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                logger.warning("yt-dlp: extract_info повернув None")
                return None

        # Шукаємо завантажений файл
        for f in Path(output_dir).glob("video.*"):
            if f.stat().st_size > 0:
                size_mb = f.stat().st_size / 1024 / 1024
                codec = info.get("vcodec", "unknown")
                logger.info(f"yt-dlp OK: {f.name} | {size_mb:.1f}MB | codec={codec}")
                return str(f)

        logger.warning("yt-dlp: файл не знайдено після завантаження")
        return None

    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if "private" in err or "login" in err or "age" in err:
            logger.warning(f"yt-dlp: потрібна авторизація — {e}")
        elif "not found" in err or "404" in err or "does not exist" in err:
            logger.warning(f"yt-dlp: контент не знайдено — {e}")
        elif "no video" in err or "photo" in err:
            logger.info(f"yt-dlp: це фото пост — {e}")
        else:
            logger.error(f"yt-dlp DownloadError: {e}")
        return None
    except Exception as e:
        logger.error(f"yt-dlp Exception: {e}", exc_info=True)
        return None

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
# TYPING INDICATOR
# ──────────────────────────────────────────
async def keep_uploading_action(chat_id: int, bot) -> None:
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action="upload_video")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass

# ──────────────────────────────────────────
# MAIN HANDLER
# ──────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    # Захист від спаму після перезапуску — ігноруємо старі повідомлення
    if time.time() - message.date.timestamp() > 30:
        return

    url_info = extract_url(message.text)
    if not url_info:
        return

    media_url, platform = url_info
    user_id = message.from_user.id

    # Rate limit
    allowed, cooldown_mins = check_rate_limit(user_id)
    if not allowed:
        err = await message.reply_text(f"Забагато запитів. Спробуй через {cooldown_mins} хв.")
        await asyncio.sleep(10)
        try: await err.delete()
        except Exception: pass
        return

    # Дедуплікація
    dedup_key = f"{media_url}:{message.chat_id}"
    if dedup_key in _processing:
        logger.info(f"Duplicate skipped: {media_url}")
        return
    _processing.add(dedup_key)

    logger.info(f"[{platform.upper()}] user={user_id} | {media_url}")
    typing_task = asyncio.create_task(keep_uploading_action(message.chat_id, context.bot))

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            media_path = await asyncio.get_event_loop().run_in_executor(
                None, download_media, media_url, tmp_dir, platform
            )

            if not media_path or not Path(media_path).exists():
                err = await message.reply_text(
                    "Не вдалося завантажити. Контент приватний, видалено або недоступний.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(5)
                try: await err.delete()
                except Exception: pass
                return

            size_mb = Path(media_path).stat().st_size / 1024 / 1024
            if size_mb > 50:
                err = await message.reply_text(
                    f"Файл завеликий ({size_mb:.0f} MB). Telegram приймає до 50 MB.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(5)
                try: await err.delete()
                except Exception: pass
                return

            # Отримуємо розміри відео для правильного відображення на всіх пристроях
            width = height = None
            try:
                import subprocess, json as _json
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height",
                     "-of", "json", media_path],
                    capture_output=True, text=True, timeout=10
                )
                stream = _json.loads(probe.stdout).get("streams", [{}])[0]
                width  = stream.get("width")
                height = stream.get("height")
            except Exception:
                pass

            # Відправка з retry
            sent = False
            for attempt in range(3):
                try:
                    with open(media_path, "rb") as f:
                        sent_msg = await context.bot.send_video(
                            chat_id=message.chat_id,
                            video=f,
                            supports_streaming=True,
                            width=width,
                            height=height,
                            write_timeout=120,
                            read_timeout=60,
                            connect_timeout=30,
                        )
                    _sent_messages[message.chat_id].append(sent_msg.message_id)
                    logger.info(f"Sent {size_mb:.1f}MB (attempt {attempt+1})")
                    sent = True
                    break
                except Exception as e:
                    logger.warning(f"Send attempt {attempt+1} failed: {e}")
                    if attempt < 2:
                        await asyncio.sleep(3)

            if sent:
                try: await message.delete()
                except Exception: pass
            else:
                err = await message.reply_text(
                    "Помилка при відправці. Спробуйте пізніше.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(5)
                try: await err.delete()
                except Exception: pass
    finally:
        typing_task.cancel()
        _processing.discard(dedup_key)

# ──────────────────────────────────────────
# ADMIN КОМАНДИ
# ──────────────────────────────────────────
async def cmd_clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clean [N|all] [chat_id] — видалити останні N відео бота з групи."""
    if not update.message or update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Немає доступу.")
        return

    args = context.args or []
    count_arg = args[0] if args else "5"
    chat_id_arg = int(args[1]) if len(args) > 1 else None

    target_chat = chat_id_arg or (next(iter(_sent_messages)) if _sent_messages else None)
    if not target_chat:
        await update.message.reply_text("Немає збережених повідомлень.")
        return

    msgs = _sent_messages.get(target_chat)
    if not msgs:
        await update.message.reply_text(f"Немає повідомлень для чату {target_chat}.")
        return

    all_msgs = list(msgs)
    to_delete = all_msgs if count_arg.lower() == "all" else all_msgs[-(int(count_arg) if count_arg.isdigit() else 5):]

    deleted = failed = 0
    for msg_id in reversed(to_delete):
        try:
            await context.bot.delete_message(chat_id=target_chat, message_id=msg_id)
            if msg_id in msgs: msgs.remove(msg_id)
            deleted += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning(f"Cannot delete {msg_id}: {e}")
            failed += 1

    await update.message.reply_text(f"Видалено: {deleted} | Не вдалось: {failed}")


async def cmd_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chats — список чатів де бот має збережені повідомлення."""
    if not update.message or update.effective_user.id != ADMIN_USER_ID:
        return
    if not _sent_messages:
        await update.message.reply_text("Немає збережених повідомлень.")
        return
    lines = ["Чати:"] + [f"  {cid}: {len(m)} шт." for cid, m in _sent_messages.items()]
    lines.append("\n/clean [N|all] [chat_id]")
    await update.message.reply_text("\n".join(lines))

# ──────────────────────────────────────────
# APP FACTORY
# ──────────────────────────────────────────
def create_application() -> Application:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не встановлено!")
    _init_cookies()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("clean", cmd_clean))
    app.add_handler(CommandHandler("chats", cmd_chats))
    logger.info("Бот запущено | Instagram + Facebook (yt-dlp + cookies)")
    logger.info(f"Cookies: {'OK' if _COOKIES_FILE else 'НЕ ВСТАНОВЛЕНО'}")
    logger.info(f"Admin: {ADMIN_USER_ID or 'не встановлено'}")
    return app
