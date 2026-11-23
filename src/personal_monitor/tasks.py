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

    specs = (
        [
            (
                scheduler.get("run_time", "19:00"),
                False,
                grace(scheduler.get("daily_misfire_grace_seconds"), 3600),
            )
        ]
        if scheduler.get("collection_enabled", True)
        else []
    )
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
        if brief.get("collection_enabled", True)
    )
    parsed = []
    for clock, weekends, window in specs:
        selected = time.fromisoformat(str(clock))
        if selected.tzinfo or selected.second or selected.microsecond:
            raise ValueError("market collection clock must be a local HH:MM")
        parsed.append((selected, bool(weekends), window))
    return zone, parsed


def _deadlines(zone, specs, now):
    local = now.astimezone(zone)
    eligible, future = [], {}
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
                future.setdefault((clock, weekends, grace), scheduled)
    return max(eligible, default=None), future


def market_deadlines(config: dict, now: datetime):
    """Return latest catch-up slot and next future slot using current quotes."""
    zone, specs = market_schedule(config)
    eligible, future = _deadlines(zone, specs, now)
    return eligible, min(future.values(), default=None)


def collection_status(config: dict, now: datetime) -> dict:
    """Expose the same calendar used by the timer, including each next slot."""
    zone, specs = market_schedule(config)
    mode = (config.get("scheduler") or {}).get("data_collection_mode", "interval")
    active = sorted(set(specs)) if mode == "three_daily" else []
    _, future = _deadlines(zone, active, now)
    next_slot = min(future.values(), default=None)
    return {
        "collection_mode": mode,
        "timezone": str(zone),
        "slots": [
            {
                "time": t.strftime("%H:%M"),
                "skip_weekends": weekends,
                "misfire_grace_seconds": grace,
            }
            for t, weekends, grace in active
        ],
        "next_by_slot": [future[slot].isoformat() for slot in active],
        "next_collection_at": next_slot.isoformat() if next_slot else None,
    }
