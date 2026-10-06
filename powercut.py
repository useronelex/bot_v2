"""
powercut.py — незалежний моніторинг графіків відключень світла.

Читає публічну веб-версію Telegram-каналу (https://t.me/s/<канал>), витягує графіки
для заданих черг і надсилає в чат повідомлення ЛИШЕ коли графік з'явився або змінився.

"""

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import RetryAfter

logger = logging.getLogger("powercut")

# ──────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("POWERCUT_CHAT_ID"))
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID") or "0")
CHANNEL = os.environ.get("POWERCUT_CHANNEL", "pat_cherkasyoblenergo")
QUEUES = tuple(q.strip() for q in os.environ.get("POWERCUT_QUEUES", "1.1,2.1,3.1").split(",") if q.strip())
POLL_INTERVAL = max(5.0, float(os.environ.get("POWERCUT_POLL_SEC") or "10"))
STATE_PATH = os.environ.get("POWERCUT_STATE_PATH", "powercut_state.json")
NOTIFY_ON_FIRST_RUN = os.environ.get("POWERCUT_NOTIFY_FIRST_RUN", "0") == "1"
ALERT_AFTER_SEC = 300  # якщо канал недоступний стільки часів поспіль — сповістити адміна

URL = f"https://t.me/s/{CHANNEL}"

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:  # немає tzdata
    KYIV = timezone(timedelta(hours=3))

MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
MONTH_NAMES = {v: k for k, v in MONTHS.items()}

_DATE_TEXT = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")", re.IGNORECASE)
_DATE_NUM = re.compile(r"(\d{1,2})\.(\d{2})\.(\d{4})")
_QUEUE_LINE = re.compile(r"^\s*(\d\.\d)\s+(.*)$")
_INTERVAL = re.compile(r"(\d{1,2}):(\d{2})\s*(?:[-–—]|до)\s*(\d{1,2}):(\d{2})")

# статус для /health
_status = {"started": None, "last_ok": None, "last_error": None, "restarts": 0}


class _RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"429, retry after {retry_after}s")
        self.retry_after = retry_after


# ──────────────────────────────────────────
# ПАРСИНГ
# ──────────────────────────────────────────
def _now_kyiv() -> datetime:
    return datetime.now(KYIV)


def _parse_date(text: str, today: date) -> date | None:
    m = _DATE_TEXT.search(text)
    try:
        if m:
            day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
            year = today.year
            if month == 1 and today.month == 12:
                year += 1
            elif month == 12 and today.month == 1:
                year -= 1
            return date(year, month, day)
        m = _DATE_NUM.search(text)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return None


