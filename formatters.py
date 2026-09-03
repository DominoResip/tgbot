from __future__ import annotations

from datetime import date, datetime, time
from html import escape

import config
from parser import DaySchedule, Lesson
from store import Chat, MAX_FAVORITES

WEEKDAYS_SHORT = {
    0: "ПН",
    1: "ВТ",
    2: "СР",
    3: "ЧТ",
    4: "ПТ",
    5: "СБ",
    6: "ВС",
}
MONTHS = [
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]
KIND_RU = {
    "group": "группа",
    "teacher": "преподаватель",
    "room": "аудитория",
}


def date_label(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}.{d.year} [{WEEKDAYS_SHORT[d.weekday()]}]"


def pretty_date(d: date, weekday: str = "") -> str:
    wd = weekday or WEEKDAYS_SHORT.get(d.weekday(), "")
    return f"{wd}, {d.day} {MONTHS[d.month]}"


def pair_start(pair: int, corpus_id: str = "1") -> time | None:
    raw = config.bells_for(corpus_id).get(pair, "")
    if not raw:
        return None
    start = raw.split("–")[0].split("-")[0].strip()
    hh, mm = start.split(":")
    return time(int(hh), int(mm))


def pair_end(pair: int, corpus_id: str = "1") -> time | None:
    raw = config.bells_for(corpus_id).get(pair, "")
    if "–" not in raw and "-" not in raw:
        return None
    end = raw.split("–")[-1].split("-")[-1].strip()
    hh, mm = end.split(":")
    return time(int(hh), int(mm))


