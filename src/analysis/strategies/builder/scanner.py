"""条件构建器策略的今日告警扫描。"""

from ...search_interface import Params


def scan_builder_today(params: Params, today: dict, history=None) -> list[dict]:
    """使用 RuleEngine 表达式评估今日条件。"""
    try:
        from ...rule_engine import ExpressionEngine
    except ImportError:
        return []
    ctx = {k: v for k, v in today.items() if v is not None}
    alerts = []
    ee = ExpressionEngine()
    # builder 策略的 scan 复用 RuleEngine 表达式路径
    # 默认规则: MA60 偏离 ±5%
    for cond_str, side, label in [
        ("deviation <= -0.05 and prev_deviation > -0.05", "buy", "MA60偏离买入-5%"),
        ("deviation >= 0.05 and prev_deviation < 0.05", "sell", "MA60偏离卖出+5%"),
    ]:
        try:
            if ee.evaluate(cond_str, ctx):
                alerts.append({"side": side, "label": label, "detail": cond_str})
        except Exception:
            pass
    return alerts
