"""Shared event and validated Ke text report builders."""

from __future__ import annotations

import html as html_lib
from datetime import date, datetime
from itertools import groupby

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from ..markets import _detect_fine_group


def _fmt(v, unit="", fmt_spec=".2f"):
    """安全格式化数值：None/pd.NA/NaN → "—"，数值 → 格式化。

    原则：绝不凭空造零。缺失数据和真实零值必须可区分。
    """
    if v is None:
        return "—"
    try:
        if pd.isna(v):
            return "—"
    except (TypeError, ValueError):
        pass
    if unit:
        return f"{v:{fmt_spec}}{unit}"
    return f"{v:{fmt_spec}}"


def _html_escape(value, default="—") -> str:
    """Escape notification data without collapsing real zeroes to a dash."""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    return html_lib.escape(str(value), quote=True)


def _safe_html_url(value) -> str:
    """Return an escaped http(s) URL or an empty string for unsafe input."""
    if value is None:
        return ""
    raw = str(value).strip()
    if raw.lower().startswith(("http://", "https://")):
        return html_lib.escape(raw, quote=True)
    return ""


def build_strategy_text_summary(session, markdown: bool = False) -> str:
    """Validated Ke curves as the text-channel substitute for the shared chart."""
    from .report_policy import build_return_curves_text

    return build_return_curves_text(getattr(session, "strategy_return_curves", None))


def _event_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


EVENT_CATEGORIES = (
    "重组并购与重大事项",
    "财报业绩",
    "分红派息",
    "股权融资与解禁",
    "例行披露",
    "其他",
)
EVENT_MARKETS = {"a_share": "A股", "hk": "港股", "us": "美股", "sg": "新加坡"}


def _event_category(item):
    """Specific filing forms win over incidental words such as annual/dividend."""
    title = str(item.get("title") or "").lower()
    source = " ".join(str(item.get(key) or "") for key in ("category", "type")).lower()
    routine_forms = (
        ("翌日披露", ("翌日披露", "next day disclosure", "next-day disclosure")),
        ("证券变动月报表", ("月報表", "月报表", "monthly return")),
        ("业绩说明会", ("业绩说明会", "業績說明會", "earnings call")),
        (
            "投票结果",
            ("投票表决结果", "投票表決結果", "投票结果", "投票結果", "voting results"),
        ),
    )
    for label, words in routine_forms:
        if any(word in title or word in source for word in words):
            return 4, label
    rules = (
        (
            0,
            (
                "重组",
                "重組",
                "收购",
                "收購",
                "并购",
                "併購",
                "合并",
                "合併",
                "控制权",
                "控制權",
                "重大资产",
                "重大資產",
                "关联交易",
                "關連交易",
                "merger",
                "acquisition",
                "restructuring",
                "material agreement",
            ),
        ),
        (
            2,
            (
                "分红",
                "分紅",
                "派息",
                "股息",
                "权益分派",
                "權益分派",
                "利润分配",
                "利潤分配",
                "dividend",
                "distribution",
            ),
        ),
        (
            3,
            (
                "定增",
                "增发",
                "增發",
                "配售",
                "解禁",
                "回购",
                "回購",
                "增持",
                "减持",
                "減持",
                "repurchase",
                "buyback",
                "placing",
                "lock-up",
            ),
        ),
        (
            1,
            (
                "季度报告",
                "季度報告",
                "季度财务",
                "季度財務",
                "年度报告",
                "年度報告",
                "年报",
                "年報",
                "季报",
                "季報",
                "中期报告",
                "中期報告",
                "半年度报告",
                "半年度報告",
                "季度业绩",
                "季度業績",
                "年度业绩",
                "年度業績",
                "中期业绩",
                "中期業績",
                "業績公佈",
                "业绩预告",
                "業績預告",
                "业绩快报",
                "業績快報",
                "盈利预警",
                "盈利預警",
                "10-k",
                "10-q",
                "20-f",
                "40-f",
                "n-csr",
                "n-csrs",
                "quarterly results",
                "annual results",
                "annual report",
                "full yearly results",
                "interim results",
                "earnings",
                "financial statements",
                "产销快报",
                "產銷快報",
                "经营数据",
                "經營數據",
                "operating results",
            ),
        ),
        (
            4,
            (
                "通函",
                "投票表决",
                "投票表決",
                "投票结果",
                "投票結果",
                "会议通知",
                "會議通告",
                "业绩说明会",
                "業績說明會",
                "circular",
                "notice of meeting",
                "voting results",
            ),
        ),
    )
    # Reliable provider categories take precedence; generic categories fall back to titles.
    for text in (source, title):
        for category, words in rules:
            if any(word in text for word in words):
                return category, EVENT_CATEGORIES[category]
    return 5, "其他"


