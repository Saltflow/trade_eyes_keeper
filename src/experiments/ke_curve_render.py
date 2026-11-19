"""Presentation for the standalone local Ke replay."""

from __future__ import annotations

import html
import io
from datetime import datetime

import numpy as np
import pandas as pd

STRATEGY_ID = "ke_threshold_allocation"
MARKETS = {"a_share": "A股", "hk": "港股", "us": "美股"}


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
    fig, axis = plt.subplots(figsize=(7, 3.2), constrained_layout=True)
    colors = {"a_share": "#d6a72a", "hk": "#2ca02c", "us": "#1f77b4"}
    for item in items:
        if item["status"] == "unavailable":
            continue
        returns = item["values"] / item["initial_capital"] * 100 - 100
        axis.plot(
            pd.to_datetime(item["dates"]),
            returns,
            color=colors[item["group"]],
            linewidth=1.8,
            label=f"{item['label']} {returns[-1]:+.2f}%",
        )
    axis.set_ylabel("税费后收益 %")
    axis.grid(alpha=0.15)
    axis.legend(
        loc="lower left",
        bbox_to_anchor=(0, 1.01),
        ncol=3,
        frameon=False,
        fontsize=14,
        columnspacing=1,
        borderaxespad=0,
    )
    axis.margins(x=0.01)
    axis.spines[["top", "right"]].set_visible(False)
    output = io.BytesIO()
    try:
        fig.savefig(output, format="png", dpi=120)
        return output.getvalue()
    finally:
        plt.close(fig)


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
