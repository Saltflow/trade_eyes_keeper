"""Dated distribution evidence and conservative corporate-action reconciliation."""

from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Iterable

import requests
from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from .market_history import CorporateAction


SINA_SHARE_BONUS_URL = (
    "https://vip.stock.finance.sina.com.cn/corp/go.php/"
    "vISSUE_ShareBonus/stockid/{code}.phtml"
)
_EMPTY = {"", "--", "—", "-"}
_CASH_PER_TEN = "sina_cash_per_10="


def _day(value, *, optional: bool = False) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if optional and (value is None or text in _EMPTY):
        return None
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid corporate-action date: {value!r}") from exc


def _number(value) -> Decimal:
    text = str(value).strip().replace(",", "")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"missing or invalid distribution amount: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"invalid distribution amount: {value!r}")
    return result


def _row_values(row) -> list[str]:
    return [
        cell.get_text(" ", strip=True)
        for cell in row.find_all(["td", "th"], recursive=False)
    ]


def _reject_rights_in_window(soup, start: date, end: date, code: str) -> None:
    table = soup.find("table", id="sharebonus_2")
    if table is None:
        raise ValueError(f"{code}: Sina rights table is absent; coverage is unknown")
    if not all(label in table.get_text() for label in ("配股", "除权日")):
        raise ValueError(f"{code}: unrecognized Sina rights table schema")
    for row in table.find_all("tr"):
        values = _row_values(row)
        if len(values) == 1:
            continue
        if len(values) < 5 or not re.match(r"^\d{4}-", values[0]):
            continue
        ex_date = _day(values[4], optional=True)
        if ex_date is not None and start <= ex_date <= end:
            raise ValueError(f"{code}: unsupported rights issue on {ex_date}")


def parse_sina_share_bonus_html(
    html: str | bytes,
    code: str,
    start: date,
    end: date,
    *,
    source_url: str | None = None,
) -> list[CorporateAction]:
    """Parse implemented A-share events; every cash/share field must be explicit.

    The source table reports all three quantities per ten existing shares.
    Its announcement date is retained independently of ex/record dates. It
    does not supply the cash payment date, so that date remains unknown.
    """
    from .market_history import CorporateAction

    code = str(code).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"unsupported Sina A-share code: {code!r}")
    start, end = _day(start), _day(end)
    if end < start:
        raise ValueError("corporate-action window ends before it starts")
    if isinstance(html, bytes):
        html = html.decode("gb18030")
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="sharebonus_1")
    if table is None:
        raise ValueError(f"{code}: Sina dividend table is absent")
    text = table.get_text(" ", strip=True)
    required = ("公告日期", "每10股", "送股", "转增", "派息", "除权除息日")
    if not all(label in text for label in required):
        raise ValueError(f"{code}: unrecognized Sina dividend table schema")
    _reject_rights_in_window(soup, start, end, code)
    url = source_url or SINA_SHARE_BONUS_URL.format(code=code)
    selected = {}
    for row in table.find_all("tr"):
        values = _row_values(row)
        if len(values) == 1:
            continue
        if len(values) < 5:
            if "实施" in values:
                raise ValueError(f"{code}: incomplete implemented dividend row")
            continue
        if values[4] != "实施":
            continue
        if len(values) != 9:
            raise ValueError(f"{code}: incomplete implemented dividend row")
        ex_date = _day(values[5])
        if not start <= ex_date <= end:
            continue
        announced = _day(values[0])
        record = _day(values[6], optional=True)
        stock_market = _day(values[7], optional=True)
        if announced > ex_date or (record is not None and record > ex_date):
            raise ValueError(f"{code}: inconsistent distribution dates on {ex_date}")
        stock, reserve, cash = map(_number, values[1:4])
        if stock == reserve == cash == 0:
            continue
        multiplier = Decimal(1) + (stock + reserve) / Decimal(10)
        diagnostics = [
            "cash_stock_components_complete",
            _CASH_PER_TEN + values[3],
            "sina_stock_per_10=" + values[1],
            "sina_reserve_per_10=" + values[2],
        ]
        if stock_market is not None:
            diagnostics.append("stock_market_date=" + stock_market.isoformat())
        action = CorporateAction(
            code=code,
            action_type=(
                "cash_and_stock_dividend"
                if cash > 0 and multiplier > 1
                else "stock_dividend"
                if multiplier > 1
                else "cash_dividend"
            ),
            ex_date=ex_date,
            published_at=announced,
            record_date=record,
            cash_per_share=float(cash / Decimal(10)),
            share_multiplier=float(multiplier),
            source="sina_share_bonus",
            source_url=url,
            currency="CNY",
            diagnostics=diagnostics,
        )
        previous = selected.get(ex_date)
        if previous is not None:
            fields = ("cash_per_share", "share_multiplier", "record_date")
            if any(getattr(previous, name) != getattr(action, name) for name in fields):
                raise ValueError(f"{code}: conflicting distributions on {ex_date}")
            # A repeated/final implementation notice cannot move knowledge earlier.
            if previous.published_at >= action.published_at:
                continue
        selected[ex_date] = action
    return [selected[key] for key in sorted(selected)]


class SinaCorporateActionProvider:
    """Read one public dividend/rights page; never retries or infers missing cash."""

    def __init__(self, config: dict | None = None, http=None):
        self.http = http or requests.Session()
        settings = (config or {}).get("point_in_time_data", {}) or {}
        market = settings.get("market_history", {}) or {}
        self.timeout = float(market.get("action_timeout_seconds", 20))

    def fetch(self, code: str, start: date, end: date) -> list[CorporateAction]:
        code = str(code).strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError(f"unsupported Sina A-share code: {code!r}")
        url = SINA_SHARE_BONUS_URL.format(code=code)
        response = self.http.get(url, timeout=self.timeout)
        response.raise_for_status()
        return parse_sina_share_bonus_html(
            response.content, code, start, end, source_url=url
        )


