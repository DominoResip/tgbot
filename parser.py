from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from bs4 import BeautifulSoup, Tag

HREF_RE = re.compile(
    r"(?:c|v)(?P<kind>[gpa])(?P<num>\d+)\.htm",
    re.IGNORECASE,
)
SUBJECT_RE = re.compile(r"^j\d+\.htm", re.IGNORECASE)
DATE_HEAD_RE = re.compile(
    r"(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>\d{4})\s*(?P<wd>[А-Яа-я]{2})",
)
DATE_CELL_RE = re.compile(
    r"(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>\d{4})\s*(?P<wd>[А-Яа-я]{2})(?:-(?P<wn>\d))?",
)
UPDATED_RE = re.compile(
    r"Обновлено:\s*(?P<d>\d{2}\.\d{2}\.\d{4})\s*в\s*(?P<t>\d{1,2}:\d{2})",
)
WEEK_RE = re.compile(r"Неделя\s+(\d+)", re.IGNORECASE)
PAIR_RE = re.compile(r"^\d{1,2}$")
NAV_MARKERS = ("m22l.gif", "m11l.gif", "m22r.gif", "psbatishev")

KIND_MAP = {"g": "group", "p": "teacher", "a": "room"}
KIND_RU = {"group": "группа", "teacher": "преподаватель", "room": "аудитория"}


def canonical_id(kind: str, num: str | int) -> str:
    prefix = {"group": "g", "teacher": "t", "room": "r"}[kind]
    return f"{prefix}{int(num)}"


def parse_href(href: str) -> tuple[str, str] | None:
    if not href:
        return None
    name = href.split("/")[-1].split("?")[0]
    m = HREF_RE.search(name)
    if not m:
        return None
    kind = KIND_MAP.get(m.group("kind").lower())
    if not kind:
        return None
    return kind, canonical_id(kind, m.group("num"))


def week_filename(entity_id: str) -> str:
    prefix, num = entity_id[0], entity_id[1:]
    letter = {"g": "g", "t": "p", "r": "a"}[prefix]
    return f"c{letter}{num}.htm"


def day_filename(kind: str) -> str:
    return {"group": "hg.htm", "teacher": "hp.htm", "room": "ha.htm"}[kind]


def _parse_date(d: str, m: str, y: str) -> date:
    return date(int(y), int(m), int(d))


@dataclass
class Lesson:
    pair: int
    subgroup: int
    subject: str = ""
    room: str = ""
    teacher: str = ""
    group: str = ""

    def is_empty(self) -> bool:
        return not (self.subject or self.room or self.teacher or self.group)

    def fingerprint(self) -> str:
        return "|".join(
            [
                str(self.pair),
                str(self.subgroup),
                self.subject,
                self.room,
                self.teacher,
                self.group,
            ]
        )


@dataclass
class DaySchedule:
    entity_id: str
    name: str
    kind: str
    day: date
    weekday: str = ""
    week_no: int | None = None
    lessons: list[Lesson] = field(default_factory=list)

    def fingerprint(self) -> str:
        parts = [self.entity_id, self.day.isoformat()]
        parts.extend(ls.fingerprint() for ls in self.lessons if not ls.is_empty())
        return "\n".join(parts)


@dataclass
class Entity:
    id: str
    name: str
    kind: str
    week_file: str


@dataclass
class ParseResult:
    kind: str
    title: str
    page_date: date | None
    weekday: str
    week_no: int | None
    updated: str
    entities: list[Entity]
    days: list[DaySchedule]
    raw_hash: str = ""