def _normalize(pairs: list[list[int]]) -> list[list[int]]:
    """Сортує інтервали й зливає ті, що перетинаються або йдуть впритул."""
    out: list[list[int]] = []
    for s, e in sorted(p for p in pairs if p[0] < p[1]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def parse_message(text: str, today: date) -> tuple[date, dict[str, list[list[int]]]] | None:
    """Повертає (дата, {черга: [[початок_хв, кінець_хв], ...]}) лише для потрібних черг."""
    day = _parse_date(text, today)
    if not day:
        return None
    queues: dict[str, list[list[int]]] = {}
    for line in text.splitlines():
        m = _QUEUE_LINE.match(line)
        if not m or m.group(1) not in QUEUES:
            continue
        pairs = []
        for h1, m1, h2, m2 in _INTERVAL.findall(m.group(2)):
            h1, m1, h2, m2 = int(h1), int(m1), int(h2), int(m2)
            if h1 <= 24 and h2 <= 24 and m1 < 60 and m2 < 60:
                pairs.append([h1 * 60 + m1, h2 * 60 + m2])
        if pairs:
            queues.setdefault(m.group(1), []).extend(pairs)
    queues = {q: _normalize(p) for q, p in queues.items()}
    return (day, queues) if queues else None


def build_current(texts: list[str], today: date) -> dict[date, dict[str, list[list[int]]]]:
    """Останнє (найновіше) повідомлення для дати перекриває попередні — по кожній черзі."""
    current: dict[date, dict[str, list[list[int]]]] = {}
    for text in texts:
        parsed = parse_message(text, today)
        if parsed:
            current.setdefault(parsed[0], {}).update(parsed[1])
    return current


# ──────────────────────────────────────────
# ФОРМАТУВАННЯ
# ──────────────────────────────────────────
def _t(x: int) -> str:
    return f"{x // 60:02d}:{x % 60:02d}"


def _fmt(intervals: list[list[int]] | None) -> str:
    if not intervals:
        return "—"
    return ", ".join(f"{_t(s)}–{_t(e)}" for s, e in intervals)


def _fmt_date(d: date) -> str:
    return f"{d.day} {MONTH_NAMES[d.month]}"


def fmt_new(d: date, queues: dict, title: str = "⚡ Графік відключень на") -> str:
    lines = [f"{title} {_fmt_date(d)}", ""]
    lines += [f"{q}: {_fmt(queues[q])}" for q in QUEUES if q in queues]
    return "\n".join(lines)


def fmt_changed(d: date, old: dict, new: dict, diff: list[str]) -> str:
    lines = [f"🔄 Зміна графіка на {_fmt_date(d)}", ""]
    for q in diff:
        lines += [f"{q}:", f"  було: {_fmt(old.get(q))}", f"  стало: {_fmt(new.get(q))}", ""]
    same = [q for q in QUEUES if (q in new or q in old) and q not in diff]
    if same:
        lines.append("Без змін: " + ", ".join(same))
    return "\n".join(lines).strip()


# ──────────────────────────────────────────
# СТАН
# ──────────────────────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("schedules", {})
        st.setdefault("initialized", True)
        return st
    except FileNotFoundError:
        return {"initialized": False, "schedules": {}}
    except Exception as e:
        logger.error(f"Не вдалося прочитати стан, починаю з нуля: {e}")
        return {"initialized": False, "schedules": {}}


def _save_state(state: dict) -> None:
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        logger.error(f"Не вдалося зберегти стан: {e}")


# ──────────────────────────────────────────
# МЕРЕЖА / TELEGRAM
# ──────────────────────────────────────────
async def _fetch_texts(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(URL)
    if r.status_code == 429:
        try:
            ra = float(r.headers.get("Retry-After", "60"))
        except ValueError:
            ra = 60.0
        raise _RateLimited(ra)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    texts = []
    for el in soup.select("div.tgme_widget_message_text"):
        for br in el.find_all("br"):
            br.replace_with("\n")
        texts.append(el.get_text())
    if not texts:
        raise RuntimeError("На сторінці каналу не знайдено повідомлень")
    return texts


async def _send(bot: Bot, chat_id: int, text: str) -> bool:
    for _ in range(3):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        except RetryAfter as e:
            ra = e.retry_after
            await asyncio.sleep((ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)) + 1)
        except Exception as e:
            logger.error(f"Помилка відправки в {chat_id}: {e}")
            await asyncio.sleep(2)
    return False


async def _process(bot: Bot, texts: list[str], state: dict, today: date) -> None:
    schedules: dict = state["schedules"]

    for k in list(schedules):  # прибираємо минулі дні
        try:
            if date.fromisoformat(k) < today:
                del schedules[k]
        except ValueError:
            del schedules[k]

    first_run = not state.get("initialized")
    dirty = False

    for d, queues in sorted(build_current(texts, today).items()):
        if d < today:
            continue
        key = d.isoformat()
        old = schedules.get(key)

        if old is None:
            if first_run and not NOTIFY_ON_FIRST_RUN:
                schedules[key] = queues
                dirty = True
                continue
            msg = fmt_new(d, queues)
        else:
            diff = [q for q in QUEUES if q in queues and old.get(q) != queues[q]]
            if not diff:
                continue
            msg = fmt_changed(d, old, queues, diff)

        if await _send(bot, CHAT_ID, msg):
            logger.info(f"Надіслано сповіщення про графік на {key}")
            schedules[key] = {**(old or {}), **queues}
            dirty = True
        else:
            logger.error(f"Не вдалося надіслати сповіщення на {key}, повторю наступного циклу")

    if first_run:
        state["initialized"] = True
        dirty = True
    if dirty:
        _save_state(state)


# ──────────────────────────────────────────
# КОМАНДА /graph (тільки адмін)
# ──────────────────────────────────────────
async def current_schedules_text() -> str:
    """Свіжі графіки з каналу (на сьогодні й далі) одним текстом."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    async with httpx.AsyncClient(headers=headers, timeout=15, follow_redirects=True) as client:
        texts = await _fetch_texts(client)
    today = _now_kyiv().date()
    current = {d: q for d, q in build_current(texts, today).items() if d >= today}
    if not current:
        return "Актуальних графіків для черг " + ", ".join(QUEUES) + " у каналі не знайдено."
    return "\n\n".join(fmt_new(d, q, "📋 Поточний графік на") for d, q in sorted(current.items()))


async def cmd_graph(update, context) -> None:
    """/graph — надіслати поточні графіки в групу POWERCUT_CHAT_ID. Лише для ADMIN_USER_ID."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not ADMIN_USER_ID or user.id != ADMIN_USER_ID:
        return
    try:
        text = await current_schedules_text()
        await context.bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        logger.error(f"/graph: {e}")
        await msg.reply_text(f"Не вдалося отримати графік: {e}")
        return
    if msg.chat_id == CHAT_ID:
        try:
            await msg.delete()  # прибираємо саму команду з групи
        except Exception:
            pass
    else:
        await msg.reply_text("Надіслано в групу ✅")


# ──────────────────────────────────────────
# ГОЛОВНИЙ ЦИКЛ
# ──────────────────────────────────────────
async def _monitor(bot: Bot) -> None:
    state = _load_state()
    fails = 0
    fail_since: float | None = None
    alerted = False
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Cache-Control": "no-cache",
        "Accept-Language": "uk,en;q=0.8",
    }
    logger.info(f"Моніторинг запущено: {URL} | черги={QUEUES} | чат={CHAT_ID} | інтервал={POLL_INTERVAL}с")

    async with httpx.AsyncClient(headers=headers, timeout=15, follow_redirects=True) as client:
        while True:
            delay = POLL_INTERVAL * random.uniform(0.8, 1.2)
            try:
                texts = await _fetch_texts(client)
                if fails:
                    logger.info(f"Канал знову доступний після {fails} помилок")
                fails, fail_since = 0, None
                _status["last_ok"] = time.time()
                if alerted and ADMIN_USER_ID:
                    await _send(bot, ADMIN_USER_ID, "✅ Моніторинг каналу відновлено.")
                alerted = False
                await _process(bot, texts, state, _now_kyiv().date())
            except asyncio.CancelledError:
                raise
            except _RateLimited as e:
                fails += 1
                fail_since = fail_since or time.time()
                delay = max(e.retry_after, 60)
                logger.warning(f"t.me обмежив запити, пауза {delay:.0f}с")
            except Exception as e:
                fails += 1
                fail_since = fail_since or time.time()
                _status["last_error"] = f"{type(e).__name__}: {e}"
                delay = min(POLL_INTERVAL * 2 ** min(fails, 6), 120)
                logger.warning(f"Помилка опитування ({fails}): {e}")

            if fail_since and not alerted and ADMIN_USER_ID and time.time() - fail_since >= ALERT_AFTER_SEC:
                alerted = await _send(
                    bot, ADMIN_USER_ID,
                    f"⚠️ Не вдається прочитати канал {CHANNEL} вже понад {ALERT_AFTER_SEC // 60} хв.\n"
                    f"Остання помилка: {_status['last_error']}",
                )
            await asyncio.sleep(delay)