def _complete(action) -> bool:
    if action.action_type not in {
        "cash_dividend",
        "stock_dividend",
        "cash_and_stock_dividend",
    }:
        return False
    if action.rights_price is not None or action.published_at is None:
        return False
    if action.published_at > action.ex_date:
        return False
    try:
        cash = _number(action.cash_per_share)
        shares = _number(action.share_multiplier)
    except ValueError:
        return False
    return shares > 0 and (cash > 0 or shares != 1)


def _cash_match(value: float, complete) -> tuple[bool, bool]:
    incoming = _number(value)
    disclosed = _number(complete.cash_per_share)
    if incoming == disclosed:
        return True, False
    raw = next(
        (
            item[len(_CASH_PER_TEN) :]
            for item in complete.diagnostics
            if item.startswith(_CASH_PER_TEN)
        ),
        None,
    )
    if raw is None:
        return False, False
    per_ten = _number(raw)
    # Source precision is measured in yuan per TEN shares. Keep a conservative
    # 1e-5 yuan/share ceiling for terse integer cells; "4" cannot explain 0.449.
    quantum = min(Decimal(1).scaleb(per_ten.as_tuple().exponent - 1), Decimal("1e-5"))
    # The page does not declare a rounding tie convention. Values exactly at
    # half a last digit can represent the same disclosure under HALF_EVEN or
    # HALF_UP; preserve the more precise feed value within that bounded cell.
    matches = abs(incoming - disclosed) <= quantum / Decimal(2)
    return matches, matches


def reconcile_corporate_actions(
    actions: Iterable[CorporateAction],
    supplementary_actions: Iterable[CorporateAction] = (),
) -> list[CorporateAction]:
    """Merge only complete, compatible evidence for the exact code and ex-date.

    Unmatched factors remain unresolved. Conflicting values, rights issues,
    or ambiguous same-day events raise instead of permitting double cash or
    dropping unexplained changes. Inputs are copied and never mutated.
    """
    grouped = defaultdict(list)
    for action in [*actions, *supplementary_actions]:
        grouped[(str(action.code), action.ex_date)].append(deepcopy(action))
    result = []
    for (code, ex_date), group in sorted(grouped.items()):
        if any(a.rights_price is not None or "rights" in a.action_type for a in group):
            raise ValueError(f"{code}: unsupported rights issue on {ex_date}")
        candidates = [action for action in group if _complete(action)]
        if not candidates:
            # No complete real record can discharge a generic factor.
            seen = set()
            for action in group:
                key = action.json(sort_keys=True)
                if key not in seen:
                    result.append(action)
                    seen.add(key)
            continue
        candidates.sort(key=lambda a: (a.source == "sina_share_bonus", a.published_at))
        merged = deepcopy(candidates[-1])
        factors = []
        more_precise_cash = set()
        for action in group:
            if action.action_type not in {
                "adjustment_factor_change",
                "cash_dividend",
                "stock_dividend",
                "cash_and_stock_dividend",
                "stock_split",
            }:
                raise ValueError(f"{code}: unsupported action type on {ex_date}")
            if action.action_type == "adjustment_factor_change":
                if (
                    action.cash_per_share is not None
                    or action.share_multiplier is not None
                ):
                    raise ValueError(f"{code}: ambiguous factor payload on {ex_date}")
                factors.append(action)
                continue
            if action.cash_per_share is not None:
                matches, rounded = _cash_match(action.cash_per_share, merged)
                if not matches:
                    raise ValueError(f"{code}: conflicting dividend cash on {ex_date}")
                if rounded:
                    more_precise_cash.add(_number(action.cash_per_share))
                    merged.diagnostics.append(
                        f"source_rounding_match:{action.source}="
                        f"{action.cash_per_share};sina={merged.cash_per_share}"
                    )
            if action.share_multiplier is not None and _number(
                action.share_multiplier
            ) != _number(merged.share_multiplier):
                raise ValueError(f"{code}: conflicting share multiplier on {ex_date}")
            if action.published_at is not None:
                if action.published_at > ex_date:
                    raise ValueError(f"{code}: noncausal announcement on {ex_date}")
                merged.published_at = max(merged.published_at, action.published_at)
            for field in ("record_date", "payable_date", "currency"):
                value, current = getattr(action, field), getattr(merged, field)
                if value is not None and current is not None and value != current:
                    raise ValueError(f"{code}: conflicting {field} on {ex_date}")
                if current is None and value is not None:
                    setattr(merged, field, value)
            merged.diagnostics.extend(action.diagnostics)
            merged.diagnostics.append(f"reconciled_source:{action.source}")
        if len(more_precise_cash) > 1:
            raise ValueError(f"{code}: ambiguous rounded dividend cash on {ex_date}")
        if more_precise_cash:
            merged.cash_per_share = float(next(iter(more_precise_cash)))
        factor_values = {a.raw_adjustment_factor for a in factors}
        if len(factor_values) > 1:
            raise ValueError(f"{code}: conflicting factor metadata on {ex_date}")
        if factors:
            merged.raw_adjustment_factor = factors[0].raw_adjustment_factor
            merged.diagnostics.append("factor_explained_by_complete_distribution")
            for factor in factors:
                merged.diagnostics.append(f"factor_source:{factor.source}")
                merged.diagnostics.extend(factor.diagnostics)
        merged.diagnostics = list(dict.fromkeys(merged.diagnostics))
        result.append(merged)
    return sorted(result, key=lambda a: (a.ex_date, a.code, a.action_type, a.source))