def _event_date(value):
    if value is None or not isinstance(value, (str, date, pd.Timestamp)):
        return None
    try:
        day = pd.Timestamp(value)
        if pd.isna(day):
            return None
        if day.tzinfo is not None:
            day = day.tz_convert("Asia/Shanghai")
        return day.date()
    except (ValueError, TypeError, OverflowError):
        return None


def group_daily_event_rows(events):
    """Stable category/market/instrument groups for every presentation channel."""
    for _, rows in groupby(
        events,
        key=lambda row: (row["priority"], row["category"], row["market"], row["code"]),
    ):
        rows = list(rows)
        first = rows[0]
        yield {
            "category": first["category_label"],
            "market": first["market_label"],
            "code": first["code"],
            "name": first["name"],
            "rows": rows,
        }


def build_daily_event_rows(
    announcements,
    placements,
    stock_data,
    dividend_events=None,
    *,
    as_of=None,
    config=None,
):
    """Share the complete, ordered event feed across report channels."""
    events = []
    row_map = (
        {str(row.get("stock_code", "")): row for _, row in stock_data.iterrows()}
        if stock_data is not None and hasattr(stock_data, "iterrows")
        else {}
    )
    for item in dividend_events or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "") or "")
        if status not in {"未除权", "已除权未派息"}:
            continue
        code = str(item.get("code", "") or "").strip()
        ex_date = str(item.get("ex_date", "") or "")
        payment_date = str(item.get("payment_date", "") or "")
        cash_per_share = _event_number(item.get("cash_per_share"))
        currency = str(item.get("currency", "") or "").strip().upper()
        name = row_map.get(code, {}).get("stock_name", "")
        amount = (
            f"每股/份现金 {currency + ' ' if currency else ''}{cash_per_share:.4f}"
            if cash_per_share is not None
            else "金额待公告"
        )
        schedule = f"除权 {ex_date or '待确认'}"
        if payment_date:
            schedule += f" · 派息 {payment_date}"
        events.append(
            {
                "date": ex_date,
                "code": code,
                "kind": status,
                "title": f"{name} · {amount} · {schedule}",
                "url": str(item.get("source_url") or ""),
                "category": 2,
                "priority": 0 if status == "已除权未派息" else 1,
            }
        )
    months = (
        ((config or {}).get("report") or {})
        .get("events", {})
        .get("announcement_lookback_months", 2)
    )
    if type(months) is not int or not 1 <= months <= 24:
        raise ValueError("公告展示期限要求1–24个自然月")
    report_date = (
        _event_date(as_of)
        if as_of is not None
        else datetime.now(ZoneInfo("Asia/Shanghai")).date()
    )
    if report_date is None:
        raise ValueError("报表日期无效")
    cutoff = (pd.Timestamp(report_date) - pd.DateOffset(months=months)).date()
    seen = set()
    for code, items in (announcements or {}).items():
        for item in items or []:
            if isinstance(item, dict):
                published = _event_date(item.get("published_at")) or _event_date(
                    item.get("date")
                )
                date = published.isoformat() if published else ""
                if published is not None and published < cutoff:
                    continue
                title = str(item.get("title", "未命名公告") or "未命名公告")
                url = html_lib.unescape(_safe_html_url(item.get("url")))
                category, subtype = _event_category(item)
            else:
                date, title, url = "", str(item), ""
                category, subtype = 5, "其他"
            identity = (str(code), date, url or title)
            if identity in seen:
                continue
            seen.add(identity)
            events.append(
                {
                    "date": date,
                    "code": str(code),
                    "kind": subtype,
                    "title": title,
                    "url": url,
                    "category": category,
                    "priority": 3,
                }
            )
    for code, item in (placements or {}).items():
        if not isinstance(item, dict):
            continue
        unlock_date = str(item.get("unlock_date", "") or "")
        name = row_map.get(str(code), {}).get("stock_name", "")
        issue_num = _event_number(item.get("issue_num"))
        issue_price = _event_number(item.get("issue_price"))
        pct_of_total = _event_number(item.get("pct_of_total"))
        issue_num_text = _fmt(
            issue_num / 1e8 if issue_num is not None else None,
            "亿股",
            ".2f",
        )
        events.append(
            {
                "date": unlock_date,
                "code": str(code),
                "kind": "未解禁定增",
                "title": (
                    f"{name} · {issue_num_text} · "
                    f"占总股本 {_fmt(pct_of_total, '%', '.2f')} · "
                    f"定增价 {_fmt(issue_price, '元', '.2f')} · "
                    f"解禁 {unlock_date or '待确认'}"
                ),
                "url": "",
                "category": 3,
                "priority": 2,
            }
        )
    for event in events:
        event["category_label"] = (
            event["kind"]
            if event["priority"] < 3
            else EVENT_CATEGORIES[event["category"]]
        )
        event["market"] = _detect_fine_group(event["code"])
        event["market_label"] = EVENT_MARKETS[event["market"]]
        event["name"] = str(
            row_map.get(event["code"], {}).get("stock_name") or event["code"]
        )
    events.sort(
        key=lambda row: (
            row["priority"],
            row["category"],
            tuple(EVENT_MARKETS).index(row["market"]),
            row["code"],
            not bool(_event_date(row["date"])),
            -(_event_date(row["date"]).toordinal() if _event_date(row["date"]) else 0),
            row["title"],
            row["url"],
        )
    )
    combined, routine = [], {}
    for row in events:
        if row["category"] == 4 and row["kind"] in {"翌日披露", "证券变动月报表"}:
            key = (row["market"], row["code"], row["kind"])
            if key in routine:
                routine[key]["members"].append(row)
                continue
            row = {**row, "members": [row]}
            routine[key] = row
        combined.append(row)
    return combined


