"""休市检测 — 基于数据指纹比较。

不需要交易日历。拉完数据后和上次推送日期比一下，
如果最新数据日期和上次一样，说明今天没开盘（休市），跳过推送。
"""

from pathlib import Path

import pandas as pd


def is_market_closed(stock_data, last_pushed_file) -> bool:
    """判断是否休市：最新数据日期 == 上次推送日期。

    Args:
        stock_data: 包含 date 列的 DataFrame，或 None
        last_pushed_file: 上次推送日期记录文件路径

    Returns:
        True = 休市/无数据（跳过推送）
        False = 有新数据（应该推送）
    """
    if stock_data is None:
        return True
    if isinstance(stock_data, pd.DataFrame) and stock_data.empty:
        return True
    if "date" not in stock_data.columns:
        return True

    latest = str(stock_data["date"].max().date())[:10]
    try:
        last = Path(last_pushed_file).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return False  # 首次运行，不跳过

    return latest == last


def mark_pushed(last_pushed_file, stock_data) -> None:
    """推送成功后记录最新数据日期。

    Args:
        last_pushed_file: 记录文件路径
        stock_data: 刚推送的 DataFrame
    """
    if stock_data is None or stock_data.empty:
        return
    latest = str(stock_data["date"].max().date())[:10]
    Path(last_pushed_file).write_text(latest, encoding="utf-8")
