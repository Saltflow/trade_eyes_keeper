"""Deduplicate prepared report quote snapshots."""

import hashlib
import json
import math
from pathlib import Path

import pandas as pd


def _fingerprint(stock_data) -> str:
    if "quote_status" not in stock_data.columns:
        raise ValueError("report fingerprint requires assessed quotes")

    def text(value):
        return None if value is None or pd.isna(value) else str(value)

    def price(value):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (TypeError, ValueError, OverflowError):
            return None

    rows = []
    for _, row in stock_data.iterrows():
        record = {
            key: text(row.get(key))
            for key in (
                "stock_code",
                "quote_status",
                "quote_as_of",
                "quote_reason",
            )
        }
        record.update(
            {
                key: price(row.get(key))
                for key in ("open", "close", "high", "low", "prev_close")
            }
        )
        rows.append(record)
    # Capture/report wall clocks are intentionally absent: an uncertain send
    # cannot become eligible for replay merely because it was observed again.
    rows.sort(
        key=lambda row: (row["stock_code"] or "", json.dumps(row, sort_keys=True))
    )
    encoded = json.dumps(
        rows, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "quote-v1:" + hashlib.sha256(encoded).hexdigest()


def is_unchanged_report(stock_data, last_pushed_file) -> bool:
    """Skip an empty or previously attempted quote snapshot."""
    if stock_data is None:
        return True
    if isinstance(stock_data, pd.DataFrame) and stock_data.empty:
        return True
    latest = _fingerprint(stock_data)
    try:
        last = Path(last_pushed_file).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return False  # 首次运行，不跳过

    return latest == last


def mark_pushed(last_pushed_file, stock_data) -> None:
    """Record the caller's submitted snapshot, including uncertain delivery.

    Args:
        last_pushed_file: 记录文件路径
        stock_data: 刚推送的 DataFrame
    """
    if stock_data is None or stock_data.empty:
        return
    latest = _fingerprint(stock_data)
    Path(last_pushed_file).write_text(latest, encoding="utf-8")
