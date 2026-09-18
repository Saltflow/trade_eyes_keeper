"""Look-through valuation for the monitored equity ETFs.

The daily report must not treat an ETF's NAV or trading price as a company
multiple.  This adapter instead uses the tracked index's published basket (or
the issuer's disclosed basket) and aggregates constituent earnings/book
yields.  It is deliberately presentation-only: it is not imported by the
backtester, optimizer, or signal engine.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Callable, Dict, Iterable, Optional, Sequence, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


CSI_CLOSE_WEIGHT_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/"
    "autofile/closeweight/{index_code}closeweight.xls"
)
VANGUARD_HOLDINGS_URL = (
    "https://investor.vanguard.com/vmf/api/VOO/portfolio-holding/stock.json"
)
YAHOO_QUOTE_URL = "https://finance.yahoo.com/quote/{ticker}/"
NIKKEI_DATA_URL = "https://indexes.nikkei.co.jp/nkave/archives/data?list={metric}"
NASDAQ100_FALLBACK_URL = "https://chartrow.com/nasdaq-100/pe-ratio"
NIKKEI_FALLBACK_URL = "https://nikkeiyosoku.com/nikkeiper/"

MIN_COVERAGE = 0.80
QQ_BATCH_SIZE = 200


@dataclass(frozen=True)
class ETFValuationReference:
    """A monitored fund's economically meaningful valuation reference."""

    kind: str
    label: str
    index_code: Optional[str] = None
    yahoo_ticker: Optional[str] = None


# The monitored products are deliberately mapped to their stated benchmarks,
# rather than inferred from a similarly named fund.  Commodities and REITs
# remain explicitly not-applicable: enterprise PE/PB would be misleading.
ETF_VALUATION_REFERENCES: Dict[str, ETFValuationReference] = {
    "512810": ETFValuationReference("csi", "中证军工", "399967"),
    "515180": ETFValuationReference("csi", "中证红利", "000922"),
    "588510": ETFValuationReference("csi", "中证科创创业人工智能", "932456"),
    "510300": ETFValuationReference("csi", "沪深300", "000300"),
    "510500": ETFValuationReference("csi", "中证500", "000905"),
    "520650": ETFValuationReference("csi", "中证港股通互联网", "931637"),
    "520810": ETFValuationReference("csi", "中证港股通高股息投资", "930914"),
    "VOO": ETFValuationReference("vanguard_sp500", "标普500"),
    "159655": ETFValuationReference("vanguard_sp500", "标普500"),
    "QQQ": ETFValuationReference("yahoo_fund", "纳斯达克100", yahoo_ticker="QQQ"),
    "513520": ETFValuationReference("nikkei", "日经225"),
    "518660": ETFValuationReference("not_applicable", "黄金现货"),
    "508091": ETFValuationReference("not_applicable", "消费REIT"),
}


def _positive_ratio(value: object, maximum: float) -> Optional[float]:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not pd.notna(number) or number <= 0 or number > maximum:
        return None
    return number


def _quote_symbol(code: object) -> Optional[str]:
    """Turn a CSI/Vanguard constituent identifier into a QQ quote symbol."""
    raw = str(code).strip().upper()
    if raw.endswith(".HK"):
        numeric = raw[:-3].strip().zfill(5)
        return f"hk{numeric}" if numeric.isdigit() else None
    if not raw or not re.fullmatch(r"[A-Z.0-9-]+", raw):
        return None
    if raw.isdigit() and len(raw) <= 6:
        numeric = raw.zfill(6)
        return f"{'sh' if numeric.startswith(('5', '6', '9')) else 'sz'}{numeric}"
    return f"us{raw}"


