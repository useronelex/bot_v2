import os
import re
import json
import logging
import asyncio
import tempfile
import random
import time
from collections import deque
from pathlib import Path
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# curl_cffi імітує точний TLS-fingerprint Chrome — Instagram не відрізняє від браузера
from curl_cffi import requests as cffi_requests

# ──────────────────────────────────────────
# RATE LIMIT — per user (без змін)
# ──────────────────────────────────────────
REQUEST_LIMIT  = 50
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
# URL PATTERNS — додано сторіз
# ──────────────────────────────────────────
# Покриває: /p/ /reel/ /reels/ /tv/ — пости та відео
INSTAGRAM_POST_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:reels?|p|tv)/([A-Za-z0-9_\-]+)(?:/[^\s]*)?'
)
# Покриває: /stories/username/media_id/
INSTAGRAM_STORY_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/stories/([A-Za-z0-9_\.]+)/(\d+)(?:/[^\s]*)?'
)
FACEBOOK_URL_PATTERN = re.compile(
    r'https?://(?:www\.|m\.|web\.)?facebook\.com/(?:watch/?\?v=|[\w\-\.]+/videos/|share/[vr]/)[\d\w\-]+'
)

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

# Residential proxy — якщо є в env, використовуємо для Instagram запитів
# Формат: "http://user:pass@host:port"
RESIDENTIAL_PROXY = os.environ.get("RESIDENTIAL_PROXY", "")

# ──────────────────────────────────────────
# USER AGENTS (без змін)
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
# COOKIES — парсимо з env у словник
# ──────────────────────────────────────────
_INSTAGRAM_COOKIES: dict = {}

def _init_cookies() -> None:
    """
    Завантажує cookies з env-змінної.
    Підтримує два формати:
      1. Netscape/cookies.txt (рядки з табуляцією) — як використовував yt-dlp
      2. JSON: {"sessionid": "...", "csrftoken": "...", ...}
    """
    global _INSTAGRAM_COOKIES
    raw = os.environ.get("INSTAGRAM_COOKIES", "").strip()
    if not raw:
        logger.warning("INSTAGRAM_COOKIES not set — приватний контент недоступний")
        return

    # Спроба розпарсити як JSON
    if raw.startswith("{"):
        try:
            _INSTAGRAM_COOKIES = json.loads(raw)
            logger.info(f"Instagram cookies loaded from JSON ({len(_INSTAGRAM_COOKIES)} keys)")
            return
        except json.JSONDecodeError:
            pass

    # Парсимо Netscape cookies.txt формат (той самий файл що використовував yt-dlp)
    cookies = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            # Формат: domain  flag  path  secure  expiry  name  value
            name, value = parts[5], parts[6]
            cookies[name] = value

    if cookies:
        _INSTAGRAM_COOKIES = cookies
        logger.info(f"Instagram cookies loaded from Netscape format ({len(cookies)} cookies)")
    else:
        logger.warning("Could not parse INSTAGRAM_COOKIES")


# ──────────────────────────────────────────
# URL EXTRACTOR — тепер розрізняє тип контенту
# ──────────────────────────────────────────
def extract_url(text: str) -> tuple[str, str, str] | None:
    """
    Повертає (url, platform, content_type).
    content_type: 'post' | 'story' | 'facebook'
    """
    # Спочатку перевіряємо сторіз (більш специфічний патерн)
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
# МЕТОД 1: Зовнішній сервіс (для публічного контенту)
# Пробуємо декілька сервісів по черзі — якщо один впав, йдемо до наступного
# ──────────────────────────────────────────
def _try_external_service(url: str, output_dir: str) -> str | None:
    """
    Пробуємо декілька публічних сервісів по черзі.
    Кожен може впасти або змінити API — тому каскад і тут.
    """
    # Сервіс 1: igram.world — стабільний, JSON API
    result = _try_igram(url, output_dir)
    if result:
        return result

    # Сервіс 2: instafinsta як резерв
    result = _try_instafinsta(url, output_dir)
    if result:
        return result

    return None


