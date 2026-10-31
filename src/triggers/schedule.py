"""Calendar calculations only; the existing ExecutionRuntime owns wakeups."""

from __future__ import annotations

from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

from .models import clock, now_utc, timestamp


def normalize_schedule(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {
        "kind",
        "type",
        "at",
        "times",
        "timezone",
        "weekdays",
        "start_date",
        "end_date",
        "window_seconds",
        "catch_up",
    }:
        raise ValueError("invalid schedule")
    zone = value.get("timezone", "Asia/Shanghai")
    ZoneInfo(zone)
    kind = value.get("kind", value.get("type"))
    recurring = kind in {"daily", "weekly"}
    if recurring:
        result = {"kind": kind, "timezone": zone, "at": clock(value.get("at"))}
        days = value.get("weekdays", list(range(7)))
        if (
            not isinstance(days, list)
            or not days
            or any(
                isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 6
                for n in days
            )
        ):
            raise ValueError("weekdays must contain values 0 through 6")
        result["weekdays"] = sorted(set(days))
        for key in ("start_date", "end_date"):
            if key in value:
                result[key] = (
                    timestamp(value[key], zone)
                    .astimezone(ZoneInfo(zone))
                    .date()
                    .isoformat()
                )
        if result.get("start_date", "0001-01-01") > result.get(
            "end_date", "9999-12-31"
        ):
            raise ValueError("schedule date bounds are reversed")
        result["catch_up"] = bool(value.get("catch_up", False))
    else:
        times = value.get("times", [value.get("at")])
        if not isinstance(times, list) or not 1 <= len(times) <= 128:
            raise ValueError("schedule requires bounded ISO times")
        result = {
            "kind": "dates",
            "timezone": zone,
            "times": sorted({timestamp(item, zone).isoformat() for item in times}),
        }
    if "window_seconds" in value:
        window = value["window_seconds"]
        if (
            isinstance(window, bool)
            or not isinstance(window, (int, float))
            or not 0 < window <= 366 * 86400
        ):
            raise ValueError("invalid schedule catchup window")
        result["window_seconds"] = float(window)
    return result


def due_times(
    schedule: dict, now: datetime, previous: datetime | None = None
) -> list[datetime]:
    now = now_utc(now)
    if schedule["kind"] == "dates":
        candidates = [timestamp(value) for value in schedule["times"]]
    else:
        zone = ZoneInfo(schedule["timezone"])
        today = now.astimezone(zone).date()
        start = today
        if schedule.get("catch_up") and previous is not None:
            start = now_utc(previous).astimezone(zone).date()
        if schedule.get("start_date"):
            start = max(start, datetime.fromisoformat(schedule["start_date"]).date())
        if (today - start).days > 127:
            start = today - timedelta(days=127)
        hour, minute = map(int, schedule["at"].split(":"))
        candidates = []
        day = start
        while day <= today:
            if day.weekday() in schedule["weekdays"] and (
                not schedule.get("end_date") or day.isoformat() <= schedule["end_date"]
            ):
                candidates.append(
                    timestamp(
                        datetime(
                            day.year, day.month, day.day, hour, minute, tzinfo=zone
                        )
                    )
                )
            day += timedelta(days=1)
    return [
        value
        for value in candidates
        if value <= now
        and (
            "window_seconds" not in schedule
            or (now - value).total_seconds() <= schedule["window_seconds"]
        )
    ]


def next_due(schedule: dict, now: datetime) -> datetime | None:
    now = now_utc(now)
    if schedule["kind"] == "dates":
        candidates = [timestamp(value) for value in schedule["times"]]
        return min((value for value in candidates if value > now), default=None)
    zone = ZoneInfo(schedule["timezone"])
    day = now.astimezone(zone).date()
    if schedule.get("start_date"):
        day = max(day, datetime.fromisoformat(schedule["start_date"]).date())
    hour, minute = map(int, schedule["at"].split(":"))
    for offset in range(8):
        current = day + timedelta(days=offset)
        if schedule.get("end_date") and current.isoformat() > schedule["end_date"]:
            return None
        candidate = timestamp(
            datetime(
                current.year, current.month, current.day, hour, minute, tzinfo=zone
            )
        )
        if current.weekday() in schedule["weekdays"] and candidate > now:
            return candidate
    return None
