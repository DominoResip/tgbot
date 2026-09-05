from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Also accept ADMIN_IDS from env; always include bot owner if listed.
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
ADMIN_IDS.update({799402938, 482753633})

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))
TZ_NAME = os.getenv("TZ", "Asia/Novokuznetsk")
TZ = ZoneInfo(TZ_NAME)

# Keep archived site days this many calendar days (by schedule date).
# 5 covers Fri→Mon gaps (weekend) without growing the DB much.
ARCHIVE_KEEP_DAYS = int(os.getenv("ARCHIVE_KEEP_DAYS", "5"))
# Drop chats with no activity longer than this.
INACTIVE_CHAT_DAYS = int(os.getenv("INACTIVE_CHAT_DAYS", "30"))

# Расписание звонков 1 корпуса (1:20 на пару).
BELLS_DEFAULT: dict[int, str] = {
    1: "08:30–09:50",
    2: "10:00–11:20",
    3: "11:50–13:10",
    4: "13:30–14:50",
    5: "14:55–16:15",
    6: "16:20–17:40",
    7: "17:45–19:05",
}

# Расписание звонков 2 корпуса (сдвинутые перемены).
BELLS_CORPUS_2: dict[int, str] = {
    1: "08:30–09:50",
    2: "10:00–11:20",
    3: "11:40–13:00",
    4: "13:20–14:40",
    5: "14:50–16:10",
    6: "16:15–17:35",
    7: "17:40–19:00",
}

CORPORA: dict[str, dict] = {
    "1": {
        "id": "1",
        "title": "1 корпус",
        "short": "1к",
        "base": os.getenv(
            "SCHEDULE_BASE_1", "https://schedule.spt42.ru/1_korpus"
        ).rstrip("/"),
        "bells": dict(BELLS_DEFAULT),
    },
    "2": {
        "id": "2",
        "title": "2 корпус",
        "short": "2к",
        "base": os.getenv(
            "SCHEDULE_BASE_2", "https://schedule.spt42.ru/2_korpus"
        ).rstrip("/"),
        "bells": dict(BELLS_CORPUS_2),
    },
}

SCHEDULE_BASE = CORPORA["1"]["base"]
BELLS = BELLS_DEFAULT

MORNING_HOUR = int(os.getenv("MORNING_HOUR", "8"))
MORNING_MINUTE = int(os.getenv("MORNING_MINUTE", "0"))
# Pause between morning messages to different chats (seconds).
MORNING_SEND_DELAY = float(os.getenv("MORNING_SEND_DELAY", "0.35"))
# Pause between "schedule changed" notifications (seconds). ~1200 chats → ~7 min at 0.35s.
NOTIFY_SEND_DELAY = float(os.getenv("NOTIFY_SEND_DELAY", "0.35"))

WEATHER_LAT = float(os.getenv("WEATHER_LAT", "55.3333"))
WEATHER_LON = float(os.getenv("WEATHER_LON", "86.0833"))
WEATHER_CITY = os.getenv("WEATHER_CITY", "Кемерово")

USER_AGENT = (
    "Mozilla/5.0 (compatible; SptScheduleBot/1.1; +https://schedule.spt42.ru)"
)

_db = os.getenv("DB_PATH", "").strip()
DB_PATH = str(Path(_db) if _db else ROOT / "data" / "sptbot.sqlite3")
PAGE_SIZE = 12

FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "90"))
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "4"))
BOOTSTRAP_RETRIES = int(os.getenv("BOOTSTRAP_RETRIES", "5"))


def corpus_meta(corpus_id: str) -> dict:
    return CORPORA.get(corpus_id) or CORPORA["1"]


def bells_for(corpus_id: str) -> dict[int, str]:
    return corpus_meta(corpus_id).get("bells") or BELLS_DEFAULT
