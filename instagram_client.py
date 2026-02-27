"""
instagram_client.py

Модуль для завантаження медіа з Instagram через instagrapi.
Використовується як fallback коли yt-dlp не може обробити фото/каруселі.

Підтримує:
  - Фото пости      (media_type = 1)
  - Відео пости     (media_type = 2)
  - Каруселі        (media_type = 8) — повертає перший медіа файл
  - Reels           (media_type = 2, product_type = clips)
"""

import os
import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy import — не падаємо якщо instagrapi не встановлено
try:
    from instagrapi import Client
    from instagrapi.exceptions import (
        LoginRequired,
        MediaNotFound,
        ClientError,
    )
    INSTAGRAPI_AVAILABLE = True
except ImportError:
    INSTAGRAPI_AVAILABLE = False
    logger.warning("instagrapi not installed — Instagram photo fallback disabled")


# ──────────────────────────────────────────
# MEDIA TYPE CONSTANTS
# ──────────────────────────────────────────
MEDIA_TYPE_PHOTO   = 1
MEDIA_TYPE_VIDEO   = 2
MEDIA_TYPE_ALBUM   = 8  # каруселька


class InstagramClient:
    """
    Обгортка навколо instagrapi.Client з lazy login і перевикористанням сесії.
    """

    def __init__(self):
        self._client: "Client | None" = None
        self._logged_in = False

    def _get_client(self) -> "Client":
        """Повертає авторизований клієнт. Логіниться якщо ще не авторизований."""
        if self._client is None:
            self._client = Client()
            # Налаштування затримок щоб не отримати бан
            self._client.delay_range = [1, 3]

        if not self._logged_in:
            self._login()

        return self._client

    def _login(self) -> None:
        """Логін через username/password з env variables."""
        username = os.environ.get("INSTAGRAM_USERNAME", "")
        password = os.environ.get("INSTAGRAM_PASSWORD", "")

        if not username or not password:
            raise ValueError(
                "INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD must be set "
                "in environment variables for instagrapi"
            )

        # Спроба відновити сесію з файлу (щоб не логінитись кожного разу)
        session_file = "/tmp/instagrapi_session.json"
        if Path(session_file).exists():
            try:
                self._client.load_settings(session_file)
                self._client.login(username, password)
                logger.info("instagrapi: Session restored from file")
                self._logged_in = True
                return
            except Exception as e:
                logger.warning(f"instagrapi: Session restore failed: {e}, doing fresh login")

        # Свіжий логін
        self._client.login(username, password)
        self._client.dump_settings(session_file)
        logger.info(f"instagrapi: Logged in as @{username}")
        self._logged_in = True

    def download(self, url: str, output_dir: str) -> tuple[list[str], str]:
        """
        Завантажує медіа з Instagram URL.

        Returns:
            (files, media_type) де:
              files      — список шляхів до завантажених файлів
              media_type — 'photo', 'video', або 'album'

        Raises:
            Exception якщо завантаження не вдалось
        """
        if not INSTAGRAPI_AVAILABLE:
            raise RuntimeError("instagrapi is not installed")

        client = self._get_client()

        # Отримуємо ID медіа з URL
        media_pk = client.media_pk_from_url(url)
        logger.info(f"instagrapi: media_pk = {media_pk}")

        # Отримуємо інфо про пост
        media_info = client.media_info(media_pk)
        media_type_id = media_info.media_type
        logger.info(f"instagrapi: media_type = {media_type_id} | product_type = {media_info.product_type}")

        output_path = Path(output_dir)

        if media_type_id == MEDIA_TYPE_PHOTO:
            # Одне фото
            downloaded = client.photo_download(media_pk, folder=output_path)
            logger.info(f"instagrapi: Photo downloaded: {downloaded}")
            return [str(downloaded)], "photo"

        elif media_type_id == MEDIA_TYPE_VIDEO:
            # Відео або Reel
            downloaded = client.video_download(media_pk, folder=output_path)
            logger.info(f"instagrapi: Video downloaded: {downloaded}")
            return [str(downloaded)], "video"

        elif media_type_id == MEDIA_TYPE_ALBUM:
            # Каруселька — завантажуємо всі файли
            downloaded_list = client.album_download(media_pk, folder=output_path)
            files = [str(f) for f in downloaded_list]
            logger.info(f"instagrapi: Album downloaded: {len(files)} files")
            return files, "album"

        else:
            raise ValueError(f"Unknown media_type: {media_type_id}")

    def reset(self) -> None:
        """Скидає сесію (корисно після бану або помилки авторизації)."""
        self._client = None
        self._logged_in = False
        session_file = "/tmp/instagrapi_session.json"
        if Path(session_file).exists():
            Path(session_file).unlink()
        logger.info("instagrapi: Session reset")


# ──────────────────────────────────────────
# Singleton — один клієнт на весь процес
# ──────────────────────────────────────────
_instagram_client = InstagramClient()


def download_instagram_media(url: str, output_dir: str) -> tuple[str | None, str]:
    """
    Публічна функція для завантаження Instagram медіа через instagrapi.

    Returns:
        (filepath, media_type) де media_type = 'photo' | 'video' | 'unknown'
        Для album повертає перший файл.

    Використовується як fallback в bot.py коли yt-dlp не може обробити пост.
    """
    if not INSTAGRAPI_AVAILABLE:
        logger.error("instagrapi not available")
        return None, "unknown"

    try:
        files, media_type = _instagram_client.download(url, output_dir)

        if not files:
            logger.error("instagrapi: No files downloaded")
            return None, "unknown"

        if media_type == "album":
            # Для каруселі повертаємо перший файл
            # TODO: в майбутньому можна відправляти всі файли як media group
            logger.info(f"instagrapi: Album — returning first of {len(files)} files")
            return files[0], "photo"

        return files[0], media_type

    except ValueError as e:
        # Невірні credentials
        logger.error(f"instagrapi: Credentials error: {e}")
        return None, "unknown"

    except Exception as e:
        error_str = str(e)

        # Якщо сесія протухла — скидаємо і повідомляємо
        if "login_required" in error_str.lower() or "LoginRequired" in error_str:
            logger.warning("instagrapi: Session expired, resetting...")
            _instagram_client.reset()

        logger.error(f"instagrapi: Download failed: {e}")
        return None, "unknown"


def is_available() -> bool:
    """Перевіряє чи instagrapi встановлено і credentials задані."""
    if not INSTAGRAPI_AVAILABLE:
        return False
    has_creds = bool(
        os.environ.get("INSTAGRAM_USERNAME") and
        os.environ.get("INSTAGRAM_PASSWORD")
    )
    return has_creds
