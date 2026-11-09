"""Shared market collection clocks used by the managed report triggers."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytz


def market_schedule(config: dict) -> tuple[object, list[tuple[time, bool, int]]]:
    """Use report clocks, independently of each report's delivery switch."""
    scheduler = config.get("scheduler", {}) or {}
    zone = pytz.timezone(scheduler.get("timezone", "Asia/Shanghai"))

    def grace(value, default):
        try:
            result = int(value)
            return result if result > 0 else default
        except (TypeError, ValueError):
            return default

    specs = [
        (
            scheduler.get("run_time", "19:00"),
            False,
            grace(scheduler.get("daily_misfire_grace_seconds"), 3600),
        )
    ]
    specs.extend(
        (
            brief.get("run_time", "09:50"),
            brief.get("skip_weekends", True),
            grace(
                brief.get(
                    "misfire_grace_seconds",
                    scheduler.get("brief_misfire_grace_seconds"),
                ),
                900,
            ),
        )
        for brief in scheduler.get("brief_reports", [])
    )
    parsed = []
    for clock, weekends, window in specs:
        selected = time.fromisoformat(str(clock))
        if selected.tzinfo or selected.second or selected.microsecond:
            raise ValueError("market collection clock must be a local HH:MM")
        parsed.append((selected, bool(weekends), window))
    return zone, parsed


def market_deadlines(config: dict, now: datetime):
    """Return latest catch-up slot and next future slot using current quotes."""
    zone, specs = market_schedule(config)
    local = now.astimezone(zone)
    eligible, future = [], []
    for offset in range(-1, 9):
        day = local.date() + timedelta(days=offset)
        for clock, weekends, grace in specs:
            if weekends and day.weekday() >= 5:
                continue
            try:
                scheduled = zone.localize(datetime.combine(day, clock), is_dst=None)
            except pytz.exceptions.NonExistentTimeError:
                continue
            except pytz.exceptions.AmbiguousTimeError:
                # The stable wall-clock slot runs only once on a repeated hour.
                scheduled = zone.localize(datetime.combine(day, clock), is_dst=False)
            if scheduled <= now:
                if (now - scheduled).total_seconds() <= grace:
                    eligible.append(scheduled)
            else:
                future.append(scheduled)
    return max(eligible, default=None), min(future, default=None)
