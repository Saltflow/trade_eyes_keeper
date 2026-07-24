"""条件构建器策略的今日告警扫描。"""

from ...search_interface import Params


def scan_builder_today(params: Params, today: dict, history=None) -> list[dict]:
    """今日告警——直接按 MA60 偏离 ±5% 判断。"""
    alerts = []
    dev = today.get("deviation")
    if dev is None:
        return alerts
    if dev <= -0.05:
        alerts.append({
            "side": "buy", "label": "MA60偏离买入-5%",
            "detail": f"deviation={dev:.1%} <= -5%",
        })
    if dev >= 0.05:
        alerts.append({
            "side": "sell", "label": "MA60偏离卖出+5%",
            "detail": f"deviation={dev:.1%} >= +5%",
        })
    return alerts
