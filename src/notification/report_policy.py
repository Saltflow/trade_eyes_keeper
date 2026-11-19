"""Shared report presentation for prices and fundamentals."""

from __future__ import annotations

import numpy as np
import pandas as pd

MARKETS = {"a_share": "A股", "hk": "港股", "us": "美股"}
DISPLAY_MARKETS = {**MARKETS, "sg": "新加坡"}
FUNDAMENTAL_COLUMNS = (
    ("stock_code", "代码"),
    ("stock_name", "名称"),
    ("quote_display", "行情状态 / 截至"),
    ("open", "开盘"),
    ("prev_close", "前收"),
    ("close", "报价"),
    ("pe_ratio", "PE"),
    ("pb_ratio", "PB"),
    ("roe", "ROE%"),
)
ROE_ESTIMATE_NOTE = (
    "ROE 带“估”的数值按 PB÷PE×100% 粗估，可能存在口径或时点差异；无标记的值为已有 ROE。"
)
PRICE_COLORS = {
    "up": "#c43c45",
    "down": "#21865c",
    "flat": "#63758a",
    "unknown": "#8995a3",
}
QUOTE_STATUS_LABELS = {
    "realtime": "实时",
    "delayed": "延迟采样",
    "last_close": "最近收盘",
    "unavailable": "缺口",
}


def quote_presentation(row) -> dict[str, str]:
    """Display the frozen quote assessment without applying another clock policy."""
    status = row.get("quote_status")
    if not isinstance(status, str) or status not in QUOTE_STATUS_LABELS:
        status, label = "unavailable", "时效未确认"
    else:
        label = QUOTE_STATUS_LABELS[status]
    value = row.get("quote_as_of")
    source_time = str(value) if value is not None and not pd.isna(value) else ""
    reason = row.get("quote_reason")
    reason = str(reason) if reason is not None and not pd.isna(reason) else ""
    if status != "unavailable":
        try:
            source = pd.Timestamp(value)
            if pd.isna(source) or source.tzinfo is None:
                raise ValueError("source clock must include timezone")
        except (TypeError, ValueError, OverflowError):
            status, label = "unavailable", "时效未确认"
            reason = reason or "缺少可核验的行情源时间"
            source_time = ""
    # Keep the source timezone visible. Neither the report date nor the fetch
    # time can stand in for an unavailable source timestamp.
    source_time = source_time.replace("T", " ")
    parts = [label, "截至 " + source_time if source_time else "源时间未知"]
    if reason:
        parts.append(reason)
    return {
        "status": status,
        "label": label,
        "as_of": source_time,
        "reason": reason,
        "text": " · ".join(parts),
    }


def quote_status_summary(stock_data) -> str:
    """Count the same frozen states that each rendered instrument displays."""
    counts = dict.fromkeys(QUOTE_STATUS_LABELS, 0)
    if stock_data is not None and hasattr(stock_data, "iterrows"):
        for _, row in stock_data.iterrows():
            counts[quote_presentation(row)["status"]] += 1
    return " · ".join(
        f"{QUOTE_STATUS_LABELS[status]} {count}" for status, count in counts.items()
    )


def format_price(value) -> str:
    """Keep meaningful quote precision, with two to eight decimal places."""
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if not np.isfinite(price) or price <= 0:
        return "—"
    if price < 0.00000001:
        return "<0.00000001"
    whole, fractional = f"{price:.8f}".split(".")
    return whole + "." + fractional.rstrip("0").ljust(2, "0")


def price_movement(close, prev_close) -> tuple[str, str]:
    """Compare original quotes; never derive a direction from rounded display values."""
    try:
        current, previous = float(close), float(prev_close)
        if (
            not np.isfinite(current)
            or not np.isfinite(previous)
            or previous <= 0
            or current <= 0
        ):
            raise ValueError("invalid comparison price")
    except (TypeError, ValueError):
        return "unknown", "涨跌未知"
    direction = "up" if current > previous else "down" if current < previous else "flat"
    return direction, f"{(current / previous - 1) * 100:+.2f}%"


def implied_ke(pe_ratio) -> float | None:
    try:
        pe = float(pe_ratio)
        result = 100 * (0.02 + 1 / pe) if np.isfinite(pe) and pe > 0 else None
        return result if result is not None and np.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def format_roe(row) -> str:
    """Keep reported ROE; label the requested PB/PE display fallback explicitly."""

    def number(value):
        try:
            result = float(value)
            return result if np.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    reported = number(row.get("roe"))
    if reported is not None:
        return f"{reported:.2f}"
    pe, pb = number(row.get("pe_ratio")), number(row.get("pb_ratio"))
    if pe is None or pe == 0 or pb is None or pb <= 0:
        return "—"
    estimate = pb / pe * 100
    return f"{estimate:.2f}(估)" if np.isfinite(estimate) else "—"


def fundamental_rows(stock_data) -> list[list[str]]:
    if stock_data is None or not hasattr(stock_data, "iterrows"):
        return []
    rows = []
    for _, row in stock_data.iterrows():
        values = []
        for key, _ in FUNDAMENTAL_COLUMNS:
            value = row.get(key)
            if key == "quote_display":
                values.append(quote_presentation(row)["text"])
            elif key in {"open", "prev_close", "close"} and row.get(
                "quote_status"
            ) == "unavailable":
                values.append("—")
            elif key in {"open", "prev_close", "close"}:
                values.append(format_price(value))
            elif key == "roe":
                values.append(format_roe(row))
            elif value is None or (np.isscalar(value) and pd.isna(value)):
                values.append("—")
            elif key in {"stock_code", "stock_name", "date"}:
                values.append(str(value))
            else:
                try:
                    values.append(f"{float(value):.2f}")
                except (TypeError, ValueError):
                    values.append("—")
        rows.append(values)
    return sorted(rows, key=lambda row: row[0])


def fundamental_text(stock_data) -> str:
    lines = [quote_status_summary(stock_data)]
    lines.append(" / ".join(label for _, label in FUNDAMENTAL_COLUMNS))
    lines.extend(" / ".join(row) for row in fundamental_rows(stock_data))
    lines.append(ROE_ESTIMATE_NOTE)
    return "\n".join(lines)