def _try_igram(url: str, output_dir: str) -> str | None:
    """igram.world — надійний публічний downloader з JSON відповіддю."""
    try:
        api_url = "https://igram.world/api/convert"
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": "https://igram.world/",
            "Origin": "https://igram.world",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = cffi_requests.post(
            api_url,
            data={"url": url, "lang": "en"},
            headers=headers,
            impersonate="chrome110",
            timeout=15,
        )
        if resp.status_code != 200:
            logger.info(f"igram returned {resp.status_code}")
            return None

        data = resp.json()
        # igram повертає список медіа — шукаємо відео
        items = data if isinstance(data, list) else data.get("media", [])
        for item in items:
            src = item.get("url") or item.get("src", "")
            if src and ("mp4" in src or item.get("type", "") == "video"):
                logger.info("igram.world succeeded")
                return _download_direct_url(src, output_dir, use_proxy=False)

        logger.info("igram: no video in response")
        return None
    except Exception as e:
        logger.info(f"igram failed: {e}")
        return None


def _try_instafinsta(url: str, output_dir: str) -> str | None:
    """saveig.app як резервний варіант після igram."""
    try:
        api_url = "https://saveig.app/api/ajaxSearch"
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": "https://saveig.app/",
            "Origin": "https://saveig.app",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = cffi_requests.post(
            api_url,
            data={"q": url, "t": "media", "lang": "en"},
            headers=headers,
            impersonate="chrome110",
            timeout=15,
        )
        if resp.status_code != 200:
            logger.info(f"saveig returned {resp.status_code}")
            return None

        data = resp.json()
        # saveig повертає HTML всередині JSON поля "data" — парсимо посилання
        html = data.get("data", "")
        # Шукаємо пряме посилання на mp4
        import re as _re
        matches = _re.findall(r'href=["\']([^"\']+\.mp4[^"\']*)["\']', html)
        if matches:
            logger.info("saveig succeeded")
            return _download_direct_url(matches[0], output_dir, use_proxy=False)

        logger.info("saveig: no video link in response")
        return None
    except Exception as e:
        logger.info(f"saveig failed: {e}")
        return None


# ──────────────────────────────────────────
# МЕТОД 2: curl_cffi з cookies акаунту
# Для приватного контенту, сторіз, 18+
# Instagram бачить "Chrome браузер" з авторизованою сесією
# ──────────────────────────────────────────
def _try_instagram_api(url: str, output_dir: str, content_type: str) -> str | None:
    """
    Завантаження через Instagram Graph API / приватний API
    з використанням cookies авторизованого акаунту.

    Принцип: надсилаємо запит точно так, як це робить мобільний Chrome,
    включно з TLS fingerprint (це і є головна перевага curl_cffi над yt-dlp).
    """
    if not _INSTAGRAM_COOKIES:
        logger.warning("No cookies — skipping authenticated request")
        return None

    try:
        # Налаштування проксі (residential IP якщо є)
        proxies = {}
        if RESIDENTIAL_PROXY:
            proxies = {"https": RESIDENTIAL_PROXY, "http": RESIDENTIAL_PROXY}
            logger.info("Using residential proxy")

        session = cffi_requests.Session()
        session.cookies.update(_INSTAGRAM_COOKIES)

        # Визначаємо shortcode або media_id з URL
        if content_type == "story":
            match = INSTAGRAM_STORY_PATTERN.search(url)
            if not match:
                return None
            username, media_id = match.group(1), match.group(2)
            return _download_story(session, username, media_id, output_dir, proxies)
        else:
            match = INSTAGRAM_POST_PATTERN.search(url)
            if not match:
                return None
            shortcode = match.group(1)
            return _download_post(session, shortcode, output_dir, proxies)

    except Exception as e:
        logger.error(f"Instagram API error: {e}", exc_info=True)
        return None


def _download_post(session, shortcode: str, output_dir: str, proxies: dict) -> str | None:
    """
    Завантажує пост/reel через каскад трьох методів:

    1. og:video з HTML сторінки — найстабільніший метод.
       Instagram вбудовує пряме CDN-посилання на відео прямо в HTML
       у мета-тезі <meta property="og:video">, саме цим користується
       Telegram коли показує прев'ю посилань. З cookies це працює і для 18+.

    2. GraphQL /graphql/query/ — швидший але нестабільний:
       doc_id змінюється з кожним оновленням Instagram.

    3. Мобільний API /api/v1/media/shortcode/info/ — як останній резерв.
    """
    # ── Метод 1: og:video з HTML ─────────────────────────────────────────────
    result = _download_post_ogvideo(session, shortcode, output_dir, proxies)
    if result:
        return result

    # ── Метод 2: GraphQL (якщо og:video не спрацював) ───────────────────────
    result = _download_post_graphql(session, shortcode, output_dir, proxies)
    if result:
        return result

    # ── Метод 3: Мобільний API ───────────────────────────────────────────────
    return _download_post_mobile_api(session, shortcode, output_dir, proxies)


