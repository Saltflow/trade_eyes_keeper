"""Report-independent source collection slots in the configured timezone."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from zoneinfo import ZoneInfo


def collection_schedule(config: dict) -> tuple[list[time], ZoneInfo] | None:
    scheduler = config.get("scheduler", {})
    values = scheduler.get("data_collection_times")
    if values is None:
        return None  # Compatibility for profiles without a collection schedule.
    if not isinstance(values, list) or not 1 <= len(values) <= 24:
        raise ValueError("data_collection_times must contain 1 to 24 HH:MM slots")
    slots = []
    for value in values:
        if (
            not isinstance(value, str)
            or len(value) != 5
            or value[2] != ":"
            or not (value[:2] + value[3:]).isdigit()
        ):
            raise ValueError("data_collection_times requires HH:MM")
        slots.append(time(int(value[:2]), int(value[3:])))
    if len(set(slots)) != len(slots):
        raise ValueError("duplicate data collection slot")
    return sorted(slots), ZoneInfo(scheduler.get("timezone", "Asia/Shanghai"))


def due_collection_slot(config: dict, now: datetime) -> datetime | None:
    schedule = collection_schedule(config)
    if schedule is None:
        return None
    slots, zone = schedule
    local = now.astimezone(zone)
    due = [datetime.combine(local.date(), slot, zone) for slot in slots]
    # A restart outside a slot must not turn into an additional network poll.
    return next(
        (slot for slot in reversed(due) if 0 <= (local - slot).total_seconds() < 600),
        None,
    )


def next_collection_slot(config: dict, now: datetime) -> datetime | None:
    schedule = collection_schedule(config)
    if schedule is None:
        return None
    slots, zone = schedule
    local = now.astimezone(zone)
    for days in (0, 1):
        for slot in slots:
            candidate = datetime.combine(
                local.date() + timedelta(days=days), slot, zone
            )
            if candidate > local:
                return candidate
    raise ValueError("collection schedule has no future slot")
