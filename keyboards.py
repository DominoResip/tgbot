from __future__ import annotations

from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from parser import Entity
from store import Chat, DEFAULT_SETTINGS, MAX_FAVORITES
import config
from formatters import date_label

KIND_BTN = {
    "group": "👥 Группы",
    "teacher": "🔎 Преподаватели",
    "room": "🚪 Аудитории",
}


def remove_reply_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def corpus_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏛 1 корпус", callback_data="c:1"),
                InlineKeyboardButton("🏛 2 корпус", callback_data="c:2"),
            ],
            [InlineKeyboardButton("К меню 🔙", callback_data="m:home")],
        ]
    )


def schedule_nav(
    day: date,
    chat: Chat,
    *,
    prev_day: date | None = None,
    next_day: date | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if prev_day and prev_day != day:
        nav_row.append(
            InlineKeyboardButton(
                f"👈 {date_label(prev_day)}",
                callback_data=f"d:{prev_day.strftime('%Y%m%d')}",
            )
        )
    if next_day and next_day != day:
        nav_row.append(
            InlineKeyboardButton(
                f"{date_label(next_day)} 👉",
                callback_data=f"d:{next_day.strftime('%Y%m%d')}",
            )
        )
    if nav_row:
        rows.append(nav_row)

    # Quick switch favorites (no manage/toggle here)
    corp = chat.corpus or "1"
    favs = [f for f in chat.favorites_for_corpus(corp) if f["id"] != chat.entity_id]
    if favs:
        rows.append(
            [
                InlineKeyboardButton(f["name"], callback_data=f"f:{f['id']}")
                for f in favs[:5]
            ]
        )

    rows.append(
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="m:set"),
            InlineKeyboardButton("К меню 🔙", callback_data="m:home"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def menu_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    if not chat.corpus:
        return corpus_keyboard()
    title = config.corpus_meta(chat.corpus)["title"]
    who = chat.entity_name or "не выбрана"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🏛 Корпус: {title}", callback_data="m:corpus")],
            [InlineKeyboardButton("📅 Расписание", callback_data="d:today")],
            [InlineKeyboardButton(f"👥 Группа: {who}", callback_data="m:pick")],
            [InlineKeyboardButton("⭐ Избранное", callback_data="m:favs")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="m:set")],
        ]
    )


def favorites_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    corp = chat.corpus or "1"
    favs = chat.favorites_for_corpus(corp)
    rows: list[list[InlineKeyboardButton]] = []
    for f in favs:
        rows.append(
            [
                InlineKeyboardButton(f"📅 {f['name']}", callback_data=f"f:{f['id']}"),
                InlineKeyboardButton("🗑", callback_data=f"xf:{f['id']}"),
            ]
        )
    if chat.entity_id and chat.entity_kind == "group":
        in_fav = any(f["id"] == chat.entity_id for f in favs)
        if not in_fav and len(favs) < MAX_FAVORITES:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"⭐ Добавить «{chat.entity_name}»",
                        callback_data="m:favadd",
                    )
                ]
            )
        elif in_fav:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"⭐ Убрать «{chat.entity_name}»",
                        callback_data="m:fav",
                    )
                ]
            )
    rows.append([InlineKeyboardButton("👥 Выбрать группу", callback_data="m:pick")])
    rows.append([InlineKeyboardButton("К меню 🔙", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)


def pick_kind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 Группа учащихся", callback_data="k:group")],
            [InlineKeyboardButton("🔎 Преподаватель", callback_data="k:teacher")],
            [InlineKeyboardButton("🚪 Аудитория", callback_data="k:room")],
            [InlineKeyboardButton("К меню 🔙", callback_data="m:home")],
        ]
    )


def letters_keyboard(kind: str, letters: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for ch in letters:
        row.append(InlineKeyboardButton(ch, callback_data=f"l:{kind[0]}:{ch}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("🔎 Поиск", callback_data=f"q:{kind[0]}"),
            InlineKeyboardButton("« Назад", callback_data="m:pick"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def entities_keyboard(
    kind: str, items: list[Entity], page: int = 0, letter: str = ""
) -> InlineKeyboardMarkup:
    page_size = config.PAGE_SIZE
    start = page * page_size
    chunk = items[start : start + page_size]
    rows: list[list[InlineKeyboardButton]] = []
    for ent in chunk:
        rows.append([InlineKeyboardButton(ent.name, callback_data=f"s:{ent.id}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("‹", callback_data=f"p:{kind[0]}:{letter}:{page - 1}")
        )
    if start + page_size < len(items):
        nav.append(
            InlineKeyboardButton("›", callback_data=f"p:{kind[0]}:{letter}:{page + 1}")
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("« К буквам", callback_data=f"k:{kind}")])
    return InlineKeyboardMarkup(rows)


def settings_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    def lab(key: str, title: str) -> str:
        on = chat.flag(key) if hasattr(chat, "flag") else chat.settings.get(
            key, DEFAULT_SETTINGS[key]
        )
        return f"{'✅' if on else '❌'} {title}"

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(lab("show_teacher", "Преподаватель"), callback_data="t:show_teacher")],
        [InlineKeyboardButton(lab("show_room", "Аудитория"), callback_data="t:show_room")],
        [InlineKeyboardButton(lab("show_bells", "Звонки"), callback_data="t:show_bells")],
        [InlineKeyboardButton(lab("show_empty", "Пустые пары"), callback_data="t:show_empty")],
        [InlineKeyboardButton(lab("notify", "Изменения на сайте"), callback_data="t:notify")],
        [InlineKeyboardButton(lab("notify_morning", "Утро в 8:00 + погода"), callback_data="t:notify_morning")],
    ]
    if chat.is_group_chat:
        rows.append(
            [
                InlineKeyboardButton(
                    lab("allow_members", "Доступ участникам"),
                    callback_data="t:allow_members",
                )
            ]
        )
    rows.append([InlineKeyboardButton("📅 К расписанию", callback_data="d:today")])
    rows.append([InlineKeyboardButton("К меню 🔙", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)
