"""Exchange opening gate for scheduled briefs; quote quality is assessed centrally."""

from __future__ import annotations

import logging
from datetime import datetime

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