def _download_post_ogvideo(session, shortcode: str, output_dir: str, proxies: dict) -> str | None:
    """
    Метод 1: завантажуємо HTML сторінку поста і витягуємо og:video URL.

    Це той самий підхід що використовує Telegram для прев'ю посилань —
    дуже стабільний бо Instagram не може його "зламати" не зламавши
    при цьому всі месенджери і соціальні мережі одночасно.
    """
    try:
        page_url = f"https://www.instagram.com/p/{shortcode}/"
        headers = {
            # Використовуємо десктопний Chrome — він отримує повний HTML з мета-тегами
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        }
        resp = session.get(
            page_url,
            headers=headers,
            impersonate="chrome120",  # найновіший fingerprint
            proxies=proxies or None,
            timeout=20,
        )

        if resp.status_code != 200:
            logger.info(f"og:video page returned {resp.status_code} for {shortcode}")
            return None

        html = resp.text

        # Шукаємо og:video — це пряме CDN посилання на відео
        # Instagram вставляє його для всіх відео-постів і рілсів
        match = re.search(r'<meta property="og:video" content="([^"]+)"', html)
        if not match:
            # Альтернативний формат мета-тегу
            match = re.search(r'<meta content="([^"]+)" property="og:video"', html)

        if match:
            video_url = match.group(1).replace("&amp;", "&")
            logger.info(f"og:video: found video URL for {shortcode}")
            return _download_direct_url(video_url, output_dir, use_proxy=bool(proxies), proxies=proxies)

        # og:video не знайдено — можливо це фото пост або Instagram не вставив мета-тег
        logger.info(f"og:video: no video meta tag for {shortcode}")
        return None

    except Exception as e:
        logger.info(f"og:video failed for {shortcode}: {e}")
        return None


def _download_post_graphql(session, shortcode: str, output_dir: str, proxies: dict) -> str | None:
    """
    Метод 2: GraphQL API.
    doc_id може застаріти — тому це вже не основний а резервний метод.
    """
    try:
        graphql_url = "https://www.instagram.com/graphql/query/"
        params = {
            "doc_id": "8845758582119845",
            "variables": json.dumps({"shortcode": shortcode, "fetch_tagged_user_count": None}),
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-IG-App-ID": "936619743392459",
            "X-CSRFToken": session.cookies.get("csrftoken", ""),
            "Referer": f"https://www.instagram.com/p/{shortcode}/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        resp = session.get(
            graphql_url,
            params=params,
            headers=headers,
            impersonate="chrome110",
            proxies=proxies or None,
            timeout=20,
        )
        if resp.status_code != 200:
            logger.info(f"GraphQL returned {resp.status_code} for {shortcode}")
            return None

        data = resp.json()
        media = data.get("data", {}).get("xdt_shortcode_media")
        if media is None:
            logger.info(f"GraphQL: media is None for {shortcode} (doc_id можливо застарів)")
            return None

        video_url = media.get("video_url")
        if video_url:
            logger.info(f"GraphQL: found video for {shortcode}")
            return _download_direct_url(video_url, output_dir, use_proxy=bool(proxies), proxies=proxies)

        # Карусель
        edges = media.get("edge_sidecar_to_children", {}).get("edges", [])
        for edge in edges:
            node = edge.get("node", {})
            if node.get("is_video") and node.get("video_url"):
                logger.info(f"GraphQL: found carousel video for {shortcode}")
                return _download_direct_url(node["video_url"], output_dir, use_proxy=bool(proxies), proxies=proxies)

    except Exception as e:
        logger.info(f"GraphQL failed: {e}")
    return None


def _download_post_mobile_api(session, shortcode: str, output_dir: str, proxies: dict) -> str | None:
    """
    Метод 3: мобільний API endpoint як останній резерв.
    """
    try:
        url = f"https://www.instagram.com/api/v1/media/{shortcode}/info/"
        headers = {
            "User-Agent": "Instagram 275.0.0.27.98 Android",
            "X-IG-App-ID": "567067343352427",
        }
        resp = session.get(
            url,
            headers=headers,
            impersonate="chrome110",
            proxies=proxies or None,
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"Mobile API returned {resp.status_code} for {shortcode}")
            return None

        data = resp.json()
        for item in data.get("items", []):
            video_versions = item.get("video_versions", [])
            if video_versions:
                video_url = video_versions[0]["url"]
                logger.info(f"Mobile API: found video for {shortcode}")
                return _download_direct_url(video_url, output_dir, use_proxy=bool(proxies), proxies=proxies)
    except Exception as e:
        logger.warning(f"Mobile API failed: {e}")
    return None


