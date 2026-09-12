from __future__ import annotations

from datetime import date

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

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


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Bottom keyboard for private chats only (hidden in groups)."""
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("📅 Расписание"),
                KeyboardButton("📋 Меню"),
            ],
            [KeyboardButton("⚙️ Настройки")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


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
    site_today: date | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []

    # «Вчера» = previous active site day (may skip weekends/holidays).
    if prev_day and prev_day != day:
        if site_today and day == site_today:
            label = f"👈 Вчера {date_label(prev_day)}"
        else:
            label = f"👈 {date_label(prev_day)}"
        nav_row.append(
            InlineKeyboardButton(label, callback_data=f"d:{prev_day.strftime('%Y%m%d')}")
        )
    if next_day and next_day != day:
        if site_today and day != site_today and next_day == site_today:
            label = f"Сегодня {date_label(next_day)} 👉"
        else:
            label = f"{date_label(next_day)} 👉"
        nav_row.append(
            InlineKeyboardButton(label, callback_data=f"d:{next_day.strftime('%Y%m%d')}")
        )
    if nav_row:
        rows.append(nav_row)

    # Quick switch: all favorites (any corpus), skip current
    favs = [
        f
        for f in chat.all_favorites()
        if not (f["id"] == chat.entity_id and f.get("corpus") == (chat.corpus or "1"))
    ]
    if favs:
        row: list[InlineKeyboardButton] = []
        for f in favs[:5]:
            short = config.corpus_meta(f.get("corpus") or "1")["short"]
            row.append(
                InlineKeyboardButton(
                    f"{short} {f['name']}",
                    callback_data=f"f:{f.get('corpus') or '1'}:{f['id']}",
                )
            )
        rows.append(row)

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
            [InlineKeyboardButton(f"👥 Сейчас: {who}", callback_data="m:pick")],
            [InlineKeyboardButton("🔎 Быстрый поиск", callback_data="m:search")],
            [InlineKeyboardButton("⭐ Избранное", callback_data="m:favs")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="m:set")],
            [InlineKeyboardButton("💙 Поддержать хостинг", callback_data="m:donate")],
        ]
    )


def donate_keyboard(*, with_back: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    url = (config.DONATION_URL or "").strip()
    if url:
        rows.append(
            [
                InlineKeyboardButton(
                    config.DONATION_TITLE or "💙 Оплатить в CloudTips",
                    url=url,
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("📷 Показать QR-код", callback_data="m:donate_qr")]
    )
    if with_back:
        rows.append([InlineKeyboardButton("К меню 🔙", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)


def broadcast_donate_keyboard() -> InlineKeyboardMarkup:
    """
    Button under mass-broadcast messages.
    Opens in-bot support menu (m:donate), not the payment URL directly.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💙 Поддержать хостинг",
                    callback_data="m:donate",
                )
            ]
        ]
    )


def favorites_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    favs = chat.all_favorites()
    corp = chat.corpus or "1"
    rows: list[list[InlineKeyboardButton]] = []
    kind_icon = {"group": "👥", "teacher": "🔎", "room": "🚪"}
    for f in favs:
        fcorp = f.get("corpus") or "1"
        short = config.corpus_meta(fcorp)["short"]
        icon = kind_icon.get(f.get("kind") or "group", "⭐")
        rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} {short} {f['name']}",
                    callback_data=f"f:{fcorp}:{f['id']}",
                ),
                InlineKeyboardButton("🗑", callback_data=f"xf:{fcorp}:{f['id']}"),
            ]
        )
    # Groups, teachers and rooms can all be favorited.
    if chat.entity_id:
        in_fav = any(
            f["id"] == chat.entity_id and f.get("corpus") == corp for f in favs
        )
        same_corp_count = sum(1 for f in favs if f.get("corpus") == corp)
        if not in_fav and same_corp_count < MAX_FAVORITES:
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
        elif not in_fav and same_corp_count >= MAX_FAVORITES:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"Лимит избранного ({MAX_FAVORITES})",
                        callback_data="m:favs",
                    )
                ]
            )
    rows.append([InlineKeyboardButton("🔎 Быстрый поиск", callback_data="m:search")])
    rows.append([InlineKeyboardButton("👥 Выбрать", callback_data="m:pick")])
    rows.append([InlineKeyboardButton("К меню 🔙", callback_data="m:home")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Статистика", callback_data="a:stats")],
            [InlineKeyboardButton("📣 Новая рассылка", callback_data="a:broadcast")],
            [InlineKeyboardButton("📋 История рассылок", callback_data="a:bc_list")],
            [InlineKeyboardButton("🔄 Обновить сайт", callback_data="a:refresh")],
            [InlineKeyboardButton("К меню 🔙", callback_data="m:home")],
        ]
    )


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Отправить всем", callback_data="a:bc_send"),
                InlineKeyboardButton("❌ Отмена", callback_data="a:bc_cancel"),
            ]
        ]
    )