def build_daily_events_text(session, stock_data=None) -> str:
    """Use the same event rows as email, without silently truncating the feed."""
    rows = build_daily_event_rows(
        getattr(session, "announcements", None),
        getattr(session, "placements", None),
        stock_data,
        getattr(session, "dividend_events", None),
        as_of=getattr(session, "created_at", None),
        config=getattr(session, "config", None),
    )
    lines = []
    last = None
    for group in group_daily_event_rows(rows):
        heading = (group["category"], group["market"])
        if heading != last:
            lines.append(_html_escape(" · ".join(heading)))
            last = heading
        lines.append(f"{_html_escape(group['code'])} {_html_escape(group['name'])}")
        for row in group["rows"]:
            members = row.get("members") or [row]
            if len(members) > 1:
                lines.append(f"{_html_escape(row['kind'])} {len(members)}份")
            for member in members:
                lines.append(
                    f"{_html_escape(member['date'] or '日期待确认')} · {_html_escape(member['title'])}"
                    + (
                        f"\n原文：{_html_escape(member['url'])}"
                        if member.get("url")
                        else ""
                    )
                )
    return "\n".join(lines)


def report_data_readiness(session) -> dict:
    """One readiness projection for execution receipts and report renderers."""
    config = getattr(session, "config", {})
    if not isinstance(config, dict) or "stocks" not in config:
        return {"status": "not_applicable"}
    expected = {
        str(stock.get("code", "") if isinstance(stock, dict) else stock).strip()
        for stock in config.get("stocks", [])
    }
    data = getattr(session, "stocks_data", {}) or {}
    available = set(data) if isinstance(data, dict) else set()
    missing = sorted(expected - available)
    errors = getattr(session, "errors", []) or []
    analysis_issues = {}
    quality = getattr(session, "report_quote_quality", None)
    quotes = quality.get("quotes", {}) if isinstance(quality, dict) else {}
    quote_issues = {
        code: quotes.get(code, {}).get("quote_reason") or "缺少报价质量判定"
        for code in expected
        if quotes.get(code, {}).get("quote_status", "unavailable") == "unavailable"
        or quotes.get(code, {}).get("quote_missing_fields")
    }
    return {
        "status": "not_ready" if missing or errors or quote_issues else "ready",
        "expected_codes": sorted(expected),
        "available_codes": sorted(available),
        "missing_codes": missing,
        "error_count": len(errors),
        "analysis_issues": analysis_issues,
        "quote_issues": quote_issues,
        "quote_as_of": quality.get("as_of") if isinstance(quality, dict) else None,
        "quote_counts": quality.get("counts", {}) if isinstance(quality, dict) else {},
    }
