"""新浪财经股指/ETF 期权数据源。

该模块与股票行情爬虫保持隔离。新浪的期权接口同时覆盖：

* 中金所 IO/HO/MO 指数期权的合约月份、Call/Put 实时链和日线；
* 上交所 510300/510500 等 ETF 期权的合约月份、Call/Put 实时链和日线。

新浪没有统一的期权 SDK，返回值也分为 JSON、JSONP 和 GBK 行情文本，
因此这里集中处理协议细节，向上提供稳定的 DataFrame 字段。
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd
import requests

logger = logging.getLogger(__name__)


SINA_CFFEX_PAGE_URL = (
    "https://stock.finance.sina.com.cn/futures/view/optionsCffexDP.php"
)
SINA_CFFEX_CHAIN_URL = (
    "https://stock.finance.sina.com.cn/futures/api/openapi.php/"
    "OptionService.getOptionData"
)
SINA_CFFEX_DAILY_URL = (
    "https://stock.finance.sina.com.cn/futures/api/jsonp.php//"
    "FutureOptionAllService.getOptionDayline"
)
SINA_ETF_METADATA_URL = (
    "https://stock.finance.sina.com.cn/futures/api/openapi.php/"
    "StockOptionService.getStockName"
)
SINA_QUOTE_URL = "https://hq.sinajs.cn/list={}"
SINA_ETF_DAILY_URL = (
    "https://stock.finance.sina.com.cn/futures/api/jsonp_v2.php//"
    "StockOptionDaylineService.getSymbolInfo"
)

# 中金所产品代码。MO 是中证 1000，并非中证 500；中证 500 在本文支持的
# ETF 期权中使用 510500。
CFFEX_PRODUCTS = {
    "ho": "上证 50",
    "io": "沪深 300",
    "mo": "中证 1000",
}
ETF_OPTION_CATEGORIES = {
    "510050": "50ETF",
    "510300": "300ETF",
    "510500": "500ETF",
    "588000": "科创50",
    "588080": "科创板50",
}

COMMON_OPTION_COLUMNS = [
    "option_code",
    "exchange",
    "underlying",
    "product",
    "contract_month",
    "option_type",
    "side",
    "strike",
    "bid_volume",
    "bid",
    "last",
    "ask",
    "ask_volume",
    "open_interest",
    "change_pct",
    "prev_close",
    "open",
    "high",
    "low",
    "volume",
    "amount",
    "quote_time",
    "expiry_date",
    "contract_name",
    "source",
    "source_url",
    "as_of",
]

DAILY_OPTION_COLUMNS = [
    "date",
    "option_code",
    "product",
    "contract_month",
    "option_type",
    "strike",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "source_url",
]


class SinaOptionError(RuntimeError):
    """新浪期权接口返回异常或无法解析时抛出的错误。"""


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    number = _as_float(value)
    return int(number) if number is not None else None


def _decode_bytes(content: bytes, encodings: Sequence[str]) -> str:
    for encoding in encodings:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode(encodings[0], errors="replace")


def _jsonp_value(text: str) -> Any:
    """从新浪 JSONP 前缀中提取第一个 JSON 数组或对象。"""
    candidates = [(text.find("["), text.rfind("]")), (text.find("{"), text.rfind("}"))]
    candidates = [item for item in candidates if item[0] >= 0 and item[1] > item[0]]
    if not candidates:
        raise SinaOptionError("新浪期权响应中没有 JSON 内容")
    start, end = min(candidates, key=lambda item: item[0])
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SinaOptionError("新浪期权 JSON 内容解析失败") from exc


def _normalize_month(value: str) -> str:
    """将 2026-09、202609、2609 统一为新浪使用的 YYMM。"""
    raw = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        year, month = raw.split("-")
        return year[-2:] + month
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 6:
        return digits[:4][-2:] + digits[4:]
    if len(digits) == 4:
        return digits
    raise SinaOptionError("无法识别期权合约月份: {}".format(value))


def _side_code(option_type: str) -> str:
    value = str(option_type).strip().lower()
    if value in {"c", "call", "认购", "up"}:
        return "UP"
    if value in {"p", "put", "认沽", "down"}:
        return "DOWN"
    raise SinaOptionError("期权类型必须是 call/C/认购 或 put/P/认沽: {}".format(option_type))


def _side_name(option_type: str) -> str:
    return "call" if _side_code(option_type) == "UP" else "put"


def _empty_frame(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


class SinaOptionDataSource:
    """新浪股指/ETF 期权数据访问器。

    ``http`` 可传入带有 ``get`` 方法的 Session 兼容对象，便于单元测试或
    上层统一网络策略。接口错误会抛出 :class:`SinaOptionError`，不会用空值
    冒充有效的期权行情。
    """

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        http: Optional[Any] = None,
    ) -> None:
        self.config = dict(config or {})
        option_config = self.config.get("sina_options") or self.config.get(
            "option_data", {}
        )
        self.enabled = bool(option_config.get("enabled", True))
        self.timeout = float(option_config.get("timeout_seconds", 20))
        self.quote_batch_size = max(1, int(option_config.get("quote_batch_size", 50)))
        self.session = http or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/131 Safari/537.36"
                    ),
                    "Referer": "https://finance.sina.com.cn/",
                }
            )

    def _get(self, url: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        if not self.enabled:
            raise SinaOptionError("新浪期权数据源已在配置中禁用")
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SinaOptionError("新浪期权请求失败: {}".format(url)) from exc
        except Exception as exc:
            raise SinaOptionError("新浪期权请求异常: {}".format(url)) from exc

        status_code = getattr(response, "status_code", 200)
        if status_code >= 400:
            raise SinaOptionError(
                "新浪期权 HTTP {}: {}".format(status_code, url)
            )
        return response

    @staticmethod
    def _text(response: Any, encodings: Sequence[str]) -> str:
        content = getattr(response, "content", None)
        if isinstance(content, bytes):
            return _decode_bytes(content, encodings)
        text = getattr(response, "text", None)
        if text is not None:
            return str(text)
        raise SinaOptionError("新浪期权响应没有文本内容")

    @staticmethod
    def _json(response: Any) -> Any:
        try:
            return response.json()
        except Exception:
            return _jsonp_value(
                SinaOptionDataSource._text(response, ("utf-8", "gbk", "gb2312"))
            )

    @staticmethod
    def _status_ok(payload: Mapping[str, Any]) -> bool:
        result = payload.get("result", {})
        status = result.get("status", {}) if isinstance(result, Mapping) else {}
        code = status.get("code", 0) if isinstance(status, Mapping) else 0
        return str(code) in {"0", "200"}

    @staticmethod
    def _cffex_product_and_month(
        product: str, contract_month: Optional[str]
    ) -> Tuple[str, str]:
        value = str(product).strip().lower()
        match = re.fullmatch(r"(ho|io|mo)(\d{4})?", value)
        if not match or match.group(1) not in CFFEX_PRODUCTS:
            raise SinaOptionError("不支持的中金所期权产品: {}".format(product))
        product_code = match.group(1)
        month = match.group(2) or (
            _normalize_month(contract_month) if contract_month is not None else None
        )
        if month is None:
            raise SinaOptionError("缺少中金所期权合约月份: {}".format(product))
        return product_code, month

    @staticmethod
    def _etf_underlying(underlying: str) -> Tuple[str, str]:
        code = str(underlying).strip()
        if code not in ETF_OPTION_CATEGORIES:
            raise SinaOptionError("不支持的上交所 ETF 期权标的: {}".format(underlying))
        return code, ETF_OPTION_CATEGORIES[code]

    @staticmethod
    def _normalize_etf_code(option_code: str) -> Tuple[str, str]:
        raw = str(option_code).strip().upper()
        match = re.fullmatch(r"(?:CON_OP_)?(\d+)", raw)
        if not match:
            raise SinaOptionError("无法识别上交所期权合约代码: {}".format(option_code))
        numeric = match.group(1)
        return "CON_OP_" + numeric, numeric

    def fetch_cffex_option_months(self, product: str = "io") -> List[str]:
        """读取中金所产品当前可用的期权合约月份，返回 YYMM。"""
        product_code = str(product).strip().lower()[:2]
        if product_code not in CFFEX_PRODUCTS:
            raise SinaOptionError("不支持的中金所期权产品: {}".format(product))
        url = "{}/{}/cffex".format(SINA_CFFEX_PAGE_URL, product_code)
        response = self._get(url)
        text = self._text(response, ("gb2312", "gbk", "utf-8"))
        pattern = r"data-value\s*=\s*[\"']({}\d{{4}})[\"']".format(product_code)
        months: List[str] = []
        for symbol in re.findall(pattern, text, flags=re.IGNORECASE):
            month = symbol[-4:]
            if month not in months:
                months.append(month)
        if not months:
            raise SinaOptionError("新浪没有返回 {} 的期权合约月份".format(product_code))
        return months

    def fetch_cffex_option_chain(
        self, product: str = "io", contract_month: Optional[str] = None
    ) -> pd.DataFrame:
        """读取中金所某月份的 Call/Put 实时链。"""
        product_code, month = self._cffex_product_and_month(product, contract_month)
        pinzhong = product_code + month
        response = self._get(
            SINA_CFFEX_CHAIN_URL,
            params={
                "type": "futures",
                "product": product_code,
                "exchange": "cffex",
                "pinzhong": pinzhong,
            },
        )
        payload = self._json(response)
        if not isinstance(payload, Mapping) or not self._status_ok(payload):
            raise SinaOptionError("新浪中金所期权链返回错误")
        result = payload.get("result", {})
        data = result.get("data", {}) if isinstance(result, Mapping) else {}
        if not isinstance(data, Mapping):
            raise SinaOptionError("新浪中金所期权链数据格式错误")

        as_of = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows: List[Dict[str, Any]] = []
        for option_type, values in (
            ("C", data.get("up", [])),
            ("P", data.get("down", [])),
        ):
            if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
                continue
            for row in values:
                if not isinstance(row, (list, tuple)):
                    continue
                parsed = self._parse_cffex_chain_row(
                    row, product_code, month, option_type, as_of
                )
                if parsed is not None:
                    rows.append(parsed)
        if not rows:
            raise SinaOptionError("新浪没有返回 {}{} 的期权链".format(product_code, month))
        frame = pd.DataFrame(rows)
        return frame.reindex(columns=COMMON_OPTION_COLUMNS).sort_values(
            ["strike", "option_type"], ignore_index=True
        )

    @staticmethod
    def _parse_cffex_chain_row(
        row: Sequence[Any],
        product: str,
        month: str,
        option_type: str,
        as_of: str,
    ) -> Optional[Dict[str, Any]]:
        if option_type == "C":
            if len(row) < 9:
                return None
            strike = _as_float(row[7])
            symbol = str(row[8]).strip().lower()
        else:
            if len(row) < 8:
                return None
            symbol = str(row[7]).strip().lower()
            match = re.fullmatch(r"(?:ho|io|mo)\d{4}p(\d+(?:\.\d+)?)", symbol)
            strike = _as_float(match.group(1)) if match else None
        if not symbol or strike is None:
            return None
        return {
            "option_code": symbol,
            "exchange": "CFFEX",
            "underlying": CFFEX_PRODUCTS[product],
            "product": product,
            "contract_month": month,
            "option_type": option_type,
            "side": "call" if option_type == "C" else "put",
            "strike": strike,
            "bid_volume": _as_int(row[0]),
            "bid": _as_float(row[1]),
            "last": _as_float(row[2]),
            "ask": _as_float(row[3]),
            "ask_volume": _as_int(row[4]),
            "open_interest": _as_int(row[5]),
            "change_pct": _as_float(row[6]),
            "prev_close": None,
            "open": None,
            "high": None,
            "low": None,
            "volume": None,
            "amount": None,
            "quote_time": None,
            "expiry_date": None,
            "contract_name": None,
            "source": "sina",
            "source_url": SINA_CFFEX_CHAIN_URL,
            "as_of": as_of,
        }

    def fetch_cffex_option_daily(self, option_symbol: str) -> pd.DataFrame:
        """读取中金所单个期权合约历史日线。"""
        symbol = str(option_symbol).strip().lower()
        match = re.fullmatch(r"(ho|io|mo)(\d{4})([cp])(\d+(?:\.\d+)?)", symbol)
        if not match:
            raise SinaOptionError("无法识别中金所期权代码: {}".format(option_symbol))
        response = self._get(SINA_CFFEX_DAILY_URL, params={"symbol": symbol})
        payload = _jsonp_value(self._text(response, ("utf-8", "gbk", "gb2312")))
        return self._daily_frame(
            payload,
            option_code=symbol,
            product=match.group(1),
            contract_month=match.group(2),
            option_type=match.group(3).upper(),
            strike=_as_float(match.group(4)),
            source_url=SINA_CFFEX_DAILY_URL,
        )

    def fetch_etf_option_months(self, underlying: str) -> List[str]:
        """读取上交所 ETF 期权当前可用月份，返回 YYMM 并去重。"""
        _, category = self._etf_underlying(underlying)
        response = self._get(
            SINA_ETF_METADATA_URL,
            params={"exchange": "null", "cate": category},
        )
        payload = self._json(response)
        if not isinstance(payload, Mapping) or not self._status_ok(payload):
            raise SinaOptionError("新浪 ETF 期权月份返回错误")
        result = payload.get("result", {})
        data = result.get("data", {}) if isinstance(result, Mapping) else {}
        months_value = (
            data.get("contractMonth", []) if isinstance(data, Mapping) else []
        )
        if isinstance(months_value, str):
            months_value = [months_value]
        months: List[str] = []
        for value in months_value or []:
            try:
                month = _normalize_month(str(value))
            except SinaOptionError:
                logger.warning("忽略无法识别的新浪 ETF 期权月份: %s", value)
                continue
            if month not in months:
                months.append(month)
        if not months:
            raise SinaOptionError("新浪没有返回 {} 的 ETF 期权月份".format(underlying))
        return months

    def fetch_etf_option_codes(
        self, underlying: str, contract_month: str, option_type: str
    ) -> List[str]:
        """读取上交所 ETF 期权某月份的一侧合约 ID。"""
        code, _ = self._etf_underlying(underlying)
        month = _normalize_month(contract_month)
        side = _side_code(option_type)
        url = SINA_QUOTE_URL.format("OP_{}_{}{}".format(side, code, month))
        response = self._get(url)
        text = self._text(response, ("gbk", "gb2312", "utf-8"))
        codes = re.findall(r"CON_OP_(\d+)", text, flags=re.IGNORECASE)
        if not codes:
            raise SinaOptionError(
                "新浪没有返回 {} {} {} 的 ETF 期权合约".format(
                    underlying, month, _side_name(option_type)
                )
            )
        return ["CON_OP_" + value for value in codes]

    def fetch_etf_option_quote(self, option_code: str) -> Dict[str, Any]:
        """读取上交所 ETF 期权单个合约实时行情。"""
        normalized, _ = self._normalize_etf_code(option_code)
        response = self._get(SINA_QUOTE_URL.format(normalized))
        quotes = self._parse_etf_quote_response(
            self._text(response, ("gbk", "gb2312", "utf-8"))
        )
        quote = quotes.get(normalized)
        if quote is None:
            raise SinaOptionError("新浪没有返回 ETF 期权行情: {}".format(option_code))
        return quote

    def fetch_etf_option_chain(
        self,
        underlying: str,
        contract_month: str,
        option_types: Sequence[str] = ("call", "put"),
    ) -> pd.DataFrame:
        """读取上交所 ETF 期权某月份的 Call/Put 实时链。"""
        code, category = self._etf_underlying(underlying)
        month = _normalize_month(contract_month)
        requested: List[Tuple[str, str]] = []
        for option_type in option_types:
            side = _side_code(option_type)
            if (side, _side_name(option_type)) not in requested:
                requested.append((side, _side_name(option_type)))

        all_codes: List[Tuple[str, str]] = []
        for side, name in requested:
            for option_code in self.fetch_etf_option_codes(code, month, side):
                all_codes.append((option_code, name))
        if not all_codes:
            raise SinaOptionError("新浪没有返回 {}{} 的 ETF 期权链".format(code, month))

        rows: List[Dict[str, Any]] = []
        for offset in range(0, len(all_codes), self.quote_batch_size):
            batch = all_codes[offset : offset + self.quote_batch_size]
            symbols = ",".join(item[0] for item in batch)
            response = self._get(SINA_QUOTE_URL.format(symbols))
            quotes = self._parse_etf_quote_response(
                self._text(response, ("gbk", "gb2312", "utf-8"))
            )
            for option_code, side_name in batch:
                quote = quotes.get(option_code)
                if quote is None:
                    logger.warning("新浪批量行情缺少合约 %s", option_code)
                    continue
                quote["underlying"] = quote.get("underlying") or code
                quote["product"] = quote.get("product") or category
                quote["contract_month"] = month
                quote["side"] = quote.get("side") or side_name
                quote["option_type"] = quote.get("option_type") or (
                    "C" if side_name == "call" else "P"
                )
                rows.append(quote)
        if not rows:
            raise SinaOptionError("新浪没有返回 {}{} 的 ETF 期权行情".format(code, month))
        frame = pd.DataFrame(rows)
        return frame.reindex(columns=COMMON_OPTION_COLUMNS).sort_values(
            ["strike", "option_type"], ignore_index=True
        )

    @staticmethod
    def _parse_etf_quote_response(text: str) -> Dict[str, Dict[str, Any]]:
        pattern = r'var\s+hq_str_(CON_OP_\d+)\s*=\s*"([^"]*)"'
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        quotes: Dict[str, Dict[str, Any]] = {}
        as_of = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for raw_code, raw_value in matches:
            code = raw_code.upper()
            fields = raw_value.split(",")
            option_type = str(fields[45]).strip().upper() if len(fields) > 45 else ""
            if option_type not in {"C", "P"}:
                option_type = None
            quotes[code] = {
                "option_code": code,
                "exchange": "SSE",
                "underlying": fields[36].strip() if len(fields) > 36 else None,
                "product": None,
                "contract_month": None,
                "option_type": option_type,
                "side": (
                    "call"
                    if option_type == "C"
                    else "put"
                    if option_type == "P"
                    else None
                ),
                "strike": _as_float(fields[7]) if len(fields) > 7 else None,
                "bid_volume": _as_int(fields[0]) if len(fields) > 0 else None,
                "bid": _as_float(fields[1]) if len(fields) > 1 else None,
                "last": _as_float(fields[2]) if len(fields) > 2 else None,
                "ask": _as_float(fields[3]) if len(fields) > 3 else None,
                "ask_volume": _as_int(fields[4]) if len(fields) > 4 else None,
                "open_interest": _as_int(fields[5]) if len(fields) > 5 else None,
                "change_pct": _as_float(fields[6]) if len(fields) > 6 else None,
                "prev_close": _as_float(fields[8]) if len(fields) > 8 else None,
                "open": _as_float(fields[9]) if len(fields) > 9 else None,
                "high": _as_float(fields[39]) if len(fields) > 39 else None,
                "low": _as_float(fields[40]) if len(fields) > 40 else None,
                "volume": _as_int(fields[41]) if len(fields) > 41 else None,
                "amount": _as_float(fields[42]) if len(fields) > 42 else None,
                "quote_time": fields[32].strip() if len(fields) > 32 else None,
                "expiry_date": fields[46].strip() if len(fields) > 46 else None,
                "contract_name": fields[37].strip() if len(fields) > 37 else None,
                "source": "sina",
                "source_url": SINA_QUOTE_URL.format(code),
                "as_of": as_of,
            }
        return quotes

    def fetch_etf_option_daily(self, option_code: str) -> pd.DataFrame:
        """读取上交所 ETF 期权单个合约历史日线。"""
        normalized, numeric = self._normalize_etf_code(option_code)
        response = self._get(SINA_ETF_DAILY_URL, params={"symbol": numeric})
        payload = _jsonp_value(self._text(response, ("utf-8", "gbk", "gb2312")))
        return self._daily_frame(
            payload,
            option_code=normalized,
            source_url=SINA_ETF_DAILY_URL,
        )

    @staticmethod
    def _daily_frame(
        payload: Any,
        option_code: str,
        source_url: str,
        product: Optional[str] = None,
        contract_month: Optional[str] = None,
        option_type: Optional[str] = None,
        strike: Optional[float] = None,
    ) -> pd.DataFrame:
        if not isinstance(payload, list):
            raise SinaOptionError("新浪期权日线数据格式错误")
        rows: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, Mapping) or not item.get("d"):
                continue
            rows.append(
                {
                    "date": item.get("d"),
                    "option_code": option_code,
                    "product": product,
                    "contract_month": contract_month,
                    "option_type": option_type,
                    "strike": strike,
                    "open": _as_float(item.get("o")),
                    "high": _as_float(item.get("h")),
                    "low": _as_float(item.get("l")),
                    "close": _as_float(item.get("c")),
                    "volume": _as_int(item.get("v")),
                    "source": "sina",
                    "source_url": source_url,
                }
            )
        frame = pd.DataFrame(rows, columns=DAILY_OPTION_COLUMNS)
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).sort_values("date", ignore_index=True)
        return frame


def resample_option_bars(daily: pd.DataFrame, rule: str = "W-FRI") -> pd.DataFrame:
    """将单个期权合约日线聚合成周/月 OHLCV。

    ``rule`` 支持 ``W-FRI``（或 ``weekly``）和 ``M``（或 ``monthly``）。
    周/月这里指行情 K 线周期，不表示新浪存在周到期或月到期以外的虚拟合约。
    多合约 DataFrame 会按 ``option_code`` 或 ``option_symbol`` 分组合并。
    """
    if not isinstance(daily, pd.DataFrame):
        raise TypeError("daily 必须是 pandas.DataFrame")
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = sorted(required.difference(daily.columns))
    if missing:
        raise ValueError("期权日线缺少字段: {}".format(", ".join(missing)))
    normalized_rule = {
        "weekly": "W-FRI",
        "week": "W-FRI",
        "monthly": "M",
        "month": "M",
    }.get(str(rule).lower(), rule)
    if normalized_rule == "M":
        try:
            pd.tseries.frequencies.to_offset("ME")
        except (AttributeError, ValueError):
            pass
        else:
            normalized_rule = "ME"
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    if frame.empty:
        return daily.iloc[0:0].copy()

    identity_columns = [
        column
        for column in ("option_code", "option_symbol")
        if column in frame.columns
    ]
    if not identity_columns:
        identity_columns = ["__single_contract"]
        frame["__single_contract"] = "option"

    pieces: List[pd.DataFrame] = []
    for identity, group in frame.groupby(identity_columns, dropna=False, sort=False):
        if not isinstance(identity, tuple):
            identity = (identity,)
        bars = (
            group.set_index("date")
            .sort_index()[["open", "high", "low", "close", "volume"]]
            .resample(normalized_rule)
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna(subset=["open", "high", "low", "close"], how="all")
            .reset_index()
        )
        for column, value in zip(identity_columns, identity):
            bars[column] = value
        for column in ("product", "contract_month", "option_type", "strike"):
            if column in group.columns:
                bars[column] = group[column].iloc[0]
        pieces.append(bars)

    result = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    if "__single_contract" in result.columns:
        result = result.drop(columns=["__single_contract"])
    preferred = [
        "date",
        "option_code",
        "option_symbol",
        "product",
        "contract_month",
        "option_type",
        "strike",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    ordered = [column for column in preferred if column in result.columns]
    ordered.extend(column for column in result.columns if column not in ordered)
    return result.reindex(columns=ordered).sort_values(
        [
            column
            for column in ("option_code", "option_symbol", "date")
            if column in result.columns
        ],
        ignore_index=True,
    )


__all__ = [
    "CFFEX_PRODUCTS",
    "ETF_OPTION_CATEGORIES",
    "SinaOptionDataSource",
    "SinaOptionError",
    "resample_option_bars",
]