def broadcasts_list_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for b in items[:12]:
        bid = b["id"]
        stamp = (b.get("created_at") or "")[:16]
        rows.append(
            [
                InlineKeyboardButton(
                    f"#{bid} · {stamp}",
                    callback_data=f"a:bc_view:{bid}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("📣 Новая", callback_data="a:broadcast")])
    rows.append([InlineKeyboardButton("« Админка", callback_data="a:home")])
    return InlineKeyboardMarkup(rows)


def broadcast_item_keyboard(broadcast_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔁 Разослать снова",
                    callback_data=f"a:bc_resend:{broadcast_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🧹 Удалить у получателей",
                    callback_data=f"a:bc_purge:{broadcast_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 Убрать из истории",
                    callback_data=f"a:bc_del:{broadcast_id}",
                )
            ],
            [InlineKeyboardButton("« История", callback_data="a:bc_list")],
            [InlineKeyboardButton("« Админка", callback_data="a:home")],
        ]
    )


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

    mh = int(chat.settings.get("morning_hour", DEFAULT_SETTINGS["morning_hour"]))
    mm = int(chat.settings.get("morning_minute", DEFAULT_SETTINGS["morning_minute"]))
    morning_lab = f"{'✅' if chat.flag('notify_morning') else '❌'} Утро + погода ({mh:02d}:{mm:02d})"

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(lab("show_teacher", "Преподаватель"), callback_data="t:show_teacher")],
        [InlineKeyboardButton(lab("show_room", "Аудитория"), callback_data="t:show_room")],
        [InlineKeyboardButton(lab("show_bells", "Звонки"), callback_data="t:show_bells")],
        [InlineKeyboardButton(lab("show_empty", "Пустые пары"), callback_data="t:show_empty")],
        [InlineKeyboardButton(lab("notify", "Изменения на сайте"), callback_data="t:notify")],
        [InlineKeyboardButton(morning_lab, callback_data="t:notify_morning")],
    ]
    if chat.flag("notify_morning"):
        rows.append(
            [InlineKeyboardButton("⏰ Время утреннего уведомления", callback_data="t:mtime")]
        )
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


def morning_time_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    """Pick morning digest time (half-hour steps, 6:00–10:00)."""
    cur_h = int(chat.settings.get("morning_hour", 8))
    cur_m = int(chat.settings.get("morning_minute", 0))
    slots = [
        (6, 0), (6, 30), (7, 0), (7, 30), (8, 0),
        (8, 30), (9, 0), (9, 30), (10, 0),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for h, m in slots:
        mark = "• " if (h == cur_h and m == cur_m) else ""
        row.append(
            InlineKeyboardButton(
                f"{mark}{h:02d}:{m:02d}",
                callback_data=f"t:mset:{h}:{m}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("« Настройки", callback_data="m:set")])
    return InlineKeyboardMarkup(rows)
