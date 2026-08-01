"""股票分组 / 手数 / 跳过设置等辅助函数。"""

import logging

logger = logging.getLogger(__name__)

# ── 常量 ──
MIN_EVAL_DAYS = 60
MIN_TRADING_DAYS = 400
RISK_FREE_A = 0.02
RISK_FREE_NON_A = 0.045


def _detect_stock_group(stock_code: str) -> str:
    """A股(6位数字) vs 非A股"""
    code = str(stock_code).strip()
    return "a_share" if (code.isdigit() and len(code) == 6) else "non_a_share"


def _detect_fine_group(stock_code: str) -> str:
    """细分：a_share(6位) / hk(5位) / us(含字母)"""
    code = str(stock_code).strip()
    if code.isdigit() and len(code) == 6:
        return "a_share"
    if code.isdigit() and len(code) == 5:
        return "hk"
    return "us"


def get_skip_search(config: dict) -> set[str]:
    return {str(c).strip() for c in (config.get("skip_search") or [])}


def get_skip_signals(config: dict) -> set[str]:
    return {str(c).strip() for c in (config.get("skip_signals") or [])}


def _get_lot_size(stock_code: str) -> int:
    code = str(stock_code).strip()
    if code.isdigit() and len(code) == 6:
        return 100
    if code.isdigit() and len(code) == 5:
        return 100
    return 1


def _get_month_key(date_str: str) -> str:
    return date_str[:7]


def _eval_lookback_days() -> int:
    """读 optimizer_constraints.yaml 计算回看天数"""
    try:
        import yaml
        with open("config/optimizer_constraints.yaml", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        wf = raw.get("walk_forward", {}) or {}
        months = int(wf.get("test_months", 9))
        return int(months * 30.4375)
    except Exception:
        return 274
