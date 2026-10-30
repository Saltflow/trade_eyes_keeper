"""Interpret source quote clocks without relabelling them as fetch time."""

from datetime import datetime
from zoneinfo import ZoneInfo

from ..instruments.classifier import detect_market


def parse_quote_time(
    value: str, code: str, *, market: str | None = None
) -> datetime | None:
    clock = str(value).strip().strip('";')
    for pattern in (
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(clock, pattern)
        except ValueError:
            continue
        zone = {
            "a_share": "Asia/Shanghai",
            "hk": "Asia/Hong_Kong",
            "us": "America/New_York",
            "sg": "Asia/Singapore",
        }.get(market or detect_market(code))
        if zone is None:
            return None
        return parsed.replace(tzinfo=ZoneInfo(zone))
    return None
