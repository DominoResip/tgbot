from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

import httpx

import config
from parser import (
    DaySchedule,
    Entity,
    ParseResult,
    decode_html,
    day_filename,
    fingerprint_days,
    parse_schedule_html,
    week_filename,
)

log = logging.getLogger("spt.schedule")


class ScheduleService:
    def __init__(self, corpus_id: str, base_url: str) -> None:
        self.corpus_id = corpus_id
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self.updated_label = ""
        self.page_date: date | None = None
        self.weekday = ""
        self.week_no: int | None = None
        self.entities: dict[str, Entity] = {}
        self.by_kind: dict[str, list[Entity]] = {
            "group": [],
            "teacher": [],
            "room": [],
        }
        self.today_days: dict[str, DaySchedule] = {}
        self._week_cache: dict[str, tuple[float, list[DaySchedule]]] = {}
        self.last_error = ""

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    config.FETCH_TIMEOUT,
                    connect=min(30.0, config.FETCH_TIMEOUT),
                ),
                headers={"User-Agent": config.USER_AGENT},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_html(self, filename: str) -> str:
        url = f"{self.base_url}/{filename}"
        client = await self._http()
        last_exc: Exception | None = None
        for attempt in range(1, config.FETCH_RETRIES + 1):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                return decode_html(resp.content)
            except Exception as exc:
                last_exc = exc
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "[%s] fetch %s failed (%s/%s): %s",
                    self.corpus_id,
                    filename,
                    attempt,
                    config.FETCH_RETRIES,
                    exc,
                )
                if attempt < config.FETCH_RETRIES:
                    await asyncio.sleep(min(2 * attempt, 8))
        assert last_exc is not None
        raise last_exc

    async def refresh_today(self) -> dict[str, str]:
        async with self._lock:
            kinds = ("group", "teacher", "room")
            htmls = await asyncio.gather(
                *(self.fetch_html(day_filename(k)) for k in kinds)
            )
            results: list[ParseResult] = [
                parse_schedule_html(html, expected_kind=kind)
                for kind, html in zip(kinds, htmls)
            ]

            entities: dict[str, Entity] = {}
            today_days: dict[str, DaySchedule] = {}
            by_kind: dict[str, list[Entity]] = {
                "group": [],
                "teacher": [],
                "room": [],
            }
            for parsed in results:
                if parsed.updated:
                    self.updated_label = parsed.updated
                if parsed.page_date:
                    self.page_date = parsed.page_date
                    self.weekday = parsed.weekday
                    self.week_no = parsed.week_no
                for ent in parsed.entities:
                    entities[ent.id] = ent
                    by_kind[ent.kind].append(ent)
                for day in parsed.days:
                    today_days[day.entity_id] = day
                    if day.entity_id not in entities:
                        entities[day.entity_id] = Entity(
                            id=day.entity_id,
                            name=day.name,
                            kind=day.kind,
                            week_file=week_filename(day.entity_id),
                        )

            for kind, lst in by_kind.items():
                uniq = {e.id: e for e in lst}
                by_kind[kind] = sorted(uniq.values(), key=lambda e: e.name.casefold())

            old = {eid: d.fingerprint() for eid, d in self.today_days.items()}
            self.entities = entities
            self.by_kind = by_kind
            self.today_days = today_days
            self.last_error = ""
            self._week_cache.clear()

            changed: dict[str, str] = {}
            for eid, day in today_days.items():
                fp = day.fingerprint()
                if old.get(eid) != fp:
                    changed[eid] = fp
            for eid in old:
                if eid not in today_days:
                    changed[eid] = ""
            return changed

    def get_entity(self, entity_id: str) -> Entity | None:
        return self.entities.get(entity_id)

    def search(self, query: str, kind: str | None = None) -> list[Entity]:
        q = query.strip().casefold()
        if not q:
            return []
        pool: Iterable[Entity]
        if kind:
            pool = self.by_kind.get(kind, [])
        else:
            pool = self.entities.values()
        scored: list[tuple[int, Entity]] = []
        for ent in pool:
            name = ent.name.casefold()
            if name == q:
                scored.append((0, ent))
            elif name.startswith(q):
                scored.append((1, ent))
            elif q in name:
                scored.append((2, ent))
        scored.sort(key=lambda x: (x[0], x[1].name.casefold()))
        return [e for _, e in scored[:20]]

    def letters(self, kind: str) -> list[str]:
        seen: list[str] = []
        for ent in self.by_kind.get(kind, []):
            ch = ent.name[:1].upper() if ent.name else "?"
            if ch not in seen:
                seen.append(ch)
        return seen

    def by_letter(self, kind: str, letter: str) -> list[Entity]:
        letter = letter.upper()
        return [e for e in self.by_kind.get(kind, []) if e.name.upper().startswith(letter)]

    def today_for(self, entity_id: str) -> DaySchedule | None:
        return self.today_days.get(entity_id)

    async def week_for(self, entity: Entity) -> list[DaySchedule]:
        now = datetime.now().timestamp()
        cached = self._week_cache.get(entity.id)
        if cached and now - cached[0] < 120:
            return cached[1]
        html = await self.fetch_html(entity.week_file)
        parsed = parse_schedule_html(
            html,
            expected_kind=entity.kind,
            entity_id=entity.id,
            entity_name=entity.name,
        )
        days = sorted(parsed.days, key=lambda d: d.day)
        for day in days:
            day.entity_id = entity.id
            day.name = entity.name
            day.kind = entity.kind
        self._week_cache[entity.id] = (now, days)
        return days

    async def day_for(
        self, entity: Entity, target: date, store=None
    ) -> DaySchedule | None:
        live: DaySchedule | None = None
        if self.page_date == target:
            live = self.today_for(entity.id)
            if live and live.lessons:
                return live
        days = await self.week_for(entity)
        for day in days:
            if day.day == target:
                if day.lessons:
                    return day
                live = live or day
                break

        if store is not None:
            archived = store.get_archived_day(self.corpus_id, entity.id, target)
            if archived and archived.lessons:
                return archived
            if archived and live is None:
                return archived

        if live is not None:
            return live
        return DaySchedule(
            entity_id=entity.id,
            name=entity.name,
            kind=entity.kind,
            day=target,
        )

    def archive_prev_date(
        self, entity_id: str | None = None, store=None
    ) -> date | None:
        """
        Previous active schedule day the user may open from archive.

        Prefer the latest archived day for this entity before the current
        site page date (handles weekend/holiday gaps: Fri -> Mon).
        Fall back to calendar day before page_date.
        """
        if not self.page_date:
            return None
        if store is not None and entity_id:
            found = store.latest_archived_before(
                self.corpus_id, entity_id, self.page_date
            )
            if found is not None:
                return found
        return self.page_date - timedelta(days=1)

    async def nav_bounds(
        self,
        entity: Entity,
        current: date,
        store=None,
    ) -> tuple[date | None, date | None]:
        """
        Prev/next for schedule UI.
        At most one archived active day before the current site page date.
        """
        page = self.page_date
        min_day = self.archive_prev_date(entity.id, store)

        if page and current == page:
            prev: date | None = min_day
            nxt = await self.neighbor_day(entity, current, 1)
            if nxt == current:
                nxt = None
            return prev, nxt

        if min_day and current == min_day:
            return None, page

        prev = await self.neighbor_day(entity, current, -1)
        nxt = await self.neighbor_day(entity, current, 1)
        if min_day and prev is not None and prev < min_day:
            prev = min_day if current > min_day else None
        if page and nxt and min_day and current < page and nxt > page:
            nxt = page
        if prev == current:
            prev = None
        if nxt == current:
            nxt = None
        return prev, nxt

    def is_archive_day(self, entity_id: str, target: date, store) -> bool:
        """True when user opened the one-day lookback (before current site page)."""
        page = self.page_date
        min_day = self.archive_prev_date(entity_id, store)
        if not page or not min_day:
            return False
        return target == min_day and target < page

    async def navigable_dates(self, entity: Entity) -> list[date]:
        """Dates present on the site week view that have at least one lesson."""
        days = await self.week_for(entity)
        dates = sorted({d.day for d in days if d.lessons})
        # Always allow "today" from site page even if empty, so morning view works.
        if self.page_date and self.page_date not in dates:
            today = self.today_for(entity.id)
            if today is not None:
                dates = sorted(set(dates) | {self.page_date})
        return dates

    async def neighbor_day(
        self, entity: Entity, current: date, direction: int
    ) -> date:
        dates = await self.navigable_dates(entity)
        if not dates:
            return current + timedelta(days=direction)
        if direction > 0:
            for d in dates:
                if d > current:
                    return d
            return dates[-1]
        for d in reversed(dates):
            if d < current:
                return d
        return dates[0]

    def snapshot_key(self, entity_id: str) -> str:
        return f"{self.corpus_id}:{entity_id}"

    def snapshot_payload(self, entity_id: str) -> dict:
        day = self.today_days.get(entity_id)
        if not day:
            return {
                "corpus": self.corpus_id,
                "entity_id": entity_id,
                "lessons": [],
            }
        return {
            "corpus": self.corpus_id,
            "entity_id": entity_id,
            "name": day.name,
            "kind": day.kind,
            "date": day.day.isoformat(),
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

    def fingerprint_for(self, entity_id: str) -> str:
        day = self.today_days.get(entity_id)
        return day.fingerprint() if day else ""

    def content_hash(self) -> str:
        blob = fingerprint_days(self.today_days.values())
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ScheduleHub:
    def __init__(self) -> None:
        self.services: dict[str, ScheduleService] = {
            cid: ScheduleService(cid, meta["base"])
            for cid, meta in config.CORPORA.items()
        }

    def get(self, corpus_id: str | None) -> ScheduleService:
        cid = corpus_id if corpus_id in self.services else "1"
        return self.services[cid]

    async def refresh_all(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for cid, svc in self.services.items():
            try:
                out[cid] = await svc.refresh_today()
            except Exception:
                log.exception("refresh failed for corpus %s", cid)
                out[cid] = {}
        return out

    async def close(self) -> None:
        for svc in self.services.values():
            await svc.close()


def group_lessons_by_pair(lessons) -> dict[int, list]:
    grouped: dict[int, list] = defaultdict(list)
    for ls in lessons:
        grouped[ls.pair].append(ls)
    return dict(grouped)