def _download_story(session, username: str, media_id: str, output_dir: str, proxies: dict) -> str | None:
    """
    Завантажує сторіз через Instagram Stories API.
    Сторіз доступні тільки для підписників приватних акаунтів
    або публічно — для публічних профілів.
    """
    # Спочатку отримуємо user_id за username
    user_info_url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "X-IG-App-ID": "936619743392459",
        "Referer": f"https://www.instagram.com/{username}/",
    }

    resp = session.get(
        user_info_url,
        headers=headers,
        impersonate="chrome110",
        proxies=proxies or None,
        timeout=15,
    )

    if resp.status_code != 200:
        logger.warning(f"Could not get user info for {username}: {resp.status_code}")
        return None

    try:
        user_data = resp.json()
        user_id = user_data["data"]["user"]["id"]
    except (KeyError, json.JSONDecodeError):
        logger.warning(f"Could not parse user_id for {username}")
        return None

    # Тепер отримуємо сторіз цього користувача
    stories_url = f"https://www.instagram.com/api/v1/feed/reels_media/?reel_ids={user_id}"
    resp = session.get(
        stories_url,
        headers=headers,
        impersonate="chrome110",
        proxies=proxies or None,
        timeout=15,
    )

    if resp.status_code != 200:
        logger.warning(f"Stories API returned {resp.status_code}")
        return None

    try:
        stories_data = resp.json()
        reels = stories_data.get("reels", {})
        reel = reels.get(user_id, reels.get(str(user_id), {}))
        items = reel.get("items", [])

        # Знаходимо конкретну сторіз за media_id
        # Instagram іноді повертає складений ID типу "3871424810811404248_123456789"
        # тому перевіряємо чи ID починається з потрібного числа
        for item in items:
            item_pk = str(item.get("pk", ""))
            item_id = str(item.get("id", ""))
            # Співпадіння якщо pk == media_id АБО id починається з media_id
            if item_pk == media_id or item_id == media_id or item_id.startswith(f"{media_id}_"):
                if item.get("media_type") == 2:  # 2 = відео в Instagram API
                    # Беремо відео найкращої якості
                    video_versions = item.get("video_versions", [])
                    if video_versions:
                        video_url = video_versions[0]["url"]
                        logger.info(f"Found story video for {username}/{media_id}")
                        return _download_direct_url(video_url, output_dir, use_proxy=bool(proxies), proxies=proxies)
                else:
                    logger.info(f"Story {media_id} is a photo, not video")
                    return None

        logger.warning(f"Story {media_id} not found in feed (можливо вже видалено або закінчився термін)")
        return None

    except Exception as e:
        logger.error(f"Story parsing error: {e}", exc_info=True)
        return None


