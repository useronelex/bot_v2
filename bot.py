import os
import re
import logging
import asyncio
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

INSTAGRAM_URL_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_\-]+(?:/[^\s]*)?'
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # Наприклад: https://your-app.onrender.com


def extract_instagram_url(text: str) -> str | None:
    match = INSTAGRAM_URL_PATTERN.search(text)
    return match.group(0) if match else None


def download_video(url: str, output_dir: str) -> str | None:
    """Download Instagram video using yt-dlp. Returns filepath or None."""
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    
    ydl_opts = {
        "outtmpl": output_template,
        "format": "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
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

    instagram_url = extract_instagram_url(message.text)
    if not instagram_url:
        return

    logger.info(f"Processing Instagram URL: {instagram_url}")
    
    # Show "uploading video" action
    await context.bot.send_chat_action(
        chat_id=message.chat_id,
        action="upload_video"
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        video_path = await asyncio.get_event_loop().run_in_executor(
            None, download_video, instagram_url, tmp_dir
        )

        if not video_path or not Path(video_path).exists():
            logger.warning(f"Failed to download: {instagram_url}")
            await message.reply_text(
                "❌ Не вдалося завантажити відео. Можливо, воно приватне або недоступне.",
                reply_to_message_id=message.message_id
            )
            return

        file_size = Path(video_path).stat().st_size
        if file_size > 50 * 1024 * 1024:
            await message.reply_text(
                "❌ Відео завелике для відправки (понад 50 МБ).",
                reply_to_message_id=message.message_id
            )
            return

        try:
            with open(video_path, "rb") as video_file:
                await context.bot.send_video(
                    chat_id=message.chat_id,
                    video=video_file,
                    supports_streaming=True,
                    caption=f"📲 @{message.from_user.username or message.from_user.first_name}" 
                            if message.chat.type in ("group", "supergroup") else None
                )
            
            # Delete original message with the link
            try:
                await message.delete()
            except Exception as e:
                logger.warning(f"Could not delete original message: {e}")

        except Exception as e:
            logger.error(f"Failed to send video: {e}")
            await message.reply_text(
                "❌ Помилка при відправці відео. Спробуйте пізніше.",
                reply_to_message_id=message.message_id
            )


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