async def _supervisor() -> None:
    """Перезапускає моніторинг після будь-якого збою."""
    backoff = 5
    while True:
        started = time.monotonic()
        try:
            async with Bot(BOT_TOKEN) as bot:
                await _monitor(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _status["last_error"] = f"{type(e).__name__}: {e}"
            _status["restarts"] += 1
            logger.error(f"Моніторинг впав, перезапуск через {backoff}с: {e}", exc_info=True)
        if time.monotonic() - started > 60:
            backoff = 5
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)


def _thread_main() -> None:
    while True:  # на випадок, якщо впаде навіть сам asyncio.run
        try:
            asyncio.run(_supervisor())
        except Exception as e:
            logger.error(f"Потік powercut впав, перезапуск: {e}", exc_info=True)
            time.sleep(5)


_thread: threading.Thread | None = None


def start_in_background() -> None:
    """Запускає моніторинг в окремому потоці зі своїм event loop."""
    global _thread
    if _thread and _thread.is_alive():
        return
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN не встановлено — powercut не запущено")
        return
    _status["started"] = time.time()
    _thread = threading.Thread(target=_thread_main, name="powercut", daemon=True)
    _thread.start()


def status() -> dict:
    return {
        "running": bool(_thread and _thread.is_alive()),
        "last_ok_sec_ago": round(time.time() - _status["last_ok"]) if _status["last_ok"] else None,
        "last_error": _status["last_error"],
        "restarts": _status["restarts"],
    }


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
    asyncio.run(_supervisor())
