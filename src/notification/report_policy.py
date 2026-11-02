"""Explicit report presentation policy; never substitutes an old strategy curve."""

from __future__ import annotations

import copy
import html
import io
from datetime import datetime

import numpy as np
import pandas as pd

STRATEGY_ID = "ke_threshold_allocation"
MARKETS = {"a_share": "A股", "hk": "港股", "us": "美股"}
FUNDAMENTAL_COLUMNS = (
    ("stock_code", "代码"),
    ("stock_name", "名称"),
    ("date", "行情日期"),
    ("open", "今开"),
    ("prev_close", "昨收"),
    ("close", "现价"),
    ("pe_ratio", "PE"),
    ("pb_ratio", "PB"),
    ("roe", "ROE%"),
)
ROE_ESTIMATE_NOTE = (
    "ROE 带“估”的数值按 PB÷PE×100% 粗估，可能存在口径或时点差异；无标记的值为已有 ROE。"
)


def technical_signals_enabled(config: dict | None) -> bool:
    report = (config or {}).get("report") or {}
    return (report.get("technical_signals") or {}).get("enabled", True) is not False


def returns_only(config: dict | None) -> bool:
    return ((config or {}).get("report") or {}).get(
        "strategy_display"
    ) == "returns_only"


def report_view(session, config: dict | None = None):
    """Suppress stale technical results on a shallow, non-mutating view."""
    config = config if config is not None else getattr(session, "config", {})
    if technical_signals_enabled(config) and not returns_only(config):
        return session
    view = copy.copy(session)
    object.__setattr__(view, "config", config)
    object.__setattr__(view, "signal_scan", None)
    for name in ("evaluation_reports", "backtest", "ref_portfolio_status"):
        object.__setattr__(view, name, {})
    return view


def return_curve_markets(payload) -> list[dict]:
    """Validate raw NAV and explicit coverage before displaying performance."""
    markets = payload.get("markets", {}) if isinstance(payload, dict) else {}
    source_ok = isinstance(payload, dict) and payload.get("strategy_id") == STRATEGY_ID
    result = []
    for group, label in MARKETS.items():
        item = markets.get(group, {}) if source_ok and isinstance(markets, dict) else {}
        if not isinstance(item, dict):
            item = {}
        coverage = item.get("coverage", {}) if isinstance(item, dict) else {}
        status = (
            coverage.get("status", "unavailable")
            if isinstance(coverage, dict)
            else "unavailable"
        )
        message = (
            str(coverage.get("message") or "")[:240]
            if isinstance(coverage, dict)
            else ""
        )
        dates, nav = [], np.empty(0, dtype=float)
        initial = 0.0
        if status in {"complete", "partial"}:
            try:
                dates = list(item["dates"])
                parsed = [
                    datetime.fromisoformat(str(day).replace("Z", "+00:00"))
                    for day in dates
                ]
                nav = np.asarray(item["values"], dtype=float)
                initial = float(item["initial_capital"])
                if (
                    len(parsed) < 2
                    or nav.shape != (len(parsed),)
                    or not np.all(np.isfinite(nav))
                    or np.any(nav <= 0)
                    or not np.isfinite(initial)
                    or initial <= 0
                    or any(left >= right for left, right in zip(parsed, parsed[1:]))
                ):
                    raise ValueError("invalid NAV series")
            except (KeyError, TypeError, ValueError, OverflowError):
                status, message, dates, nav = (
                    "unavailable",
                    "收益曲线数据不可用",
                    [],
                    np.empty(0),
                )
        else:
            status = "unavailable"
        result.append(
            {
                "group": group,
                "label": label,
                "status": status,
                "message": message,
                "dates": dates,
                "values": nav,
                "initial_capital": initial,
                "data_as_of": str(item.get("data_as_of") or ""),
                "coverage_label": str(coverage.get("label") or "历史Ke覆盖不完整")[:80]
                if isinstance(coverage, dict)
                else "历史Ke覆盖不完整",
            }
        )
    return result


def build_return_curves_text(payload) -> str:
    lines = ["Ke策略收益曲线（历史模拟；非实盘收益）"]
    alphabet = "▁▂▃▄▅▆▇█"
    for item in return_curve_markets(payload):
        label, status = item["label"], item["status"]
        if status == "unavailable":
            lines.append(f"{label}：暂不可用；{item['message'] or '等待历史Ke数据'}")
            continue
        values = item["values"] / item["initial_capital"] * 100 - 100
        selected = values[
            np.linspace(0, len(values) - 1, min(len(values), 40)).astype(int)
        ]
        span = float(np.ptp(selected))
        spark = "".join(
            alphabet[min(7, int((value - selected.min()) / span * 7))]
            if span
            else alphabet[0]
            for value in selected
        )
        coverage = "完整" if status == "complete" else item["coverage_label"]
        lines.append(f"{label}：{spark}  {values[-1]:+.2f}%")
        lines.append(
            f"{str(item['dates'][0])[:10]} 至 {str(item['dates'][-1])[:10]} · {coverage}"
        )
        if item["message"]:
            lines.append(item["message"])
        if item["data_as_of"]:
            lines.append(f"数据截至 {item['data_as_of']}")
    return "\n".join(lines)


def generate_return_curve_png(payload) -> bytes | None:
    items = return_curve_markets(payload)
    if not any(item["status"] != "unavailable" for item in items):
        return None
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from ..utils.font_setup import setup_cjk_font

    setup_cjk_font()
    fig, axes = plt.subplots(3, 1, figsize=(9, 8), constrained_layout=True)
    for axis, item in zip(axes, items):
        axis.set_title(f"{item['label']} · Ke策略（历史模拟）")
        if item["status"] == "unavailable":
            axis.text(
                0.5,
                0.5,
                "暂不可用：等待历史Ke数据",
                ha="center",
                transform=axis.transAxes,
            )
            axis.set_axis_off()
            continue
        axis.plot(
            pd.to_datetime(item["dates"]),
            item["values"] / item["initial_capital"] * 100 - 100,
        )
        axis.set_ylabel("税费后收益 %")
        axis.grid(alpha=0.2)
        if item["status"] == "partial":
            axis.text(
                0.5,
                0.5,
                item["coverage_label"],
                color="darkred",
                alpha=0.7,
                ha="center",
                transform=axis.transAxes,
            )
    output = io.BytesIO()
    try:
        fig.savefig(output, format="png", dpi=120)
        return output.getvalue()
    finally:
        plt.close(fig)


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
            if key == "roe":
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
    lines = [" / ".join(label for _, label in FUNDAMENTAL_COLUMNS)]
    lines.extend(" / ".join(row) for row in fundamental_rows(stock_data))
    lines.append(ROE_ESTIMATE_NOTE)
    return "\n".join(lines)


def return_curves_html(payload, image_source: str | None = None) -> str:
    text = html.escape(build_return_curves_text(payload))
    image = (
        (
            f'<img alt="Ke策略收益曲线" src="{html.escape(image_source, quote=True)}" '
            'style="max-width:100%;height:auto">'
        )
        if image_source
        else ""
    )
    return (
        f"<section><h3>Ke策略收益曲线</h3>{image}"
        f'<pre style="white-space:pre-wrap;overflow-wrap:anywhere">{text}</pre></section>'
    )