def decode_html(content: bytes) -> str:
    for enc in ("windows-1251", "utf-8", "cp1251"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("windows-1251", errors="replace")


def parse_schedule_html(
    html: str,
    expected_kind: str | None = None,
    entity_id: str | None = None,
    entity_name: str | None = None,
) -> ParseResult:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)

    kind = expected_kind or _kind_from_title(title)
    page_date, weekday = _extract_page_date(soup)
    week_no = _extract_week_no(soup)
    updated = ""
    um = UPDATED_RE.search(soup.get_text(" ", strip=True))
    if um:
        updated = f"{um.group('d')} {um.group('t')}"

    table = _find_schedule_table(soup)
    if table is None:
        return ParseResult(
            kind=kind or "group",
            title=title,
            page_date=page_date,
            weekday=weekday,
            week_no=week_no,
            updated=updated,
            entities=[],
            days=[],
        )

    entity_from_title = _entity_from_title(title, kind)
    if entity_id and entity_from_title:
        forced = Entity(
            id=entity_id,
            name=entity_name or entity_from_title[1],
            kind=entity_from_title[0] or (kind or "group"),
            week_file=week_filename(entity_id),
        )
    elif entity_id:
        forced = Entity(
            id=entity_id,
            name=entity_name or title,
            kind=kind or "group",
            week_file=week_filename(entity_id),
        )
    else:
        forced = None
    days, entities = _parse_table(
        table,
        kind=kind or "group",
        page_date=page_date,
        weekday=weekday,
        week_no=week_no,
        title_entity=entity_from_title,
        forced_entity=forced,
    )
    return ParseResult(
        kind=kind or "group",
        title=title,
        page_date=page_date,
        weekday=weekday,
        week_no=week_no,
        updated=updated,
        entities=_unique_entities(entities),
        days=days,
    )


def _kind_from_title(title: str) -> str | None:
    t = title.lower()
    if "преподавател" in t:
        return "teacher"
    if "аудитор" in t:
        return "room"
    if "групп" in t:
        return "group"
    return None


def _entity_from_title(title: str, kind: str | None) -> tuple[str, str] | None:
    for prefix, k in (("Группа:", "group"), ("Преподаватель:", "teacher"), ("Аудитория:", "room")):
        if title.startswith(prefix):
            return k, title.split(":", 1)[1].strip()
    return (kind, title) if kind and ":" in title else None


def _extract_page_date(soup: BeautifulSoup) -> tuple[date | None, str]:
    text = soup.get_text("\n", strip=True)
    m = DATE_HEAD_RE.search(text)
    if not m:
        return None, ""
    return _parse_date(m.group("d"), m.group("m"), m.group("y")), m.group("wd")


def _extract_week_no(soup: BeautifulSoup) -> int | None:
    m = WEEK_RE.search(soup.get_text(" ", strip=True))
    return int(m.group(1)) if m else None


def _find_schedule_table(soup: BeautifulSoup) -> Tag | None:
    best: Tag | None = None
    best_rows = 0
    for table in soup.find_all("table"):
        sample = str(table)[:1200].lower()
        if any(marker in sample for marker in NAV_MARKERS):
            continue
        rows = table.find_all("tr")
        if len(rows) > best_rows:
            best = table
            best_rows = len(rows)
    return best


def _parse_table(
    table: Tag,
    kind: str,
    page_date: date | None,
    weekday: str,
    week_no: int | None,
    title_entity: tuple[str, str] | None,
    forced_entity: Entity | None = None,
) -> tuple[list[DaySchedule], list[Entity]]:
    current_entity: Entity | None = forced_entity
    current_date = page_date
    current_wd = weekday
    current_wn = week_no
    buckets: dict[tuple[str, date], DaySchedule] = {}
    entities: list[Entity] = []
    if forced_entity:
        entities.append(forced_entity)

    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        if _is_header_row(tds):
            continue

        entity = _entity_from_cells(tds)
        if entity:
            current_entity = entity
            entities.append(entity)

        date_info = _date_from_cells(tds)
        if date_info:
            current_date, current_wd, wn = date_info
            if wn is not None:
                current_wn = wn

        pair, lesson_cells = _split_pair_and_lessons(tds)
        if pair is None:
            continue

        eid, name, ekind = _resolve_entity(current_entity, title_entity, kind)
        if not eid or current_date is None:
            continue

        key = (eid, current_date)
        day = buckets.get(key)
        for idx, cell in enumerate(lesson_cells, start=1):
            lesson = _parse_lesson_cell(cell, pair, idx)
            if not lesson or lesson.is_empty():
                continue
            if day is None:
                day = DaySchedule(
                    entity_id=eid,
                    name=name,
                    kind=ekind,
                    day=current_date,
                    weekday=current_wd,
                    week_no=current_wn,
                )
                buckets[key] = day
            day.lessons.append(lesson)

    days = list(buckets.values())
    for day in days:
        day.lessons.sort(key=lambda x: (x.pair, x.subgroup))
    return days, entities


