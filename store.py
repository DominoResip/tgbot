from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config

log = logging.getLogger("spt.store")

# Bump when DB init/migration logic changes (visible in startup logs).
STORE_SCHEMA = 3

DEFAULT_SETTINGS = {
    "show_teacher": True,
    "show_room": True,
    "show_bells": True,
    "show_empty": False,
    "compact": False,
    "notify": True,
    "notify_morning": False,
    # Groups: if False, only chat admins may use the bot
    "allow_members": True,
    "favorites": [],
    "corpus": "",
}

MAX_FAVORITES = 5


@dataclass
class Chat:
    chat_id: int
    chat_type: str
    entity_id: str = ""
    entity_name: str = ""
    entity_kind: str = "group"
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    title: str = ""
    updated_at: str = ""

    def flag(self, key: str) -> bool:
        return bool(self.settings.get(key, DEFAULT_SETTINGS.get(key, False)))

    @property
    def is_group_chat(self) -> bool:
        return self.chat_type in {"group", "supergroup"}

    @property
    def corpus(self) -> str:
        return str(self.settings.get("corpus") or "")

    def favorites(self) -> list[dict[str, str]]:
        raw = self.settings.get("favorites") or []
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for item in raw:
            if isinstance(item, dict) and item.get("id") and item.get("name"):
                out.append(
                    {
                        "id": str(item["id"]),
                        "name": str(item["name"]),
                        "kind": str(item.get("kind") or "group"),
                        "corpus": str(item.get("corpus") or self.corpus or "1"),
                    }
                )
        # keep all favorites; per-corpus limit applied in add_favorite
        return out

    def favorites_for_corpus(self, corpus_id: str) -> list[dict[str, str]]:
        return [f for f in self.favorites() if f.get("corpus") == corpus_id][:MAX_FAVORITES]

    def all_favorites(self) -> list[dict[str, str]]:
        """Favorites from every corpus (stable order: corpus, name)."""
        favs = self.favorites()
        return sorted(
            favs,
            key=lambda f: (f.get("corpus") or "1", (f.get("name") or "").casefold()),
                str(f.get("corpus") or "1"),
                str(f.get("name") or "").casefold(),
            ),
        )