class ETFValuationResolver:
    """Fetch and aggregate constituent PE/PB for daily-report ETFs only."""

    def __init__(
        self,
        timeout: int = 30,
        user_agent: Optional[str] = None,
        http_get: Callable[..., requests.Response] = requests.get,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent or "Mozilla/5.0 (TradeEyesKeeper ETF valuation)"
        self._http_get = http_get
        self._cache: Dict[str, Optional[Dict[str, Optional[float]]]] = {}

    @staticmethod
    def supports(code: object) -> bool:
        return str(code).strip().upper() in ETF_VALUATION_REFERENCES

    def resolve(self, code: object) -> Optional[Dict[str, Optional[float]]]:
        normalized = str(code).strip().upper()
        if normalized in self._cache:
            return self._cache[normalized]

        reference = ETF_VALUATION_REFERENCES.get(normalized)
        if reference is None or reference.kind == "not_applicable":
            self._cache[normalized] = None
            return None

        try:
            if reference.kind == "csi" and reference.index_code:
                result = self._resolve_csi(reference)
            elif reference.kind == "vanguard_sp500":
                result = self._resolve_vanguard_sp500(reference)
            elif reference.kind == "yahoo_fund" and reference.yahoo_ticker:
                result = self._resolve_yahoo_fund(reference)
            elif reference.kind == "nikkei":
                result = self._resolve_nikkei(reference)
            else:
                result = None
        except Exception as exc:
            logger.warning("ETF %s %s 估值补全失败: %s", normalized, reference.label, exc)
            result = None

        self._cache[normalized] = result
        return result

    def _get(self, url: str, **kwargs: object) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("User-Agent", self.user_agent)
        response = self._http_get(
            url,
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )
        response.raise_for_status()
        return response

    def _resolve_csi(
        self, reference: ETFValuationReference
    ) -> Optional[Dict[str, Optional[float]]]:
        response = self._get(
            CSI_CLOSE_WEIGHT_URL.format(index_code=reference.index_code)
        )
        frame = pd.read_excel(BytesIO(response.content))
        if frame.shape[1] < 10 or frame.empty:
            raise ValueError("中证成分权重文件为空或列结构不完整")
        pairs = self._weight_pairs(frame.iloc[:, 4], frame.iloc[:, -1])
        return self._aggregate_pairs(pairs, f"中证官方成分权重 · {reference.label}")

    def _resolve_vanguard_sp500(
        self, reference: ETFValuationReference
    ) -> Optional[Dict[str, Optional[float]]]:
        entities = []
        for suffix in ("", "?start=501&count=500"):
            payload = self._get(VANGUARD_HOLDINGS_URL + suffix).json()
            entities.extend((payload.get("fund") or {}).get("entity") or [])
        pairs = self._weight_pairs(
            (item.get("ticker") for item in entities),
            (item.get("percentWeight") for item in entities),
            percent_weights=True,
        )
        return self._aggregate_pairs(pairs, f"Vanguard官方成分股 · {reference.label}")

    def _resolve_yahoo_fund(
        self, reference: ETFValuationReference
    ) -> Optional[Dict[str, Optional[float]]]:
        try:
            response = self._get(
                YAHOO_QUOTE_URL.format(ticker=reference.yahoo_ticker)
            )
            soup = BeautifulSoup(response.text, "html.parser")
            result = None
            for tag in soup.find_all("script", type="application/json"):
                text = tag.string or ""
                if "topHoldings" not in text:
                    continue
                outer = json.loads(text)
                body = outer.get("body")
                if not isinstance(body, str):
                    continue
                candidate = json.loads(body).get("quoteSummary", {}).get("result", [])
                if candidate:
                    result = candidate[0]
                    break
            if not isinstance(result, dict):
                raise ValueError("Yahoo 未返回 ETF 成分估值")

            holdings = result.get("topHoldings", {}).get("equityHoldings", {})
            earnings_yield = _positive_ratio(
                (holdings.get("priceToEarnings") or {}).get("raw"), 1.0
            )
            book_yield = _positive_ratio(
                (holdings.get("priceToBook") or {}).get("raw"), 1.0
            )
            trailing_pe = _positive_ratio(
                (result.get("summaryDetail", {}).get("trailingPE") or {}).get(
                    "raw"
                ),
                1000.0,
            )
            pe = trailing_pe or (1.0 / earnings_yield if earnings_yield else None)
            pb = 1.0 / book_yield if book_yield else None
            return self._result(pe, pb, f"公开基金成分股聚合 · {reference.label}")
        except Exception as exc:
            logger.info("Yahoo QQQ 估值不可用，改用Nasdaq-100公开汇总: %s", exc)
            return self._resolve_nasdaq100_fallback(reference)

    def _resolve_nasdaq100_fallback(
        self, reference: ETFValuationReference
    ) -> Optional[Dict[str, Optional[float]]]:
        response = self._get(NASDAQ100_FALLBACK_URL)
        text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
        pe_match = re.search(r"Nasdaq-100 P/E ratio:\s*(\d+(?:\.\d+)?)", text)
        pb_match = re.search(r"Price/Book\s*(\d+(?:\.\d+)?)", text)
        pe = _positive_ratio(pe_match.group(1) if pe_match else None, 1000.0)
        pb = _positive_ratio(pb_match.group(1) if pb_match else None, 100.0)
        return self._result(pe, pb, f"Nasdaq-100公开汇总 · {reference.label}")

    def _resolve_nikkei(
        self, reference: ETFValuationReference
    ) -> Optional[Dict[str, Optional[float]]]:
        try:
            pe = self._nikkei_latest("per")
            pb = self._nikkei_latest("pbr")
            return self._result(pe, pb, f"日经官方指数统计 · {reference.label}")
        except Exception as exc:
            logger.info("日经官方指数统计不可用，改用公开日度表: %s", exc)
            return self._resolve_nikkei_fallback(reference)

    def _resolve_nikkei_fallback(
        self, reference: ETFValuationReference
    ) -> Optional[Dict[str, Optional[float]]]:
        response = self._get(NIKKEI_FALLBACK_URL)
        # The provider's historical rows use malformed ``<trclass>`` opening
        # tags and normal ``</tr>`` closers.  Parse those source fragments
        # explicitly instead of relying on an HTML parser to repair them.
        fragments = re.findall(
            r"<trclass[^>]*>(.*?)(?:</tr>|</trclass>)",
            response.text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for fragment in fragments:
            row = BeautifulSoup(fragment, "html.parser")
            cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
            if len(cells) < 5:
                continue
            pe = _positive_ratio(cells[3], 1000.0)
            pb = _positive_ratio(cells[4], 100.0)
            if pe is not None and pb is not None:
                return self._result(pe, pb, f"日经公开日度表 · {reference.label}")
        raise ValueError("日经公开日度表没有可用 PER/PBR 行")

    def _nikkei_latest(self, metric: str) -> Optional[float]:
        response = self._get(NIKKEI_DATA_URL.format(metric=metric))
        soup = BeautifulSoup(response.text, "html.parser")
        rows = []
        for row in soup.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
            if len(cells) >= 2 and re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", cells[0]):
                rows.append(cells)
        if not rows:
            raise ValueError(f"日经 {metric} 页面没有可用日期行")
        return _positive_ratio(rows[-1][1], 1000.0)

    def _weight_pairs(
        self,
        codes: Iterable[object],
        weights: Iterable[object],
        *,
        percent_weights: bool = False,
    ) -> Sequence[Tuple[str, float]]:
        pairs = []
        for code, raw_weight in zip(codes, weights):
            symbol = _quote_symbol(code)
            try:
                weight = float(raw_weight)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if percent_weights:
                weight /= 100.0
            if symbol and pd.notna(weight) and weight > 0:
                pairs.append((symbol, weight))
        if not pairs:
            raise ValueError("成分股或权重为空")
        return pairs

    def _aggregate_pairs(
        self, pairs: Sequence[Tuple[str, float]], source: str
    ) -> Optional[Dict[str, Optional[float]]]:
        quotes = self._qq_valuations([symbol for symbol, _ in pairs])
        pe, pe_coverage = self._weighted_harmonic(pairs, quotes, "pe_ratio")
        pb, pb_coverage = self._weighted_harmonic(pairs, quotes, "pb_ratio")
        result = self._result(pe, pb, source)
        if result is not None:
            logger.info(
                "%s ETF估值补全: PE=%s (覆盖%.1f%%), PB=%s (覆盖%.1f%%)",
                source,
                f"{pe:.2f}" if pe is not None else "—",
                pe_coverage * 100.0,
                f"{pb:.2f}" if pb is not None else "—",
                pb_coverage * 100.0,
            )
        return result

    def _qq_valuations(self, symbols: Sequence[str]) -> Dict[str, Dict[str, float]]:
        result: Dict[str, Dict[str, float]] = {}
        for start in range(0, len(symbols), QQ_BATCH_SIZE):
            response = self._get(
                "http://qt.gtimg.cn/q=" + ",".join(symbols[start : start + QQ_BATCH_SIZE])
            )
            for line in response.text.splitlines():
                match = re.match(r'v_([^=]+)="(.*)";', line)
                if match is None:
                    continue
                symbol, raw = match.groups()
                fields = raw.split("~")
                pe = _positive_ratio(fields[39] if len(fields) > 39 else None, 1000.0)
                pb_index = 58 if symbol.startswith("hk") else 51 if symbol.startswith("us") else 46
                pb = _positive_ratio(
                    fields[pb_index] if len(fields) > pb_index else None, 100.0
                )
                result[symbol] = {
                    key: value
                    for key, value in (("pe_ratio", pe), ("pb_ratio", pb))
                    if value is not None
                }
        return result

    @staticmethod
    def _weighted_harmonic(
        pairs: Sequence[Tuple[str, float]],
        quotes: Dict[str, Dict[str, float]],
        field: str,
    ) -> Tuple[Optional[float], float]:
        total_weight = sum(weight for _, weight in pairs)
        eligible = [
            (weight, quotes.get(symbol, {}).get(field))
            for symbol, weight in pairs
            if quotes.get(symbol, {}).get(field) is not None
        ]
        covered_weight = sum(weight for weight, _ in eligible)
        coverage = covered_weight / total_weight if total_weight > 0 else 0.0
        if coverage < MIN_COVERAGE or not eligible:
            return None, coverage
        denominator = sum(weight / value for weight, value in eligible if value)
        return (covered_weight / denominator if denominator > 0 else None), coverage

    @staticmethod
    def _result(
        pe: Optional[float], pb: Optional[float], source: str
    ) -> Optional[Dict[str, Optional[float]]]:
        # ETF display is a paired valuation contract: presenting PE without
        # its matching PB is less interpretable than an explicit no-data row.
        if pe is None or pb is None:
            return None
        return {
            "pe_ratio": pe,
            "pb_ratio": pb,
            "valuation_source": source,  # type: ignore[dict-item]
        }
