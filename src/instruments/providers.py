"""Real-data providers for instrument audits.

Official statements take priority when they are available.  Yahoo and QQ are
explicitly secondary sources used to improve current-report coverage; their
snapshots are never injected into historical strategy inputs.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import requests

from .classifier import detect_market, normalize_yahoo_symbol
from .models import (
    FinancialStatementSnapshot,
    FundHolding,
    FundProfile,
    MetricStatus,
    MetricValue,
)

logger = logging.getLogger(__name__)


def _float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        value = value.get("raw", value.get("value"))
    try:
        number = float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return number


def _date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _weight(value: Any) -> float:
    number = _float(value)
    if number is None or number < 0:
        return 0.0
    return number / 100.0 if number > 1.0 else number


@dataclass
class ProviderPayload:
    metadata: dict[str, Any] = field(default_factory=dict)
    price: Optional[MetricValue] = None
    quoted_pe: Optional[MetricValue] = None
    quoted_pb: Optional[MetricValue] = None
    market_cap: Optional[MetricValue] = None
    free_float_market_cap: Optional[MetricValue] = None
    ttm_dividend: Optional[MetricValue] = None
    latest_dividend: Optional[MetricValue] = None
    statements: list[FinancialStatementSnapshot] = field(default_factory=list)
    fund: Optional[FundProfile] = None
    attempts: list[dict[str, Any]] = field(default_factory=list)


class HttpProvider:
    def __init__(
        self,
        config: dict,
        http: requests.Session | None = None,
    ):
        self.config = config
        self.http = http or requests.Session()
        audit_config = config.get("instrument_audit", {}) or {}
        self.timeout = int(audit_config.get("timeout_seconds", 20))
        self.user_agent = audit_config.get(
            "user_agent",
            "Mozilla/5.0 (compatible; trade-eyes-keeper instrument audit)",
        )

    def _get(self, url: str, **kwargs) -> requests.Response:
        headers = {"User-Agent": self.user_agent}
        headers.update(kwargs.pop("headers", {}) or {})
        response = self.http.get(
            url,
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )
        response.raise_for_status()
        return response


class YahooFinanceProvider(HttpProvider):
    SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
    CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    TIMESERIES_URL = (
        "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/"
        "v1/finance/timeseries/{symbol}"
    )
    TIMESERIES_TYPES = (
        "quarterlyTotalRevenue",
        "quarterlyNetIncomeCommonStockholders",
        "quarterlyNetIncome",
        "quarterlyStockholdersEquity",
        "quarterlyDilutedAverageShares",
        "quarterlyOrdinarySharesNumber",
        "quarterlyBasicEPS",
        "quarterlyDilutedEPS",
        "quarterlyOperatingCashFlow",
        "quarterlyFreeCashFlow",
        "annualTotalRevenue",
        "annualNetIncomeCommonStockholders",
        "annualNetIncome",
        "annualStockholdersEquity",
        "annualDilutedAverageShares",
        "annualOrdinarySharesNumber",
        "annualBasicEPS",
        "annualDilutedEPS",
        "annualOperatingCashFlow",
        "annualFreeCashFlow",
    )
    FIELD_MAP = {
        "TotalRevenue": "revenue",
        "NetIncomeCommonStockholders": "net_income_parent",
        "NetIncome": "net_income_parent",
        "StockholdersEquity": "parent_equity",
        "DilutedAverageShares": "diluted_average_shares",
        "OrdinarySharesNumber": "common_shares_outstanding",
        "BasicEPS": "basic_eps",
        "DilutedEPS": "diluted_eps",
        "OperatingCashFlow": "operating_cash_flow",
        "FreeCashFlow": "free_cash_flow",
    }

    def fetch(self, code: str, evaluation_date: date) -> ProviderPayload:
        symbol = normalize_yahoo_symbol(code)
        payload = ProviderPayload()
        self._fetch_metadata(symbol, payload)
        self._fetch_chart(symbol, evaluation_date, payload)
        self._fetch_statements(symbol, evaluation_date, payload)
        return payload

    def _fetch_metadata(self, symbol: str, payload: ProviderPayload) -> None:
        try:
            response = self._get(self.SEARCH_URL, params={"q": symbol})
            quotes = response.json().get("quotes", [])
            exact = next(
                (
                    quote
                    for quote in quotes
                    if str(quote.get("symbol", "")).upper() == symbol.upper()
                ),
                quotes[0] if quotes else {},
            )
            payload.metadata.update(
                {
                    "name": exact.get("longname") or exact.get("shortname"),
                    "quote_type": exact.get("quoteType"),
                    "exchange": exact.get("exchange")
                    or exact.get("exchDisp"),
                    "sector": exact.get("sector"),
                    "industry": exact.get("industry"),
                }
            )
            payload.attempts.append(
                {"source": "yahoo_search", "status": "success"}
            )
        except Exception as exc:
            payload.attempts.append(
                {
                    "source": "yahoo_search",
                    "status": "failed",
                    "reason": str(exc),
                }
            )

    def _fetch_chart(
        self,
        symbol: str,
        evaluation_date: date,
        payload: ProviderPayload,
    ) -> None:
        period1 = int(
            datetime.combine(
                evaluation_date - timedelta(days=400),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).timestamp()
        )
        period2 = int(
            datetime.combine(
                evaluation_date + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).timestamp()
        )
        try:
            response = self._get(
                self.CHART_URL.format(symbol=symbol),
                params={
                    "period1": period1,
                    "period2": period2,
                    "interval": "1d",
                    "events": "div",
                },
            )
            result = response.json().get("chart", {}).get("result", [])
            if not result:
                raise ValueError("empty chart result")
            chart = result[0]
            meta = chart.get("meta", {})
            payload.metadata.update(
                {
                    key: value
                    for key, value in {
                        "name": meta.get("longName") or meta.get("shortName"),
                        "exchange": meta.get("exchangeName"),
                        "currency": meta.get("currency"),
                        "quote_type": meta.get("instrumentType"),
                    }.items()
                    if value
                }
            )
            price = _float(meta.get("regularMarketPrice"))
            price_date = None
            regular_time = meta.get("regularMarketTime")
            if regular_time:
                price_date = datetime.fromtimestamp(
                    regular_time, tz=timezone.utc
                ).date()
            if price is not None:
                payload.price = MetricValue(
                    value=price,
                    status=MetricStatus.OBSERVED,
                    as_of=price_date or evaluation_date,
                    source="yahoo_chart",
                    source_url=response.url,
                    currency=meta.get("currency"),
                )

            events = chart.get("events", {}).get("dividends", {}) or {}
            cutoff = evaluation_date - timedelta(days=365)
            dividends: list[tuple[date, float]] = []
            for event in events.values():
                event_date = _date(event.get("date"))
                if event_date is None and event.get("date"):
                    event_date = datetime.fromtimestamp(
                        int(event["date"]), tz=timezone.utc
                    ).date()
                amount = _float(event.get("amount"))
                if (
                    event_date
                    and amount is not None
                    and cutoff <= event_date <= evaluation_date
                ):
                    dividends.append((event_date, amount))
            total = sum(amount for _, amount in dividends)
            payload.ttm_dividend = MetricValue(
                value=total,
                status=(
                    MetricStatus.OBSERVED
                    if total > 0
                    else MetricStatus.KNOWN_ZERO
                ),
                as_of=evaluation_date,
                period="TTM",
                source="yahoo_chart_dividends",
                source_url=response.url,
                currency=meta.get("currency"),
            )
            if dividends:
                latest_date, latest_amount = max(dividends, key=lambda item: item[0])
                payload.latest_dividend = MetricValue(
                    value=latest_amount,
                    status=MetricStatus.OBSERVED,
                    as_of=latest_date,
                    source="yahoo_chart_dividends",
                    source_url=response.url,
                    currency=meta.get("currency"),
                )
            else:
                payload.latest_dividend = MetricValue(
                    value=0.0,
                    status=MetricStatus.KNOWN_ZERO,
                    as_of=evaluation_date,
                    source="yahoo_chart_dividends",
                    source_url=response.url,
                    currency=meta.get("currency"),
                )
            payload.attempts.append(
                {"source": "yahoo_chart", "status": "success"}
            )
        except Exception as exc:
            payload.attempts.append(
                {
                    "source": "yahoo_chart",
                    "status": "failed",
                    "reason": str(exc),
                }
            )

    def _fetch_statements(
        self,
        symbol: str,
        evaluation_date: date,
        payload: ProviderPayload,
    ) -> None:
        period1 = int(
            datetime.combine(
                evaluation_date - timedelta(days=365 * 6),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).timestamp()
        )
        period2 = int(
            datetime.combine(
                evaluation_date + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).timestamp()
        )
        try:
            response = self._get(
                self.TIMESERIES_URL.format(symbol=symbol),
                params={
                    "symbol": symbol,
                    "type": ",".join(self.TIMESERIES_TYPES),
                    "period1": period1,
                    "period2": period2,
                },
            )
            results = response.json().get("timeseries", {}).get("result", [])
            records: dict[tuple[date, str], dict[str, Any]] = {}
            for series in results:
                type_names = series.get("meta", {}).get("type", [])
                if not type_names:
                    continue
                type_name = str(type_names[0])
                frequency = (
                    "annual" if type_name.startswith("annual") else "quarterly"
                )
                metric_name = type_name.removeprefix(frequency)
                target_field = self.FIELD_MAP.get(metric_name)
                if not target_field:
                    continue
                for observation in series.get(type_name, []) or []:
                    period_end = _date(observation.get("asOfDate"))
                    if period_end is None or period_end > evaluation_date:
                        continue
                    key = (period_end, frequency)
                    record = records.setdefault(
                        key,
                        {
                            "period_end": period_end,
                            "published_at": None,
                            "period_type": (
                                "year" if frequency == "annual" else "quarter"
                            ),
                            "is_cumulative": False,
                            "currency": observation.get("currencyCode"),
                            "source": "yahoo_fundamentals_timeseries",
                            "source_url": response.url,
                            "diagnostics": [
                                "publication_date_unavailable_current_audit_only"
                            ],
                        },
                    )
                    value = _float(observation.get("reportedValue"))
                    if value is not None and record.get(target_field) is None:
                        record[target_field] = value
            payload.statements.extend(
                FinancialStatementSnapshot(**record)
                for record in records.values()
                if any(
                    record.get(field)
                    for field in self.FIELD_MAP.values()
                )
            )
            payload.attempts.append(
                {
                    "source": "yahoo_fundamentals_timeseries",
                    "status": (
                        "success" if payload.statements else "empty"
                    ),
                }
            )
        except Exception as exc:
            payload.attempts.append(
                {
                    "source": "yahoo_fundamentals_timeseries",
                    "status": "failed",
                    "reason": str(exc),
                }
            )


class QQQuoteProvider(HttpProvider):
    URL = "http://qt.gtimg.cn/q={symbol}"

    def fetch(self, code: str, evaluation_date: date) -> ProviderPayload:
        market = detect_market(code)
        normalized = str(code).upper().strip()
        if market == "a_share":
            prefix = "sh" if normalized.startswith(("5", "6", "9")) else "sz"
            symbol = f"{prefix}{normalized}"
            currency = "CNY"
        elif market == "hk":
            symbol = f"hk{normalized.zfill(5)}"
            currency = "HKD"
        else:
            symbol = f"us{normalized}"
            currency = "USD"
        payload = ProviderPayload()
        try:
            response = self._get(self.URL.format(symbol=symbol))
            content = response.text
            if "=" not in content:
                raise ValueError("invalid QQ quote response")
            items = content.split("=", 1)[1].strip('";').split("~")
            if len(items) < 47:
                raise ValueError(f"short QQ quote response ({len(items)} fields)")
            payload.metadata["name"] = items[1] or None
            price = _float(items[3])
            if price is not None:
                payload.price = MetricValue(
                    value=price,
                    status=MetricStatus.OBSERVED,
                    as_of=evaluation_date,
                    source="qq_realtime",
                    source_url=response.url,
                    currency=currency,
                )
            pe = _float(items[39])
            if pe is not None and 0 < pe <= 1000:
                payload.quoted_pe = MetricValue(
                    value=pe,
                    status=MetricStatus.OBSERVED,
                    as_of=evaluation_date,
                    source="qq_realtime",
                    source_url=response.url,
                )
            # QQ field 46 is PB only for A shares.  In HK/US payloads it is
            # commonly the English company name.
            pb = _float(items[46]) if market == "a_share" else None
            if pb is not None and 0 < pb <= 50:
                payload.quoted_pb = MetricValue(
                    value=pb,
                    status=MetricStatus.OBSERVED,
                    as_of=evaluation_date,
                    source="qq_realtime",
                    source_url=response.url,
                )
            free_float_cap = _float(items[44])
            total_cap = _float(items[45])
            if free_float_cap is not None:
                payload.free_float_market_cap = MetricValue(
                    value=free_float_cap * 100_000_000,
                    status=MetricStatus.OBSERVED,
                    as_of=evaluation_date,
                    source="qq_realtime_field_44",
                    source_url=response.url,
                    currency=currency,
                    note="QQ市值字段，按亿元换算；仅公司画像使用",
                )
            if total_cap is not None:
                payload.market_cap = MetricValue(
                    value=total_cap * 100_000_000,
                    status=MetricStatus.OBSERVED,
                    as_of=evaluation_date,
                    source="qq_realtime_field_45",
                    source_url=response.url,
                    currency=currency,
                    note="QQ总市值字段，按亿元换算；仅公司画像使用",
                )
            payload.attempts.append({"source": "qq_realtime", "status": "success"})
        except Exception as exc:
            payload.attempts.append(
                {
                    "source": "qq_realtime",
                    "status": "failed",
                    "reason": str(exc),
                }
            )
        return payload


class SecCompanyFactsProvider(HttpProvider):
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    TAGS = {
        "Revenues": "revenue",
        "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
        "NetIncomeLossAvailableToCommonStockholdersBasic": "net_income_parent",
        "NetIncomeLoss": "net_income_parent",
        "StockholdersEquity": "parent_equity",
        "CommonStockSharesOutstanding": "common_shares_outstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding": "diluted_average_shares",
        "EarningsPerShareBasic": "basic_eps",
        "EarningsPerShareDiluted": "diluted_eps",
        "NetCashProvidedByUsedInOperatingActivities": "operating_cash_flow",
        "PaymentsToAcquirePropertyPlantAndEquipment": (
            "capital_expenditures"
        ),
    }

    def __init__(self, config: dict, http: requests.Session | None = None):
        super().__init__(config, http=http)
        email_sender = os.getenv("EMAIL_SENDER", "").strip()
        email_user_agent = (
            f"trade-eyes-keeper/1.0 {email_sender}"
            if email_sender
            else ""
        )
        self.sec_user_agent = (
            os.getenv("SEC_USER_AGENT")
            or (config.get("instrument_audit", {}) or {}).get("sec_user_agent")
            or email_user_agent
            or ""
        ).strip()
        self._tickers: Optional[dict[str, str]] = None

    def _sec_get(self, url: str) -> requests.Response:
        if not self.sec_user_agent:
            raise RuntimeError(
                "SEC User-Agent unavailable; configure SEC_USER_AGENT, "
                "instrument_audit.sec_user_agent, or EMAIL_SENDER "
                "(for example: app-name admin@example.com)"
            )
        return self._get(
            url,
            headers={
                "User-Agent": self.sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
        )

    def _load_tickers(self) -> dict[str, str]:
        if self._tickers is not None:
            return self._tickers
        response = self._sec_get(self.TICKERS_URL)
        mapping = {}
        for row in response.json().values():
            ticker = str(row.get("ticker", "")).upper()
            cik = str(row.get("cik_str", "")).zfill(10)
            if ticker and cik:
                mapping[ticker] = cik
        self._tickers = mapping
        return mapping

    @staticmethod
    def _pick_unit(units: dict[str, list[dict]]) -> list[dict]:
        for unit in ("USD", "shares", "USD/shares"):
            if unit in units:
                return units[unit]
        return next(iter(units.values()), [])

    def fetch(self, code: str, evaluation_date: date) -> ProviderPayload:
        payload = ProviderPayload()
        ticker = str(code).upper().replace(".", "-")
        try:
            cik = self._load_tickers().get(ticker)
            if not cik:
                raise ValueError(f"CIK not found for {ticker}")
            response = self._sec_get(self.FACTS_URL.format(cik=cik))
            facts = response.json()
            payload.metadata.update(
                {
                    "name": facts.get("entityName"),
                    "exchange": "SEC",
                    "currency": "USD",
                }
            )
            records: dict[tuple[date, date, str], dict[str, Any]] = {}
            us_gaap = facts.get("facts", {}).get("us-gaap", {})
            for tag, target_field in self.TAGS.items():
                fact = us_gaap.get(tag)
                if not fact:
                    continue
                for observation in self._pick_unit(fact.get("units", {})):
                    form = observation.get("form")
                    if form not in {"10-Q", "10-K"}:
                        continue
                    period_end = _date(observation.get("end"))
                    filed = _date(observation.get("filed"))
                    value = _float(observation.get("val"))
                    if (
                        period_end is None
                        or filed is None
                        or value is None
                        or filed > evaluation_date
                    ):
                        continue
                    frame = str(observation.get("frame", ""))
                    if form == "10-Q" and frame and not frame.endswith(
                        (
                            "Q1",
                            "Q2",
                            "Q3",
                            "Q4",
                            "Q1I",
                            "Q2I",
                            "Q3I",
                            "Q4I",
                        ),
                    ):
                        continue
                    key = (period_end, filed, form)
                    record = records.setdefault(
                        key,
                        {
                            "period_end": period_end,
                            "published_at": filed,
                            "period_type": "year" if form == "10-K" else "quarter",
                            "is_cumulative": True,
                            "currency": "USD",
                            "accounting_standard": "US-GAAP",
                            "source": "sec_companyfacts",
                            "source_url": response.url,
                        },
                    )
                    start = _date(observation.get("start"))
                    duration_days = (
                        (period_end - start).days
                        if start is not None and start <= period_end
                        else None
                    )
                    field_durations = record.setdefault(
                        "_field_durations", {}
                    )
                    previous_duration = field_durations.get(target_field)
                    prefer_shortest = (
                        form == "10-Q"
                        and target_field
                        in {
                            "basic_eps",
                            "diluted_eps",
                            "diluted_average_shares",
                        }
                    )
                    replace_value = record.get(target_field) is None
                    if not replace_value and duration_days is not None:
                        replace_value = previous_duration is None or (
                            duration_days < previous_duration
                            if prefer_shortest
                            else duration_days > previous_duration
                        )
                    if replace_value:
                        record[target_field] = value
                        field_durations[target_field] = duration_days
            # Company Facts repeats prior-period comparatives in later filings.
            # Keep only the current period of each filing, while preserving the
            # original filing for every historical period.
            filing_ends: dict[tuple[date, str], date] = {}
            for (_, filed, form), record in records.items():
                filing_key = (filed, form)
                previous_end = filing_ends.get(filing_key)
                if previous_end is None or record["period_end"] > previous_end:
                    filing_ends[filing_key] = record["period_end"]

            current_records = []
            for (_, filed, form), record in records.items():
                if record["period_end"] != filing_ends[(filed, form)]:
                    continue
                record.pop("_field_durations", None)
                diagnostics = record.setdefault("diagnostics", [])
                if record["period_type"] == "quarter":
                    diagnostics.append("sec_flow_values_are_fiscal_ytd")
                if (
                    record.get("operating_cash_flow") is not None
                    and record.get("capital_expenditures") is not None
                ):
                    record["free_cash_flow"] = (
                        record["operating_cash_flow"]
                        - record["capital_expenditures"]
                    )
                    diagnostics.append(
                        "free_cash_flow=operating_cash_flow-capital_expenditures"
                    )
                current_records.append(record)

            payload.statements.extend(
                FinancialStatementSnapshot(**record)
                for record in sorted(
                    current_records,
                    key=lambda record: (
                        record["period_end"],
                        record["published_at"],
                        record["period_type"],
                    ),
                )
            )
            payload.attempts.append(
                {
                    "source": "sec_companyfacts",
                    "status": "success" if payload.statements else "empty",
                }
            )
        except Exception as exc:
            payload.attempts.append(
                {
                    "source": "sec_companyfacts",
                    "status": "failed",
                    "reason": str(exc),
                }
            )
        return payload


class ConfiguredFundProvider(HttpProvider):
    """Official fund facts/holdings declared in a code-keyed catalog.

    Issuers expose incompatible endpoints.  The catalog keeps those URLs at
    the data boundary instead of hard-coding portfolio symbols in Python.
    """

    def fetch(
        self,
        code: str,
        evaluation_date: date,
    ) -> ProviderPayload:
        catalog = self.config.get("instrument_catalog", {}) or {}
        raw = catalog.get(str(code), {}) or {}
        payload = ProviderPayload()
        if not raw:
            payload.attempts.append(
                {
                    "source": "configured_official_fund",
                    "status": "missing",
                    "reason": "no instrument_catalog entry",
                }
            )
            return payload
        payload.metadata.update(
            {
                key: raw.get(key)
                for key in (
                    "name",
                    "exchange",
                    "currency",
                    "instrument_type",
                    "asset_class",
                    "sector",
                    "industry",
                )
                if raw.get(key) is not None
            }
        )
        fund = FundProfile(
            issuer=raw.get("issuer"),
            tracking_index=raw.get("tracking_index"),
            asset_class=raw.get("asset_class"),
            region_exposure=raw.get("region_exposure"),
            sector_exposure=raw.get("sector_exposure"),
            property_type=raw.get("property_type"),
            holdings_as_of=_date(raw.get("holdings_as_of")),
        )
        metric_names = (
            "aum",
            "expense_ratio",
            "nav_per_unit",
            "ttm_dividend_per_unit",
            "tracking_difference",
            "duration",
            "yield_to_maturity",
            "ttm_ffo",
            "ffo_per_unit",
            "occupancy_rate",
        )
        for name in metric_names:
            value = raw.get(name)
            if value is None:
                continue
            if isinstance(value, dict):
                metric = MetricValue(**value)
            else:
                metric = MetricValue(
                    value=_float(value),
                    status=MetricStatus.OBSERVED,
                    as_of=evaluation_date,
                    source="configured_official_fund",
                )
            if metric.value is not None and metric.status == MetricStatus.MISSING:
                metric.status = MetricStatus.OBSERVED
            setattr(fund, name, metric)

        holdings = raw.get("top_holdings", []) or []
        if raw.get("holdings_url"):
            try:
                holdings = self._fetch_holdings_csv(raw)
                payload.attempts.append(
                    {
                        "source": "configured_holdings_url",
                        "status": "success",
                    }
                )
            except Exception as exc:
                payload.attempts.append(
                    {
                        "source": "configured_holdings_url",
                        "status": "failed",
                        "reason": str(exc),
                    }
                )
        fund.top_holdings = [
            FundHolding(
                code=str(item.get("code") or item.get("symbol") or "").strip(),
                name=item.get("name"),
                market=item.get("market"),
                currency=item.get("currency"),
                weight=_weight(item.get("weight", 0)),
                as_of=_date(item.get("as_of") or raw.get("holdings_as_of")),
                source=item.get("source") or "configured_official_fund",
            )
            for item in holdings
            if (item.get("code") or item.get("symbol"))
            and _float(item.get("weight")) is not None
        ]
        payload.fund = fund
        payload.attempts.append(
            {"source": "configured_official_fund", "status": "success"}
        )
        return payload

    def _fetch_holdings_csv(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        response = self._get(str(raw["holdings_url"]))
        reader = csv.DictReader(io.StringIO(response.text))
        mapping = raw.get("holdings_columns", {}) or {}
        code_key = mapping.get("code", "code")
        name_key = mapping.get("name", "name")
        weight_key = mapping.get("weight", "weight")
        result = []
        for row in reader:
            code = row.get(code_key)
            weight = _float(row.get(weight_key))
            if not code or weight is None:
                continue
            if weight > 1:
                weight /= 100.0
            result.append(
                {
                    "code": code,
                    "name": row.get(name_key),
                    "weight": weight,
                    "as_of": raw.get("holdings_as_of"),
                    "source": raw.get("holdings_source")
                    or "configured_holdings_url",
                }
            )
        return result


def merge_metric(
    preferred: MetricValue | None,
    fallback: MetricValue | None,
    *,
    conflict_tolerance: float = 0.05,
) -> MetricValue:
    if preferred is None or preferred.value is None:
        return fallback or MetricValue()
    if fallback is None or fallback.value is None:
        return preferred
    result = preferred.copy(deep=True)
    denominator = max(abs(preferred.value), abs(fallback.value), 1e-12)
    relative_gap = abs(preferred.value - fallback.value) / denominator
    if relative_gap > conflict_tolerance:
        result.status = MetricStatus.CONFLICT
        result.alternatives.append(
            {
                "value": fallback.value,
                "source": fallback.source,
                "as_of": fallback.as_of.isoformat() if fallback.as_of else None,
                "relative_gap": relative_gap,
            }
        )
    return result
