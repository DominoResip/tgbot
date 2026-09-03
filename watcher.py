from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError

from formatters import format_day, format_diff
from parser import DaySchedule
from schedule import ScheduleHub, ScheduleService
from store import Store
import weather

log = logging.getLogger("spt.watcher")


async def bootstrap(hub: ScheduleHub, store: Store) -> bool:
    import asyncio

    import config

    last_exc: Exception | None = None
    for attempt in range(1, config.BOOTSTRAP_RETRIES + 1):
        try:
            await hub.refresh_all()
            for svc in hub.services.values():
                _save_all_snapshots(svc, store)
                if svc.updated_label:
                    store.set_meta(f"updated:{svc.corpus_id}", svc.updated_label)
            store.set_meta("bootstrapped", "1")
            total = sum(len(s.entities) for s in hub.services.values())
            log.info(
                "loaded %s entities across corpora (attempt %s)",
                total,
                attempt,
            )
            return True
        except Exception as exc:
            last_exc = exc
            log.exception(
                "initial schedule fetch failed (%s/%s)",
                attempt,
                config.BOOTSTRAP_RETRIES,
            )
            if attempt < config.BOOTSTRAP_RETRIES:
                await asyncio.sleep(min(3 * attempt, 15))
    if last_exc:
        log.error("bootstrap gave up: %s", last_exc)
    return False


def _save_all_snapshots(service: ScheduleService, store: Store) -> None:
    for eid, day in service.today_days.items():
        store.save_snapshot(
            service.snapshot_key(eid),
            day.fingerprint(),
            service.snapshot_payload(eid),
            service.updated_label,
        )


async def poll_changes(hub: ScheduleHub, store: Store, bot: Bot) -> None:
    bootstrapped = store.get_meta("bootstrapped") == "1"
    try:
        changes = await hub.refresh_all()
    except Exception:
        log.exception("schedule poll failed")
        return

    if not bootstrapped:
        for svc in hub.services.values():
            _save_all_snapshots(svc, store)
            if svc.updated_label:
                store.set_meta(f"updated:{svc.corpus_id}", svc.updated_label)
        store.set_meta("bootstrapped", "1")
        return

    for corpus_id, svc in hub.services.items():
        previous_label = store.get_meta(f"updated:{corpus_id}") or ""
        label_changed = bool(
            svc.updated_label and svc.updated_label != previous_label
        )
        changed_ids = _today_diffs(svc, store)
        week_changed = await _week_diffs(svc, store)
        notify_ids = set(changed_ids) | set(week_changed)

        if svc.updated_label and (label_changed or notify_ids):
            store.set_meta(f"updated:{corpus_id}", svc.updated_label)

        if not notify_ids:
            _save_all_snapshots(svc, store)
            continue

        log.info("corpus %s changed for %s entities", corpus_id, len(notify_ids))
        for eid in notify_ids:
            await _notify_entity(svc, store, bot, eid)
            store.save_snapshot(
                svc.snapshot_key(eid),
                svc.fingerprint_for(eid),
                svc.snapshot_payload(eid),
                svc.updated_label,
            )


def _today_diffs(service: ScheduleService, store: Store) -> list[str]:
    changed: list[str] = []
    for eid, day in service.today_days.items():
        key = service.snapshot_key(eid)
        prev = store.get_snapshot(key)
        fp = day.fingerprint()
        if not prev:
            store.save_snapshot(
                key, fp, service.snapshot_payload(eid), service.updated_label
            )
            continue
        old_fp, old_json = prev
        try:
            old = json.loads(old_json)
        except json.JSONDecodeError:
            old = {}
        old_date = old.get("date")
        if old_date and old_date != day.day.isoformat():
            store.save_snapshot(
                key, fp, service.snapshot_payload(eid), service.updated_label
            )
            continue
        if old_fp != fp:
            changed.append(eid)
    return changed


async def _week_diffs(service: ScheduleService, store: Store) -> list[str]:
    changed: list[str] = []
    subscribed = {
        c.entity_id
        for c in store.subscribers(corpus=service.corpus_id)
        if c.entity_id
    }
    for eid in subscribed:
        ent = service.get_entity(eid)
        if not ent:
            continue
        try:
            days = await service.week_for(ent)
        except Exception:
            log.exception("week fetch failed for %s/%s", service.corpus_id, eid)
            continue
        fp = "\n---\n".join(d.fingerprint() for d in days)
        key = f"week:{service.corpus_id}:{eid}"
        old = store.get_meta(key)
        if old is None:
            store.set_meta(key, fp)
            continue
        if old != fp:
            changed.append(eid)
            store.set_meta(key, fp)
    return changed


async def _notify_entity(
    service: ScheduleService, store: Store, bot: Bot, entity_id: str
) -> None:
    day = service.today_for(entity_id)
    if day is None:
        ent = service.get_entity(entity_id)
        if not ent:
            return
        day = DaySchedule(
            entity_id=entity_id,
            name=ent.name,
            kind=ent.kind,
            day=service.page_date or date.today(),
        )
    prev = store.get_snapshot(service.snapshot_key(entity_id))
    old_lessons = []
    if prev:
        try:
            old_lessons = json.loads(prev[1]).get("lessons") or []
        except json.JSONDecodeError:
            old_lessons = []

    for chat in store.subscribers(entity_id, corpus=service.corpus_id):
        text = format_diff(old_lessons, day, chat) or format_day(
            day, chat, updated_label=service.updated_label or "обновлено"
        )
        text = "🔔 Расписание изменилось на сайте\n\n" + text
        try:
            await bot.send_message(
                chat_id=chat.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Forbidden:
            log.info("bot blocked in chat %s", chat.chat_id)
        except TelegramError:
            log.exception("notify failed for chat %s", chat.chat_id)


async def send_morning(hub: ScheduleHub, store: Store, bot: Bot) -> None:
    weather_line = await weather.kemerovo_weather_line()
    for chat in store.morning_subscribers():
        if not chat.corpus or not chat.entity_id:
            continue
        svc = hub.get(chat.corpus)
        if svc.page_date is None:
            try:
                await svc.refresh_today()
            except Exception:
                log.exception("morning refresh failed for %s", chat.corpus)
                continue
        today = svc.page_date
        if today is None:
            continue
        ent = svc.get_entity(chat.entity_id)
        if not ent:
            continue
        day = svc.today_for(ent.id)
        if day is None:
            try:
                day = await svc.day_for(ent, today)
            except Exception:
                log.exception("morning day fetch failed")
                continue
        schedule = format_day(day, chat, updated_label=svc.updated_label)
        text = weather.format_morning(
            "🌅 Доброе утро, студенты!",
            weather_line,
            schedule,
        )
        try:
            await bot.send_message(
                chat_id=chat.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Forbidden:
            log.info("bot blocked in chat %s", chat.chat_id)
        except TelegramError:
            log.exception("morning send failed for %s", chat.chat_id)


def tomorrow_date(service: ScheduleService) -> date:
    base = service.page_date or date.today()
    return base + timedelta(days=1)
