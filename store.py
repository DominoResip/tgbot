from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config


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
        # favorites for current corpus only in UI
        return out[:MAX_FAVORITES]

    def favorites_for_corpus(self, corpus_id: str) -> list[dict[str, str]]:
        return [f for f in self.favorites() if f.get("corpus") == corpus_id][:MAX_FAVORITES]


class Store:
    def __init__(self, path: str = config.DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
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
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

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
            return existing
        chat = Chat(chat_id=chat_id, chat_type=chat_type, title=title)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chats (chat_id, chat_type, entity_id, entity_name,
                                   entity_kind, settings_json, title)
                VALUES (?, ?, '', '', 'group', ?, ?)
                """,
                (chat_id, chat_type, json.dumps(chat.settings, ensure_ascii=False), title),
            )
        return chat

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

    def set_corpus(self, chat_id: int, corpus_id: str) -> Chat:
        chat = self.get_chat(chat_id)
        if not chat:
            raise KeyError(chat_id)
        old = chat.corpus
        chat.settings["corpus"] = corpus_id
        # Switching corpus clears current entity (different lists).
        if old and old != corpus_id:
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
