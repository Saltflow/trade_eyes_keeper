"""Scheduled briefs require an open exchange and a quote from that session."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from ..data.market_calendar import resolve_market_session
from ..instruments.classifier import detect_market

logger = logging.getLogger(__name__)


def has_open_brief_market(codes, as_of: datetime) -> bool:
    checked = set()
    for code in codes:
        market = detect_market(str(code))
        if market in checked:
            continue
        checked.add(market)
        try:
            session = resolve_market_session(str(code), as_of=as_of)
            if session is not None and session.is_open(as_of):
                return True
        except ValueError as exc:
            logger.warning("简报交易日历不可用，跳过该市场 %s: %s", market, exc)
    return False


def active_brief_codes(stock_data, as_of: datetime) -> tuple[str, ...]:
    """Reject cached, pre-open, future and closed-market quote timestamps."""
    eligible = []
    sessions = {}
    for _, row in stock_data.iterrows():
        code = str(row.get("stock_code", ""))
        market = detect_market(code)
        if market not in sessions:
            try:
                sessions[market] = resolve_market_session(code, as_of=as_of)
            except ValueError as exc:
                logger.warning("简报交易日历不可用 %s: %s", market, exc)
                sessions[market] = None
        session = sessions[market]
        if session is None or not session.is_open(as_of):
            continue
        try:
            quote = pd.Timestamp(row.get("quote_time"))
            if pd.isna(quote) or quote.tzinfo is None:
                continue
            quote = quote.tz_convert("UTC").to_pydatetime()
            if session.open <= quote <= as_of.astimezone(timezone.utc):
                eligible.append(code)
        except (ValueError, TypeError, OverflowError):
            continue
    return tuple(eligible)