class Store:
    def __init__(self, path: str = config.DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            # Create base tables first (IF NOT EXISTS won't alter old chats schema).
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    chat_type TEXT NOT NULL,
                    entity_id TEXT DEFAULT '',
                    entity_name TEXT DEFAULT '',
                    entity_kind TEXT DEFAULT 'group',
                    settings_json TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    entity_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_label TEXT DEFAULT '',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS day_archive (
                    corpus_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    day_date TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    kind TEXT DEFAULT 'group',
                    payload_json TEXT NOT NULL,
                    saved_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (corpus_id, entity_id, day_date)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_day_archive_date
                    ON day_archive(day_date);
                """
            )
            # Migrate older DBs missing last_active, then index.
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(chats)").fetchall()
            }
            if "last_active" not in cols:
                conn.execute("ALTER TABLE chats ADD COLUMN last_active TEXT")
                conn.execute(
                    "UPDATE chats SET last_active = COALESCE(updated_at, CURRENT_TIMESTAMP) "
                    "WHERE last_active IS NULL OR last_active = ''"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chats_last_active ON chats(last_active)"
            )
        log.info("sqlite ready at %s (%s chats)", self.path, self.chat_count())


    def get_chat(self, chat_id: int) -> Chat | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return self._chat_from_row(row) if row else None

    def ensure_chat(self, chat_id: int, chat_type: str, title: str = "") -> Chat:
        existing = self.get_chat(chat_id)
        if existing:
            if title and title != existing.title:
                self.set_title(chat_id, title)
                existing.title = title
            self.touch(chat_id)
            existing = self.get_chat(chat_id) or existing
            return existing
        chat = Chat(chat_id=chat_id, chat_type=chat_type, title=title)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chats (chat_id, chat_type, entity_id, entity_name,
                                   entity_kind, settings_json, title,
                                   updated_at, last_active)
                VALUES (?, ?, '', '', 'group', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (chat_id, chat_type, json.dumps(chat.settings, ensure_ascii=False), title),
            )
        return chat

    def touch(self, chat_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE chats
                SET last_active = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (chat_id,),
            )

    def chat_count(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()
        return int(row["n"] if row else 0)

    def purge_inactive_chats(self, days: int = 30) -> int:
        """Remove chats with no activity for more than `days` days."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM chats
                WHERE COALESCE(last_active, updated_at, '1970-01-01')
                      < datetime('now', ?)
                """,
                (f"-{int(days)} days",),
            )
            return int(cur.rowcount or 0)

    def set_title(self, chat_id: int, title: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE chats SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
                (title, chat_id),
            )

    def set_entity(self, chat_id: int, entity_id: str, name: str, kind: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE chats
                SET entity_id = ?, entity_name = ?, entity_kind = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (entity_id, name, kind, chat_id),
            )

    def _save_settings(self, chat: Chat) -> Chat:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE chats SET settings_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (json.dumps(chat.settings, ensure_ascii=False), chat.chat_id),
            )
        return chat

    def set_corpus(
        self, chat_id: int, corpus_id: str, *, clear_entity: bool = True
    ) -> Chat:
        chat = self.get_chat(chat_id)
        if not chat:
            raise KeyError(chat_id)
        old = chat.corpus
        chat.settings["corpus"] = corpus_id
        # Switching corpus clears current entity (different lists), unless caller
        # will set a new entity immediately (e.g. opening a favorite).
        if clear_entity and old and old != corpus_id:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    UPDATE chats
                    SET entity_id = '', entity_name = '', entity_kind = 'group',
                        settings_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ?
                    """,
                    (json.dumps(chat.settings, ensure_ascii=False), chat_id),
                )
            chat.entity_id = ""
            chat.entity_name = ""
            chat.entity_kind = "group"
            return chat
        return self._save_settings(chat)

    def apply_favorite(
        self, chat_id: int, fav: dict[str, str]
    ) -> Chat:
        """Switch corpus (keeping favorites) and select favorite entity."""
        corpus = str(fav.get("corpus") or "1")
        self.set_corpus(chat_id, corpus, clear_entity=False)
        self.set_entity(
            chat_id,
            str(fav["id"]),
            str(fav.get("name") or ""),
            str(fav.get("kind") or "group"),
        )
        chat = self.get_chat(chat_id)
        if not chat:
            raise KeyError(chat_id)
        return chat

    def add_favorite(
        self, chat_id: int, entity_id: str, name: str, kind: str = "group",
        corpus: str = "",
    ) -> Chat:
        chat = self.get_chat(chat_id)
        if not chat:
            raise KeyError(chat_id)
        corpus = corpus or chat.corpus or "1"
        favs = chat.favorites()
        if any(f["id"] == entity_id and f.get("corpus") == corpus for f in favs):
            return chat
        same = [f for f in favs if f.get("corpus") == corpus]
        other = [f for f in favs if f.get("corpus") != corpus]
        if len(same) >= MAX_FAVORITES:
            same = same[-(MAX_FAVORITES - 1) :]
        same.append({"id": entity_id, "name": name, "kind": kind, "corpus": corpus})
        chat.settings["favorites"] = other + same
        return self._save_settings(chat)

    def remove_favorite(self, chat_id: int, entity_id: str, corpus: str = "") -> Chat:
        chat = self.get_chat(chat_id)
        if not chat:
            raise KeyError(chat_id)
        corpus = corpus or chat.corpus or "1"
        chat.settings["favorites"] = [
            f
            for f in chat.favorites()
            if not (f["id"] == entity_id and f.get("corpus") == corpus)
        ]
        return self._save_settings(chat)

    def toggle_favorite(
        self, chat_id: int, entity_id: str, name: str, kind: str = "group",
        corpus: str = "",
    ) -> tuple[Chat, bool]:
        chat = self.get_chat(chat_id)
        if not chat:
            raise KeyError(chat_id)
        corpus = corpus or chat.corpus or "1"
        if any(f["id"] == entity_id and f.get("corpus") == corpus for f in chat.favorites()):
            return self.remove_favorite(chat_id, entity_id, corpus), False
        return self.add_favorite(chat_id, entity_id, name, kind, corpus), True

    def toggle_setting(self, chat_id: int, key: str) -> Chat:
        chat = self.get_chat(chat_id)
        if not chat:
            raise KeyError(chat_id)
        chat.settings[key] = not bool(chat.settings.get(key, DEFAULT_SETTINGS.get(key)))
        return self._save_settings(chat)

    def set_setting(self, chat_id: int, key: str, value: bool) -> Chat:
        chat = self.get_chat(chat_id)
        if not chat:
            raise KeyError(chat_id)
        chat.settings[key] = value
        return self._save_settings(chat)

    def subscribers(
        self, entity_id: str | None = None, corpus: str | None = None
    ) -> list[Chat]:
        sql = "SELECT * FROM chats WHERE json_extract(settings_json, '$.notify') = 1 AND entity_id != ''"
        args: list = []
        if entity_id:
            sql += " AND entity_id = ?"
            args.append(entity_id)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        chats = [self._chat_from_row(r) for r in rows]
        if corpus is not None:
            chats = [c for c in chats if c.corpus == corpus]
        return chats

    def morning_subscribers(self) -> list[Chat]:
        sql = """
            SELECT * FROM chats
            WHERE json_extract(settings_json, '$.notify_morning') = 1
              AND entity_id != ''
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [self._chat_from_row(r) for r in rows]

    def all_chats(self) -> list[Chat]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chats ORDER BY updated_at DESC"
            ).fetchall()
        return [self._chat_from_row(r) for r in rows]

    def get_snapshot(self, entity_id: str) -> tuple[str, str] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT fingerprint, payload_json FROM snapshots WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
        if not row:
            return None
        return row["fingerprint"], row["payload_json"]

    def save_snapshot(
        self, entity_id: str, fingerprint: str, payload: Any, updated_label: str = ""
    ) -> None:
        blob = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO snapshots (entity_id, fingerprint, payload_json, updated_label)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    payload_json = excluded.payload_json,
                    updated_label = excluded.updated_label,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (entity_id, fingerprint, blob, updated_label),
            )

    def get_meta(self, key: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def save_archived_day(
        self,
        corpus_id: str,
        entity_id: str,
        day_date: str,
        name: str,
        kind: str,
        payload: Any,
    ) -> None:
        blob = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO day_archive
                    (corpus_id, entity_id, day_date, name, kind, payload_json, saved_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(corpus_id, entity_id, day_date) DO UPDATE SET
                    name = excluded.name,
                    kind = excluded.kind,
                    payload_json = excluded.payload_json,
                    saved_at = CURRENT_TIMESTAMP
                """,
                (corpus_id, entity_id, day_date, name, kind, blob),
            )

    def archive_service_days(self, service: Any) -> int:
        """Persist current site day pages for later (yesterday after rollover)."""
        page = getattr(service, "page_date", None)
        today_days = getattr(service, "today_days", None) or {}
        if not page or not today_days:
            return 0
        n = 0
        for eid, day in today_days.items():
            payload = {
                "corpus": service.corpus_id,
                "entity_id": eid,
                "name": day.name,
                "kind": day.kind,
                "date": day.day.isoformat(),
                "weekday": day.weekday,
                "week_no": day.week_no,
                "lessons": [
                    {
                        "pair": ls.pair,
                        "subgroup": ls.subgroup,
                        "subject": ls.subject,
                        "room": ls.room,
                        "teacher": ls.teacher,
                        "group": ls.group,
                    }
                    for ls in day.lessons
                ],
            }
            self.save_archived_day(
                service.corpus_id,
                eid,
                day.day.isoformat(),
                day.name,
                day.kind,
                payload,
            )
            n += 1
        return n

    def get_archived_day(
        self, corpus_id: str, entity_id: str, day
    ) -> Any | None:
        from datetime import date as date_cls

        from parser import DaySchedule, Lesson

        day_s = day.isoformat() if hasattr(day, "isoformat") else str(day)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM day_archive
                WHERE corpus_id = ? AND entity_id = ? AND day_date = ?
                """,
                (corpus_id, entity_id, day_s),
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            return None
        lessons = [
            Lesson(
                pair=int(x.get("pair") or 0),
                subgroup=int(x.get("subgroup") or 1),
                subject=str(x.get("subject") or ""),
                room=str(x.get("room") or ""),
                teacher=str(x.get("teacher") or ""),
                group=str(x.get("group") or ""),
            )
            for x in (data.get("lessons") or [])
        ]
        d = date_cls.fromisoformat(day_s)
        return DaySchedule(
            entity_id=entity_id,
            name=str(data.get("name") or row["name"] or ""),
            kind=str(data.get("kind") or row["kind"] or "group"),
            day=d,
            weekday=str(data.get("weekday") or ""),
            week_no=data.get("week_no"),
            lessons=lessons,
        )

    def has_archived_day(self, corpus_id: str, entity_id: str, day) -> bool:
        day_s = day.isoformat() if hasattr(day, "isoformat") else str(day)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM day_archive
                WHERE corpus_id = ? AND entity_id = ? AND day_date = ?
                """,
                (corpus_id, entity_id, day_s),
            ).fetchone()
        return row is not None

    def purge_archived_days(
        self, keep_days: int = 2, today: Any = None
    ) -> int:
        """Drop archived schedule older than keep_days (by schedule date)."""
        from datetime import date as date_cls
        from datetime import datetime, timedelta

        if today is None:
            today = datetime.now(config.TZ).date()
        elif not isinstance(today, date_cls):
            today = date_cls.fromisoformat(str(today))
        cutoff = (today - timedelta(days=int(keep_days))).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM day_archive WHERE day_date < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    def archive_count(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM day_archive").fetchone()
        return int(row["n"] if row else 0)

    @staticmethod
    def _chat_from_row(row: sqlite3.Row) -> Chat:
        settings = dict(DEFAULT_SETTINGS)
        try:
            settings.update(json.loads(row["settings_json"] or "{}"))
        except json.JSONDecodeError:
            pass
        updated = ""
        try:
            updated = row["updated_at"] or ""
        except (IndexError, KeyError):
            updated = ""
        return Chat(
            chat_id=row["chat_id"],
            chat_type=row["chat_type"],
            entity_id=row["entity_id"] or "",
            entity_name=row["entity_name"] or "",
            entity_kind=row["entity_kind"] or "group",
            settings=settings,
            title=row["title"] or "",
            updated_at=updated,
        )