def _fmt_delta(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hh, rem = divmod(seconds, 3600)
    mm, ss = divmod(rem, 60)
    if hh:
        return f"{hh:02d}:{mm:02d}:{ss:02d}"
    return f"{mm:02d}:{ss:02d}"


def pair_countdown(
    pair: int, day: date, now: datetime, corpus_id: str = "1"
) -> str | None:
    """Status line for this pair if it is ongoing or the next upcoming one."""
    if day != now.date():
        return None
    start = pair_start(pair, corpus_id)
    end = pair_end(pair, corpus_id)
    if not start or not end:
        return None
    now_t = now.time()
    if start <= now_t < end:
        left = datetime.combine(day, end) - datetime.combine(day, now_t)
        return f"* До конца пары: {_fmt_delta(left.total_seconds())}"
    return None


def next_pair_countdown(
    pairs: list[int], day: date, now: datetime, corpus_id: str = "1"
) -> tuple[int, str] | None:
    """If no pair is ongoing, attach 'until start' to the next upcoming pair."""
    if day != now.date():
        return None
    now_t = now.time()
    for pair in pairs:
        start = pair_start(pair, corpus_id)
        end = pair_end(pair, corpus_id)
        if not start or not end:
            continue
        if start <= now_t < end:
            return None  # ongoing handled per-pair
        if now_t < start:
            left = datetime.combine(day, start) - datetime.combine(day, now_t)
            return pair, f"* До начала пары: {_fmt_delta(left.total_seconds())}"
    return None


def _lesson_title(ls: Lesson, kind: str) -> str:
    title = ls.subject or "Занятие"
    if kind != "group" and ls.group:
        title = f"{ls.group} · {title}"
    return title


def format_day(
    day: DaySchedule | None,
    chat: Chat,
    *,
    heading: str | None = None,
    note: str = "",
    updated_label: str = "",
    now: datetime | None = None,
    from_archive: bool = False,
) -> str:
    if day is None:
        return "📭 Расписание пока не загружено. Откройте меню чуть позже."

    kind = day.kind or chat.entity_kind
    corpus_id = chat.corpus or "1"
    name = escape(day.name)
    lines: list[str] = []

    corp_title = config.corpus_meta(corpus_id)["title"]
    if heading:
        lines.append(heading)
    else:
        lines.append(
            f"<i>Расписание на <b>{escape(date_label(day.day))}</b> для <b>{name}</b>:</i>"
        )
        lines.append(f"🏛 {escape(corp_title)}")
        if from_archive:
            lines.append("<i>📜 Архив — день уже снят с сайта</i>")

    if now is None:
        now = datetime.now(config.TZ)
    local_now = now.astimezone(config.TZ) if now.tzinfo else now
    wall = datetime(
        local_now.year,
        local_now.month,
        local_now.day,
        local_now.hour,
        local_now.minute,
        local_now.second,
    )

    lessons = day.lessons
    if not lessons:
        lines.append("")
        lines.append("Пар нет ✨")
    else:
        by_pair: dict[int, list[Lesson]] = {}
        for ls in lessons:
            by_pair.setdefault(ls.pair, []).append(ls)
        pairs = list(range(1, 8)) if chat.flag("show_empty") else sorted(by_pair)
        upcoming = next_pair_countdown(pairs, day.day, wall, corpus_id)
        lines.append("")
        for pair in pairs:
            items = by_pair.get(pair, [])
            if not items and not chat.flag("show_empty"):
                continue
            start = pair_start(pair, corpus_id)
            time_s = start.strftime("%H:%M") if start and chat.flag("show_bells") else ""
            prefix = f"[#{pair}" + (f" - {time_s}" if time_s else "") + "]"
            if not items:
                lines.append(f"{prefix} —")
            else:
                for ls in items:
                    title = escape(_lesson_title(ls, kind))
                    if len(items) > 1 or ls.subgroup > 1:
                        title = f"{ls.subgroup}) {title}"
                    tail_bits: list[str] = []
                    if chat.flag("show_room") and kind != "room" and ls.room:
                        tail_bits.append(escape(ls.room))
                    if chat.flag("show_teacher") and kind != "teacher" and ls.teacher:
                        tail_bits.append(escape(ls.teacher))
                    line = f"{prefix} <b>{title}</b>"
                    if tail_bits:
                        line += " - " + " · ".join(tail_bits)
                    lines.append(line)

            ongoing = pair_countdown(pair, day.day, wall, corpus_id)
            if ongoing:
                lines.append(f"<i>{escape(ongoing)}</i>")
            elif upcoming and upcoming[0] == pair:
                lines.append(f"<i>{escape(upcoming[1])}</i>")

    upd = updated_label or note
    if note and updated_label:
        lines.append("")
        lines.append(f"ℹ️ {escape(note)}")
        lines.append(f"ℹ️ Последнее обновление: {escape(updated_label)}")
    elif upd:
        lines.append("")
        lines.append(f"ℹ️ Последнее обновление: {escape(upd)}" if updated_label else f"ℹ️ {escape(upd)}")

    return "\n".join(lines).rstrip()


def split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        add = len(line) + 1
        if size + add > limit and buf:
            parts.append("\n".join(buf))
            buf = [line]
            size = add
        else:
            buf.append(line)
            size += add
    if buf:
        parts.append("\n".join(buf))
    return parts


def format_settings(chat: Chat) -> str:
    def mark(key: str, label: str) -> str:
        on = chat.flag(key)
        return f"{'✅' if on else '❌'} {label}"

    who = chat.entity_name or "не выбрано"
    kind = KIND_RU.get(chat.entity_kind, chat.entity_kind)
    corp = (
        config.corpus_meta(chat.corpus)["title"] if chat.corpus else "не выбран"
    )
    favs = chat.all_favorites()
    fav_line = (
        ", ".join(
            f"{config.corpus_meta(f.get('corpus') or '1')['short']} {f['name']}"
            for f in favs
        )
        if favs
        else "пока пусто"
    )
    lines = [
        "⚙️ <b>Настройки</b>",
        f"🏛 Корпус: <b>{escape(corp)}</b>",
        f"Сейчас: {kind} <b>{escape(who)}</b>",
        f"⭐ Избранное: {escape(fav_line)}",
        "",
        mark("show_teacher", "Преподаватель"),
        mark("show_room", "Аудитория"),
        mark("show_bells", "Время звонков"),
        mark("show_empty", "Пустые пары"),
        "",
        mark("notify", "Уведомлять об изменениях"),
        mark("notify_morning", "Утро в 8:00 + погода"),
    ]
    if chat.is_group_chat:
        lines.append("")
        lines.append(
            mark(
                "allow_members",
                "Участники могут пользоваться ботом",
            )
        )
        lines.append(
            "<i>Если выключено — кнопки и команды только для админов чата.</i>"
        )
    lines.append("")
    lines.append("В группах менять настройки могут только администраторы чата.")
    return "\n".join(lines)


def menu_text(chat: Chat) -> str:
    lines = [
        "📋 <b>Меню</b>",
        "Бот расписания СПТ: пары, звонки, избранные группы и уведомления об изменениях на сайте.",
    ]
    if chat.corpus:
        lines.append(f"🏛 {escape(config.corpus_meta(chat.corpus)['title'])}")
    if chat.entity_name:
        lines.append(f"Сейчас: <b>{escape(chat.entity_name)}</b>")
    lines.append("")
    lines.append("<i>Выберите действие кнопками ниже.</i>")
    return "\n".join(lines)


def format_stats(chats: list[Chat], *, archive_rows: int = 0) -> str:
    privates = [c for c in chats if c.chat_type == "private"]
    groups = [c for c in chats if c.chat_type in {"group", "supergroup"}]
    with_entity = [c for c in chats if c.entity_id]
    notify = [c for c in chats if c.flag("notify") and c.entity_id]
    morning = [c for c in chats if c.flag("notify_morning") and c.entity_id]
    c1 = [c for c in with_entity if c.corpus == "1"]
    c2 = [c for c in with_entity if c.corpus == "2"]
    members_off = [
        c for c in groups if not c.flag("allow_members")
    ]

    lines = [
        "📊 <b>Статистика бота</b>",
        "",
        f"Всего чатов: <b>{len(chats)}</b>",
        f"· личных: <b>{len(privates)}</b>",
        f"· групп: <b>{len(groups)}</b>",
        f"С выбранной группой/преподавателем: <b>{len(with_entity)}</b>",
        f"· 1 корпус: <b>{len(c1)}</b> · 2 корпус: <b>{len(c2)}</b>",
        f"Уведомления об изменениях: <b>{len(notify)}</b>",
        f"Утренние сообщения: <b>{len(morning)}</b>",
        f"Групп с закрытым доступом для участников: <b>{len(members_off)}</b>",
        f"Архив дней в БД: <b>{archive_rows}</b>",
    ]

    if groups:
        lines.append("")
        lines.append("<b>Группы</b>")
        for c in groups[:25]:
            title = escape(c.title or str(c.chat_id))
            who = escape(c.entity_name or "—")
            corp = config.corpus_meta(c.corpus)["short"] if c.corpus else "—"
            access = "🔓" if c.flag("allow_members") else "🔒"
            lines.append(
                f"{access} {title} · {corp} · <b>{who}</b> · <code>{c.chat_id}</code>"
            )
        if len(groups) > 25:
            lines.append(f"… ещё {len(groups) - 25}")

    if privates:
        lines.append("")
        lines.append("<b>Личные чаты</b> (последние 20)")
        for c in privates[:20]:
            title = escape(c.title or str(c.chat_id))
            who = escape(c.entity_name or "—")
            corp = config.corpus_meta(c.corpus)["short"] if c.corpus else "—"
            lines.append(
                f"· {title} · {corp} · <b>{who}</b> · <code>{c.chat_id}</code>"
            )
        if len(privates) > 20:
            lines.append(f"… ещё {len(privates) - 20}")

    return "\n".join(lines)


def format_favorites(chat: Chat) -> str:
    favs = chat.all_favorites()
    lines = ["⭐ <b>Избранное</b> (оба корпуса)", ""]
    if not favs:
        lines.append("Пока пусто. Выберите группу и добавьте её сюда.")
    else:
        for i, f in enumerate(favs, 1):
            short = config.corpus_meta(f.get("corpus") or "1")["short"]
            same = (
                f["id"] == chat.entity_id
                and f.get("corpus") == (chat.corpus or "1")
            )
            mark = " ← сейчас" if same else ""
            lines.append(
                f"{i}. {escape(short)} <b>{escape(f['name'])}</b>{mark}"
            )
    lines.append("")
    lines.append(
        f"До {MAX_FAVORITES} групп на каждый корпус. Смена корпуса список не сбрасывает."
    )
    return "\n".join(lines)


def format_diff(old_lessons: list[dict], new_day: DaySchedule, chat: Chat) -> str:
    old_fp = {
        f"{x.get('pair')}|{x.get('subgroup')}|{x.get('subject')}|{x.get('room')}|{x.get('teacher')}|{x.get('group')}"
        for x in old_lessons
    }
    new_fp = {ls.fingerprint() for ls in new_day.lessons}
    if old_fp == new_fp:
        return ""
    header = (
        f"🔔 <b>Расписание изменилось</b>\n"
        f"{escape(new_day.name)} · {date_label(new_day.day)}"
    )
    body = format_day(new_day, chat)
    return header + "\n\n" + body


def help_text() -> str:
    return (
        "📅 <b>СПТ · 1 и 2 корпус</b>\n\n"
        "Все действия — кнопками под сообщениями; в личке также есть клавиатура внизу.\n"
        "Меню: корпус, расписание, смена группы, избранное, настройки.\n"
        "Пустые дни на сайте при листании пропускаются.\n"
        "Кнопка «Вчера» — расписание за предыдущий день (в т.ч. уже снятое с сайта).\n"
        "Утро в 8:00 (Кемерово): приветствие, погода и пары.\n\n"
        "В группах настройки меняют только админы.\n"
        "Админ группы может отключить доступ участникам в настройках."
    )


def welcome_text(is_group: bool) -> str:
    if is_group:
        return (
            "👋 Бот расписания <b>СПТ</b> (1 и 2 корпус).\n\n"
            "Администратор выбирает корпус и группу — дальше чат видит пары "
            "и может получать уведомления об изменениях.\n"
            "Нажмите /menu"
        )
    return (
        "👋 Бот расписания <b>Сибирского политехнического техникума</b>.\n\n"
        "Выберите корпус и группу кнопками в меню.\n"
        "Нажмите /menu"
    )