def _resolve_entity(
    current: Entity | None,
    title_entity: tuple[str, str] | None,
    kind: str,
) -> tuple[str, str, str]:
    if current:
        return current.id, current.name, current.kind
    if title_entity:
        # Week page without repeating entity id in rows — caller should set id via filename
        return "", title_entity[1], title_entity[0] or kind
    return "", "", kind


def _is_header_row(tds: list[Tag]) -> bool:
    words = {"день", "пара", "группа", "преподаватель", "аудитория", "группы"}
    texts = [td.get_text(" ", strip=True).casefold() for td in tds]
    return any(t in words for t in texts)


def _entity_from_cells(tds: list[Tag]) -> Entity | None:
    # Only the first cell can introduce a group/teacher/room.
    # Later cells are lessons and also contain ca/cp links on week pages.
    td = tds[0]
    text = td.get_text(" ", strip=True).replace("\xa0", "")
    if PAIR_RE.match(text) and not td.find("a"):
        return None
    for a in td.find_all("a"):
        parsed = parse_href(a.get("href", ""))
        if not parsed:
            continue
        kind, eid = parsed
        name = a.get_text(" ", strip=True)
        if not name:
            continue
        return Entity(
            id=eid,
            name=name,
            kind=kind,
            week_file=week_filename(eid),
        )
    return None


def _date_from_cells(tds: list[Tag]) -> tuple[date, str, int | None] | None:
    text = tds[0].get_text(" ", strip=True).replace("\xa0", " ")
    m = DATE_CELL_RE.search(text)
    if m:
        wn = int(m.group("wn")) if m.group("wn") else None
        return (
            _parse_date(m.group("d"), m.group("m"), m.group("y")),
            m.group("wd"),
            wn,
        )
    return None


def _split_pair_and_lessons(tds: list[Tag]) -> tuple[int | None, list[Tag]]:
    for i, td in enumerate(tds):
        text = td.get_text(" ", strip=True).replace("\xa0", "")
        if PAIR_RE.match(text) and not td.find("a"):
            pair = int(text)
            if 1 <= pair <= 12:
                return pair, tds[i + 1 :]
    return None, []


def _parse_lesson_cell(td: Tag, pair: int, subgroup: int) -> Lesson | None:
    lesson = Lesson(pair=pair, subgroup=subgroup)
    for a in td.find_all("a"):
        href = a.get("href", "")
        name = a.get_text(" ", strip=True)
        if not name:
            continue
        if SUBJECT_RE.match(href.split("/")[-1]):
            lesson.subject = name
            continue
        parsed = parse_href(href)
        if not parsed:
            continue
        kind, _eid = parsed
        if kind == "group":
            lesson.group = name
        elif kind == "teacher":
            lesson.teacher = name
        elif kind == "room":
            lesson.room = name
    if lesson.is_empty():
        leftover = td.get_text(" ", strip=True)
        if leftover and leftover not in {str(pair), ""}:
            # Unlinked leftover text — ignore navigation crumbs
            pass
        return None
    return lesson


def _unique_entities(items: Iterable[Entity]) -> list[Entity]:
    seen: dict[str, Entity] = {}
    for item in items:
        if item.id not in seen:
            seen[item.id] = item
    return sorted(seen.values(), key=lambda e: _sort_key(e.name))


def _sort_key(name: str) -> tuple:
    return (name.casefold(), name)


def fingerprint_days(days: Iterable[DaySchedule]) -> str:
    lines = [d.fingerprint() for d in sorted(days, key=lambda x: (x.entity_id, x.day))]
    return "\n---\n".join(lines)


def now_local(tz) -> datetime:
    return datetime.now(tz)
