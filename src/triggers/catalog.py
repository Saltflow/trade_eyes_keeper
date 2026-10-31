"""Discoverable, data-only fields shared by reports and trigger predicates."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date
from typing import Any

from ..models.schemas import AlertStock, SessionContext, StockPriceData

_PART = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9_-]*|\*)\Z")
_PRIVATE = re.compile(
    r"(?:password|secret|credential|authorization|api_?key|private_?key|token)",
    re.IGNORECASE,
)
_EXCLUDED = {"config", "env", "errors", "session_id", "created_at"}
HISTORY_FIELDS = frozenset(
    [
        "date",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "amount",
        "amplitude",
        "change_pct",
        "change",
        "turnover",
        "ma60",
        "wma20",
        "wma30",
        "wma50",
        "rsi",
        "rsi_14",
        "macd",
        "macd_signal",
        "macd_hist",
        "adx",
        "plus_di",
        "minus_di",
        "boll_upper",
        "boll_middle",
        "boll_lower",
        "boll_pct_b",
        "boll_bandwidth",
        "volume_ratio",
        "vol_ratio",
        "boll_ma",
    ]
)
VALUATION_FIELDS = frozenset({"pe_ratio", "pb_ratio"})
_FIELD_MEANINGS = {
    "stock_code": "证券代码",
    "stock_name": "证券名称",
    "date": "行情所属日期",
    "open": "开盘价",
    "close": "日报行情收盘/最近价（非鲜活盘中保证）",
    "high": "当日最高价",
    "low": "当日最低价",
    "volume": "成交量",
    "amount": "成交额",
    "amplitude": "振幅",
    "change_pct": "涨跌幅",
    "change": "价格涨跌额",
    "turnover": "换手率",
    "ma60": "60日移动平均价",
    "wma20": "20周移动平均价",
    "wma30": "30周移动平均价",
    "wma50": "50周移动平均价",
    "dividend_per_share": "每股分红",
    "dividend_yield": "股息率",
    "earnings_growth": "盈利增长率",
    "roe": "净资产收益率",
    "pe_ratio": "市盈率PE",
    "pb_ratio": "市净率PB",
    "rsi": "相对强弱指标RSI",
    "rsi_14": "14期相对强弱指标RSI",
    "adx": "趋势强度指标ADX",
    "plus_di": "正向指标DI",
    "minus_di": "负向指标DI",
    "macd": "MACD差值线",
    "macd_signal": "MACD信号线",
    "macd_hist": "MACD柱值",
    "boll_upper": "布林带上轨价",
    "boll_middle": "布林带中轨价",
    "boll_ma": "布林带中轨价",
    "boll_lower": "布林带下轨价",
    "boll_pct_b": "布林带相对位置（0为下轨、1为上轨，可越界）",
    "boll_bandwidth": "布林带宽度（按日报计算原值）",
    "volume_ratio": "成交量相对均量倍数",
    "vol_ratio": "成交量相对均量倍数",
}
_PERCENT_FIELDS = {
    "dividend_yield",
    "earnings_growth",
    "roe",
    "amplitude",
    "change_pct",
    "turnover",
}
_PRICE_FIELDS = {
    "open",
    "close",
    "high",
    "low",
    "change",
    "dividend_per_share",
    "ma60",
    "wma20",
    "wma30",
    "wma50",
    "macd",
    "macd_signal",
    "macd_hist",
    "boll_upper",
    "boll_middle",
    "boll_ma",
    "boll_lower",
}


def _unit(field):
    if field in _PERCENT_FIELDS:
        return "percent_0_100"
    if field in _PRICE_FIELDS:
        return "quote_currency"
    if field in {"pe_ratio", "pb_ratio", "volume_ratio", "vol_ratio"}:
        return "ratio"
    if field in {"rsi", "rsi_14", "adx", "plus_di", "minus_di"}:
        return "index_0_100"
    if field == "boll_pct_b":
        return "ratio_0_1"
    if field == "amount":
        return "quote_currency"
    if field == "volume":
        return "source_volume_unit"
    if field == "date":
        return "ISO_date"
    return "report_native_unknown"


def safe_segment(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(_PART.fullmatch(value))
        and value not in _EXCLUDED
        and not _PRIVATE.search(value)
    )


def safe_path(path: Any) -> bool:
    return (
        isinstance(path, str)
        and len(path) <= 256
        and 1 <= len(path.split(".")) <= 10
        and all(safe_segment(part) for part in path.split("."))
    )


def data_mapping(value: Any) -> dict:
    """Read stored data only, never invoke arbitrary attributes or properties."""
    if isinstance(value, dict):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return vars(value)
    if isinstance(value, (SessionContext, StockPriceData)):
        return vars(value)
    # Report result dataclasses and Pydantic models store their public data here.
    from pydantic import BaseModel

    return vars(value) if isinstance(value, BaseModel) else {}


def iter_safe_leaves(value: Any, prefix: str = "", *, limit: int = 4096):
    """Bounded enumeration of safe JSON-like report data, including empty lists."""
    pending = [(prefix, value)]
    emitted = 0
    while pending and emitted < limit:
        path, current = pending.pop()
        if path and not safe_path(path):
            continue
        mapping = data_mapping(current)
        if mapping:
            for key, child in reversed(list(mapping.items())[:limit]):
                key = str(key).replace(".", "-")
                if safe_segment(key):
                    pending.append((f"{path}.{key}".strip("."), child))
        elif isinstance(current, (list, tuple)) and current:
            for index, child in reversed(list(enumerate(current[:limit]))):
                pending.append((f"{path}.{index}", child))
        elif path and (
            current is None
            or isinstance(current, (str, int, float, bool, date, list, tuple, dict))
        ):
            emitted += 1
            yield path, current


@dataclass(frozen=True)
class FieldDefinition:
    path: str
    value_type: str
    description: str
    producer: str
    availability: str
    source: str
    unit: str | None = None


class FieldCatalog:
    def __init__(self, fields: list[FieldDefinition]):
        self.fields = {item.path: item for item in fields}

    def list_fields(self) -> list[FieldDefinition]:
        return [self.fields[key] for key in sorted(self.fields)]

    def resolve(self, path: str) -> FieldDefinition | None:
        if not safe_path(path):
            return None
        if path in self.fields:
            return self._instrument_definition(path, self.fields[path])
        parts = path.split(".")
        for item in self.list_fields():
            pattern = item.path.split(".")
            if len(pattern) == len(parts) and all(
                left == right or left == "*" or right == "*"
                for left, right in zip(pattern, parts)
            ):
                return self._instrument_definition(path, item)
        return None

    @staticmethod
    def _instrument_definition(path, item):
        parts = path.split(".")
        if (
            len(parts) == 3
            and parts[0] == "stocks_data"
            and parts[2] in VALUATION_FIELDS
        ):
            from ..data.etf_valuation import ETFValuationResolver

            if parts[1] != "*" and ETFValuationResolver.supports(parts[1]):
                return replace(
                    item,
                    producer="report_snapshot",
                    availability="observed"
                    if item.availability == "observed"
                    else "snapshot_only",
                    source="ETF穿透估值仅使用已完成日报快照，轮询不自动抓取成分股估值",
                )
        return item

    def describe_fields(self) -> list[dict]:
        return [asdict(item) for item in self.list_fields()]


def _definition(path: str, value_type: str, *, observed: bool = False):
    parts = path.split(".")
    field = parts[-1]
    producer, source = "report_snapshot", "日报已准备数据；缺失时等待更新"
    if path.startswith("stocks_data.") and field in HISTORY_FIELDS:
        producer, source = "history", "免费历史行情及日报同口径技术指标"
    elif path.startswith("stocks_data.") and field in VALUATION_FIELDS:
        producer, source = (
            "valuation",
            "普通股票免费行情源估值；ETF昂贵穿透估值仅已完成日报快照；保留源数据时点",
        )
    unit = _unit(field)
    meaning = _FIELD_MEANINGS.get(field, "日报字段 " + path)
    if path.startswith("alerts."):
        if field in {"percentage", "percentage_difference"}:
            unit = "percent_0_100"
            meaning = "警报价格相对锚点的偏离百分数（-10表示低于锚点10%）"
        elif field in {"price", "low_price", "anchor_value", "price_difference"}:
            unit = "quote_currency"
            meaning = (
                "警报日内最低价（非实时最新价）"
                if field in {"price", "low_price"}
                else "警报锚点价/价格差"
            )
    return FieldDefinition(
        path,
        value_type,
        f"{meaning}；未知值不视为零或否"
        + (
            "；单位未知，按日报原值，不能猜测百分数换算"
            if unit == "report_native_unknown"
            else ""
        ),
        producer,
        "observed"
        if observed
        else ("on_demand" if producer != "report_snapshot" else "snapshot_only"),
        source,
        unit,
    )


def build_report_field_catalog(session=None) -> FieldCatalog:
    """Enumerate the full public report schema and safe observed extension data."""
    fields = []
    for name, field in StockPriceData.__fields__.items():
        if safe_segment(name):
            kind = getattr(field.type_, "__name__", "scalar")
            fields.append(_definition(f"stocks_data.*.{name}", kind))
    for name in sorted(HISTORY_FIELDS - set(StockPriceData.__fields__)):
        fields.append(_definition(f"stocks_data.*.{name}", "number"))
    for name, field in AlertStock.__fields__.items():
        if safe_segment(name):
            fields.append(
                _definition(
                    f"alerts.*.{name}", getattr(field.type_, "__name__", "scalar")
                )
            )
    for name in SessionContext.__fields__:
        if safe_segment(name) and name != "stocks_data":
            fields.append(_definition(name, "object"))
    fields.append(_definition("ref_portfolio_status", "object"))
    for name in ("last", "open", "high", "low", "prev_close", "change_pct"):
        fields.append(
            FieldDefinition(
                f"quotes.*.{name}",
                "number",
                "实时最新成交价（last）/行情；要求开市且源时间新鲜，low仅为日内最低价",
                "quote",
                "on_demand",
                "QQ实时行情，源quote_time与交易所session校验",
                "percent_0_100" if name == "change_pct" else "quote_currency",
            )
        )
    fields.append(
        FieldDefinition(
            "quotes.*.fresh",
            "boolean",
            "真实行情时点及开市状态是否满足新鲜要求",
            "quote",
            "on_demand",
            "交易所日历及源行情时间",
        )
    )
    fields.append(
        FieldDefinition(
            "events.announcement.q3_report",
            "event_list",
            "第三季度报告正式公告；按原公布日与calendar次日匹配，非列表非空规则",
            "announcements",
            "on_demand",
            "有完整窗口覆盖证明的披露源",
        )
    )
    if session is not None:
        fields.extend(
            _definition(path, type(value).__name__, observed=True)
            for path, value in iter_safe_leaves(session)
        )
    return FieldCatalog(fields)


def describe_fields(session=None) -> list[dict]:
    return build_report_field_catalog(session).describe_fields()
