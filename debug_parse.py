"""Fetch live pages and print a parse summary. Run from SptBot folder."""
from __future__ import annotations

import asyncio
import sys

from schedule import ScheduleService


async def main() -> None:
    svc = ScheduleService()
    try:
        changed = await svc.refresh_today()
        print("updated:", svc.updated_label)
        print("page date:", svc.page_date, svc.weekday, "week", svc.week_no)
        for kind, items in svc.by_kind.items():
            print(f"{kind}: {len(items)}")
            print("  ", ", ".join(e.name for e in items[:12]), "...")
        print("today days:", len(svc.today_days))
        sample = next(iter(svc.by_kind["group"]), None)
        if sample:
            day = svc.today_for(sample.id)
            print("sample group", sample.name, sample.id, sample.week_file)
            if day:
                for ls in day.lessons:
                    print(
                        f"  pair {ls.pair}.{ls.subgroup} {ls.subject} | {ls.room} | {ls.teacher}"
                    )
            week = await svc.week_for(sample)
            print("week days", len(week))
            for d in week:
                print(
                    f"  {d.day} {d.weekday} lessons={len(d.lessons)}"
                )
        print("changed on first load", len(changed))
    finally:
        await svc.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
