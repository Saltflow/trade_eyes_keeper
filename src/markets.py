"""股票市场分组与配置中的跳过设置。"""


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