# ──────────────────────────────────────────
# МЕТОД 3: Facebook через yt-dlp (залишаємо як є, там він добре працює)
# ──────────────────────────────────────────
def _download_facebook(url: str, output_dir: str) -> str | None:
    """Facebook завантаження через yt-dlp — там він справляється добре."""
    try:
        import yt_dlp

        output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
        user_agent = random.choice(USER_AGENTS)
        ydl_opts = {
            "outtmpl": output_template,
            "quiet": False,
            "format": "best[ext=mp4][height<=1080]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "max_filesize": 50 * 1024 * 1024,
            "http_headers": {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None
            for f in Path(output_dir).iterdir():
                if f.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv"):
                    return str(f)
        return None
    except Exception as e:
        logger.error(f"Facebook download error: {e}")
        return None


# ──────────────────────────────────────────
# ДОПОМІЖНА: завантажити файл за прямим URL
# ──────────────────────────────────────────
def _download_direct_url(
    video_url: str,
    output_dir: str,
    use_proxy: bool = False,
    proxies: dict | None = None,
    chunk_size: int = 1024 * 1024,  # 1MB chunks
) -> str | None:
    """
    Завантажує файл за прямим CDN-посиланням.
    Стримінгове завантаження по шматках — не навантажує RAM.
    """
    try:
        output_path = os.path.join(output_dir, "video.mp4")
        resp = cffi_requests.get(
            video_url,
            impersonate="chrome110",
            proxies=proxies if use_proxy and proxies else None,
            timeout=60,
            stream=True,  # стримінг — не чекаємо поки весь файл буферизується
        )

        if resp.status_code != 200:
            logger.warning(f"Direct download returned {resp.status_code}")
            return None

        total_size = 0
        max_size = 50 * 1024 * 1024  # 50MB ліміт Telegram

        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    total_size += len(chunk)
                    if total_size > max_size:
                        logger.warning("File exceeds 50MB limit, aborting")
                        return None
                    f.write(chunk)

        logger.info(f"Downloaded {total_size / 1024 / 1024:.2f} MB to {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"Direct download error: {e}")
        return None


# ──────────────────────────────────────────
# ГОЛОВНА ФУНКЦІЯ — КАСКАД МЕТОДІВ
# ──────────────────────────────────────────
def download_media(url: str, output_dir: str, platform: str, content_type: str) -> tuple[str | None, str]:
    """
    Каскадний підхід:
      1. Зовнішній сервіс (швидко, публічний контент, не навантажує акаунт)
      2. curl_cffi + cookies (приватне, сторіз, 18+, з residential proxy якщо є)
      3. yt-dlp (тільки для Facebook)

    Повертає (filepath, media_type_string).
    """
    if platform == "facebook":
        path = _download_facebook(url, output_dir)
        return (path, "video") if path else (None, "unknown")

    # Instagram — спочатку пробуємо зовнішній сервіс
    # Він не потребує наших cookies і не ризикує акаунтом
    logger.info(f"[Instagram] Step 1: Trying external service | type={content_type}")
    time.sleep(random.uniform(0.3, 1.0))  # пауза щоб не виглядати як бот

    # Сторіз не мають публічного доступу через зовнішні сервіси — одразу переходимо до крок 2
    if content_type != "story":
        path = _try_external_service(url, output_dir)
        if path and Path(path).exists():
            logger.info("[Instagram] External service succeeded ✓")
            return path, "video"
        logger.info("[Instagram] External service failed — trying authenticated request")

    # Крок 2: curl_cffi з нашими cookies
    logger.info(f"[Instagram] Step 2: Authenticated curl_cffi | cookies={'yes' if _INSTAGRAM_COOKIES else 'no'}")
    path = _try_instagram_api(url, output_dir, content_type)
    if path and Path(path).exists():
        logger.info("[Instagram] Authenticated request succeeded ✓")
        return path, "video"

    logger.warning("[Instagram] All methods failed")
    return None, "unknown"


# ──────────────────────────────────────────
# TYPING INDICATOR (без змін)
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
# RATE LIMIT (без змін)
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
# HANDLER — оновлено для нового extract_url
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

    media_url, platform, content_type = url_info
    logger.info(f"Processing {platform.upper()} [{content_type}] | user_id={user_id} | {media_url}")

    typing_task = asyncio.create_task(
        keep_uploading_action(message.chat_id, context.bot, "video")
    )

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Запускаємо синхронний download в окремому потоці
            # щоб не блокувати async event loop Telegram
            media_path, media_type = await asyncio.get_event_loop().run_in_executor(
                None, download_media, media_url, tmp_dir, platform, content_type
            )

            if not media_path or not Path(media_path).exists():
                logger.warning(f"Download failed [{platform}/{content_type}]: {media_url}")
                err = await message.reply_text(
                    "Не вдалося завантажити.\n"
                    "Можливі причини: приватний акаунт без підписки бота, "
                    "сторіз вже видалено або контент недоступний.",
                    reply_to_message_id=message.message_id
                )
                await asyncio.sleep(10)
                try:
                    await err.delete()
                except Exception:
                    pass
                return

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

            typing_task.cancel()
            typing_task = asyncio.create_task(
                keep_uploading_action(message.chat_id, context.bot, media_type)
            )

            try:
                with open(media_path, "rb") as f:
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
# APP FACTORY (без змін)
# ──────────────────────────────────────────
def create_application() -> Application:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set!")
    _init_cookies()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    logger.info("Bot started | Platforms: Instagram (posts/reels/stories), Facebook")
    logger.info(f"Instagram cookies: {'loaded' if _INSTAGRAM_COOKIES else 'NOT SET'}")
    logger.info(f"Residential proxy: {'configured' if RESIDENTIAL_PROXY else 'not set (datacenter IP)'}")
    return app
