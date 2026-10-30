"""Resolve daily-bar cutoffs from exchange sessions and actual closing times."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache

import pandas as pd
from packaging.version import Version

from ..instruments.classifier import detect_market

_CALENDARS = {
    "a_share": ("XSHG", "Asia/Shanghai"),
    "hk": ("XHKG", "Asia/Hong_Kong"),
    "us": ("XNYS", "America/New_York"),
}


@dataclass(frozen=True)
class MarketDataCutoff:
    """Audit the requested date and the latest completed exchange session."""

    calendar: str
    calendar_version: str
    requested_end: date
    effective_end: date
    session_close: datetime
    as_of: datetime

    def as_dict(self) -> dict[str, str]:
        return {
            "calendar": self.calendar,
            "calendar_version": self.calendar_version,
            "requested_end": self.requested_end.isoformat(),
            "effective_end": self.effective_end.isoformat(),
            "session_close": self.session_close.isoformat(),
            "as_of": self.as_of.isoformat(),
        }


@lru_cache(maxsize=64)
def _load_calendar(name: str, year: int):
    # Import lazily: unrelated report and service paths do not need calendars.
    try:
        import exchange_calendars
    except ImportError as exc:
        raise ValueError(
            "exchange-calendars>=4.13.1,<5 is required for market data cutoffs"
        ) from exc
    version = exchange_calendars.__version__
    if not Version("4.13.1") <= Version(version) < Version("5"):
        raise ValueError(
            f"unsupported exchange-calendars version {version}; requires >=4.13.1,<5"
        )
    # Include the prior year so January holidays can resolve to December.
    # Calendars with unpublished/out-of-bounds holidays must fail closed.
    calendar = exchange_calendars.get_calendar(
        name, start=date(year - 1, 1, 1), end=date(year, 12, 31)
    )
    return calendar, version


def resolve_market_data_cutoff(
    code: str,
    requested_end: date,
    *,
    as_of: datetime | None = None,
) -> MarketDataCutoff:
    """Use the last closed session at or before the requested market date.

    Session closing times include exchange holidays, half days and DST. A
    current or future request is additionally capped by the observation time;
    an in-progress daily bar is never considered a completed session.
    """
    observed = pd.Timestamp(as_of or datetime.now(timezone.utc))
    if observed.tzinfo is None:
        raise ValueError("market cutoff as_of must include a timezone")
    observed = observed.tz_convert("UTC")
    market = detect_market(code)
    if market not in _CALENDARS:
        raise ValueError(f"no exchange calendar for market {market}: {code}")
    name, market_timezone = _CALENDARS[market]
    candidate_end = min(requested_end, observed.tz_convert(market_timezone).date())
    try:
        calendar, version = _load_calendar(name, candidate_end.year)
        schedule = calendar.schedule
        eligible = schedule.loc[
            (schedule.index <= pd.Timestamp(candidate_end))
            & (schedule["close"] <= observed)
        ]
        if eligible.empty:
            raise ValueError("no completed exchange session in calendar range")
        session = eligible.index[-1]
        close = pd.Timestamp(eligible.iloc[-1]["close"])
    except Exception as exc:
        raise ValueError(
            f"{name} calendar unavailable for {candidate_end.isoformat()}: {exc}"
        ) from exc
    return MarketDataCutoff(
        calendar=name,
        calendar_version=version,
        requested_end=requested_end,
        effective_end=session.date(),
        session_close=close.to_pydatetime(),
        as_of=observed.to_pydatetime(),
    )


@dataclass(frozen=True)
class MarketSession:
    open: datetime
    close: datetime
    break_start: datetime | None = None
    break_end: datetime | None = None

    def is_open(self, as_of: datetime) -> bool:
        if as_of.tzinfo is None:
            raise ValueError("market session as_of must include a timezone")
        observed = as_of.astimezone(timezone.utc)
        return self.open <= observed < self.close and not (
            self.break_start is not None
            and self.break_end is not None
            and self.break_start <= observed < self.break_end
        )


def resolve_market_session(
    code: str, *, as_of: datetime
) -> MarketSession | None:
    """Today's actual regular session, including holidays, breaks and half days."""
    if as_of.tzinfo is None:
        raise ValueError("market session as_of must include a timezone")
    market = detect_market(code)
    name, zone = _CALENDARS[market]
    day = pd.Timestamp(as_of).tz_convert(zone).date()
    try:
        calendar, _ = _load_calendar(name, day.year)
        label = pd.Timestamp(day)
        if label not in calendar.schedule.index:
            return None
        row = calendar.schedule.loc[label]

        def clock(field):
            value = row.get(field)
            return None if pd.isna(value) else pd.Timestamp(value).to_pydatetime()

        opening, closing = clock("open"), clock("close")
        if opening is None or closing is None:
            raise ValueError("exchange session has no opening or closing time")
        return MarketSession(
            open=opening, close=closing,
            break_start=clock("break_start"), break_end=clock("break_end"),
        )
    except Exception as exc:
        raise ValueError(f"{name} session unavailable for {day}: {exc}") from exc
