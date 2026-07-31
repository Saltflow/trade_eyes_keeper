"""Full-universe instrument profile audit and report rendering."""

from __future__ import annotations

import html
import json
import logging
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .calculations import derive_company_fundamentals, derive_fund_profile
from .classifier import (
    applicable_metrics,
    classify_instrument,
    detect_market,
)
from .models import (
    CompanyFundamentals,
    FundProfile,
    InstrumentAuditReport,
    InstrumentProfile,
    InstrumentType,
    MetricStatus,
    MetricValue,
)
from .fund_providers import EastmoneyFundProfileProvider
from .providers import (
    ConfiguredFundProvider,
    ProviderPayload,
    QQQuoteProvider,
    SecCompanyFactsProvider,
    YahooFinanceProvider,
    merge_metric,
)
from .vanguard_provider import VanguardFundProfileProvider

logger = logging.getLogger(__name__)


def _code(item: object) -> str:
    if isinstance(item, dict):
        return str(item.get("code", "")).strip()
    return str(item).strip()


def _metric_status(metric: MetricValue | None) -> MetricStatus:
    return metric.status if metric is not None else MetricStatus.MISSING


class InstrumentAuditService:
    """Build current typed profiles without exposing them to the optimizer."""

    def __init__(
        self,
        config: dict,
        *,
        session_prices: Optional[dict[str, dict[str, Any]]] = None,
        yahoo_provider: YahooFinanceProvider | None = None,
        qq_provider: QQQuoteProvider | None = None,
        sec_provider: SecCompanyFactsProvider | None = None,
        fund_provider: ConfiguredFundProvider | None = None,
        public_fund_provider: EastmoneyFundProfileProvider | None = None,
        official_fund_provider: VanguardFundProfileProvider | None = None,
    ):
        self.config = config
        self.session_prices = session_prices or {}
        self.yahoo = yahoo_provider or YahooFinanceProvider(config)
        self.qq = qq_provider or QQQuoteProvider(config)
        self.sec = sec_provider or SecCompanyFactsProvider(config)
        self.fund_provider = fund_provider or ConfiguredFundProvider(config)
        self.public_fund_provider = (
            public_fund_provider or EastmoneyFundProfileProvider(config)
        )
        self.official_fund_provider = (
            official_fund_provider or VanguardFundProfileProvider(config)
        )
        self._profile_cache: dict[tuple[str, date], InstrumentProfile] = {}
        audit_config = config.get("instrument_audit", {}) or {}
        self.output_dir = Path(
            audit_config.get("output_dir", "data/instrument_audit")
        )
        self.enrich_holdings = bool(
            audit_config.get("enrich_top_holdings", True)
        )
        self.max_holding_profiles = int(
            audit_config.get("max_holding_profiles_per_fund", 10)
        )

    def run(
        self,
        codes: Iterable[object] | None = None,
        *,
        evaluation_date: date | None = None,
        write_files: bool = True,
    ) -> InstrumentAuditReport:
        evaluation_date = evaluation_date or date.today()
        configured = list(codes if codes is not None else self.config.get("stocks", []))
        normalized_codes = [code for item in configured if (code := _code(item))]
        profiles = [
            self.audit_one(code, evaluation_date=evaluation_date)
            for code in normalized_codes
        ]
        report = InstrumentAuditReport(
            generated_at=datetime.now(),
            evaluation_date=evaluation_date,
            profiles=profiles,
            summary=self._build_summary(profiles),
        )
        if write_files:
            paths = self.write_report(report)
            report.summary["output_files"] = {
                key: str(value) for key, value in paths.items()
            }
        return report

    def audit_one(
        self,
        code: str,
        *,
        evaluation_date: date,
        enrich_holdings: bool = True,
    ) -> InstrumentProfile:
        cache_key = (str(code), evaluation_date)
        cached = self._profile_cache.get(cache_key)
        if cached is not None:
            return cached.copy(deep=True)

        market = detect_market(code)
        configured = (
            self.config.get("instrument_catalog", {}) or {}
        ).get(str(code), {}) or {}
        yahoo = self.yahoo.fetch(code, evaluation_date)
        qq = self.qq.fetch(code, evaluation_date)
        metadata = self._merge_metadata(configured, yahoo, qq)
        instrument_type = classify_instrument(
            code,
            quote_type=metadata.get("quote_type"),
            name=metadata.get("name"),
            configured_type=metadata.get("instrument_type"),
        )
        price = self._current_price(code, evaluation_date, qq, yahoo)
        profile = InstrumentProfile(
            code=str(code),
            name=metadata.get("name"),
            market=market,
            exchange=metadata.get("exchange"),
            currency=metadata.get("currency") or self._market_currency(market),
            instrument_type=instrument_type,
            asset_class=metadata.get("asset_class"),
            sector=metadata.get("sector"),
            industry=metadata.get("industry"),
            latest_price=price,
            applicable_metrics=applicable_metrics(instrument_type),
            source_attempts=yahoo.attempts + qq.attempts,
        )
        self._profile_cache[cache_key] = profile.copy(deep=True)

        if instrument_type == InstrumentType.EQUITY:
            official = (
                self.sec.fetch(code, evaluation_date)
                if market == "us"
                else ProviderPayload(
                    attempts=[
                        {
                            "source": (
                                "exchange_filing"
                                if market == "a_share"
                                else "hkex_filing"
                            ),
                            "status": "unavailable",
                            "reason": (
                                "structured official adapter unavailable; "
                                "Yahoo current-audit fallback used"
                            ),
                        }
                    ]
                )
            )
            profile.source_attempts.extend(official.attempts)
            statements = self._merge_statements(
                official.statements,
                yahoo.statements,
            )
            quoted_pe = merge_metric(qq.quoted_pe, yahoo.quoted_pe)
            quoted_pb = merge_metric(qq.quoted_pb, yahoo.quoted_pb)
            company = derive_company_fundamentals(
                statements,
                current_price=price,
                evaluation_date=evaluation_date,
                quoted_pe=quoted_pe,
                quoted_pb=quoted_pb,
                ttm_dividend_per_share=yahoo.ttm_dividend,
                latest_dividend_per_share=yahoo.latest_dividend,
            )
            company.market_cap = qq.market_cap or MetricValue()
            company.free_float_market_cap = (
                qq.free_float_market_cap or MetricValue()
            )
            if company.total_shares.value is None:
                company.total_shares = self._shares_from_market_cap(
                    company.market_cap,
                    price,
                )
            company.pe_ttm = merge_metric(
                company.pe_ttm,
                quoted_pe,
                conflict_tolerance=0.1,
            )
            company.pb = merge_metric(
                company.pb,
                quoted_pb,
                conflict_tolerance=0.1,
            )
            profile.company = company
        else:
            public_fund = self.public_fund_provider.fetch(
                code, evaluation_date, instrument_type
            )
            official_fund = self.official_fund_provider.fetch(
                code, evaluation_date, instrument_type
            )
            configured_fund = self.fund_provider.fetch(code, evaluation_date)
            profile.source_attempts.extend(public_fund.attempts)
            profile.source_attempts.extend(official_fund.attempts)
            profile.source_attempts.extend(configured_fund.attempts)
            profile.name = (
                configured_fund.metadata.get("name")
                or official_fund.metadata.get("name")
                or public_fund.metadata.get("name")
                or profile.name
            )
            profile.asset_class = (
                configured_fund.metadata.get("asset_class")
                or official_fund.metadata.get("asset_class")
                or public_fund.metadata.get("asset_class")
                or profile.asset_class
            )
            fund = self._merge_fund_profiles(
                public_fund.fund or FundProfile(),
                official_fund.fund,
            )
            fund = self._merge_fund_profiles(
                fund,
                configured_fund.fund,
            )
            if fund.ttm_dividend_per_unit.value is None and yahoo.ttm_dividend is not None:
                fund.ttm_dividend_per_unit = yahoo.ttm_dividend
            if (
                enrich_holdings
                and self.enrich_holdings
                and instrument_type
                in {InstrumentType.INDEX_ETF, InstrumentType.SECTOR_ETF}
            ):
                self._enrich_fund_holdings(fund, evaluation_date)
            profile.fund = derive_fund_profile(fund, current_price=price)

        profile.completeness = self._profile_completeness(profile)
        profile.diagnostics.extend(self._diagnostics(profile))
        self._profile_cache[cache_key] = profile.copy(deep=True)
        return profile

    @staticmethod
    def _market_currency(market: str) -> str:
        return {"a_share": "CNY", "hk": "HKD", "us": "USD"}.get(market, "")

    @staticmethod
    def _merge_fund_profiles(
        fallback: FundProfile,
        preferred: FundProfile | None,
    ) -> FundProfile:
        """Merge per field; configured issuer facts override mirror values."""
        result = fallback.copy(deep=True)
        if preferred is None:
            return result
        for field_name in preferred.__fields__:
            value = getattr(preferred, field_name)
            if isinstance(value, MetricValue):
                if value.value is not None or value.status not in {
                    MetricStatus.MISSING,
                    MetricStatus.NOT_APPLICABLE,
                }:
                    setattr(result, field_name, value.copy(deep=True))
            elif isinstance(value, list):
                if value:
                    setattr(result, field_name, list(value))
            elif isinstance(value, dict):
                if value:
                    setattr(result, field_name, dict(value))
            elif value not in (None, ""):
                setattr(result, field_name, value)
        return result


    @staticmethod
    def _merge_metadata(
        configured: dict[str, Any],
        yahoo: ProviderPayload,
        qq: ProviderPayload,
    ) -> dict[str, Any]:
        result = {}
        for source in (qq.metadata, yahoo.metadata, configured):
            result.update(
                {
                    key: value
                    for key, value in source.items()
                    if value not in (None, "")
                }
            )
        return result

    def _current_price(
        self,
        code: str,
        evaluation_date: date,
        qq: ProviderPayload,
        yahoo: ProviderPayload,
    ) -> MetricValue:
        current = self.session_prices.get(str(code), {}) or {}
        value = current.get("close")
        if value is not None:
            try:
                return MetricValue(
                    value=float(value),
                    status=MetricStatus.OBSERVED,
                    as_of=current.get("date") or evaluation_date,
                    source="daily_session",
                    currency=current.get("currency"),
                )
            except (TypeError, ValueError):
                pass
        return qq.price or yahoo.price or MetricValue.missing(
            "QQ and Yahoo current price unavailable"
        )

    @staticmethod
    def _merge_statements(
        official: list,
        fallback: list,
    ) -> list:
        selected = {}
        for priority, statements in ((0, fallback), (1, official)):
            for statement in statements:
                key = (statement.period_end, statement.period_type)
                current = selected.get(key)
                if current is None or priority >= current[0]:
                    selected[key] = (priority, statement)
        return sorted(
            (value[1] for value in selected.values()),
            key=lambda item: (item.period_end, item.period_type),
        )

    @staticmethod
    def _shares_from_market_cap(
        market_cap: MetricValue,
        price: MetricValue,
    ) -> MetricValue:
        if (
            market_cap.value is None
            or price.value is None
            or price.value <= 0
        ):
            return MetricValue.missing(
                "statement shares and market-cap-derived shares unavailable"
            )
        return MetricValue(
            value=market_cap.value / price.value,
            status=MetricStatus.DERIVED,
            as_of=price.as_of,
            source=f"{market_cap.source}+{price.source}",
            note="总市值/现价，仅用于当前画像校验",
        )

    def _enrich_fund_holdings(
        self,
        fund: FundProfile,
        evaluation_date: date,
    ) -> None:
        from .models import HoldingFundamentals

        for holding in sorted(
            fund.top_holdings,
            key=lambda item: (-item.weight, item.code),
        )[: self.max_holding_profiles]:
            child = self.audit_one(
                holding.code,
                evaluation_date=evaluation_date,
                enrich_holdings=False,
            )
            company = child.company
            if company is None:
                continue
            holding.market = holding.market or child.market
            holding.currency = holding.currency or child.currency
            holding.name = holding.name or child.name
            holding.fundamentals = HoldingFundamentals(
                pe_ttm=company.pe_ttm,
                pb=company.pb,
                roe_ttm=company.roe_ttm,
                dividend_yield=company.dividend_yield,
                revenue_yoy=company.growth.get("revenue_yoy")
                or HoldingFundamentals().revenue_yoy,
                revenue_qoq=company.growth.get("revenue_qoq")
                or HoldingFundamentals().revenue_qoq,
                net_income_yoy=company.growth.get("net_income_yoy")
                or HoldingFundamentals().net_income_yoy,
                net_income_qoq=company.growth.get("net_income_qoq")
                or HoldingFundamentals().net_income_qoq,
            )

    @staticmethod
    def _profile_completeness(profile: InstrumentProfile) -> dict[str, Any]:
        statuses = {}
        for metric in profile.applicable_metrics:
            statuses[metric] = InstrumentAuditService._status_for(profile, metric).value
        complete_statuses = {
            MetricStatus.OBSERVED.value,
            MetricStatus.DERIVED.value,
            MetricStatus.KNOWN_ZERO.value,
            MetricStatus.NOT_MEANINGFUL.value,
            MetricStatus.CONFLICT.value,
        }
        complete = sum(status in complete_statuses for status in statuses.values())
        total = len(statuses)
        return {
            "complete": complete,
            "applicable": total,
            "fill_rate": complete / total if total else 1.0,
            "statuses": statuses,
        }

    @staticmethod
    def _status_for(profile: InstrumentProfile, name: str) -> MetricStatus:
        identity = {
            "name": profile.name,
            "market": profile.market,
            "exchange": profile.exchange,
            "currency": profile.currency,
            "instrument_type": profile.instrument_type,
        }
        if name in identity:
            return (
                MetricStatus.OBSERVED
                if identity[name] not in (None, "")
                else MetricStatus.MISSING
            )
        if profile.company is not None:
            company_map = {
                "total_shares": profile.company.total_shares,
                "pe_ttm": profile.company.pe_ttm,
                "pb": profile.company.pb,
                "roe_ttm": profile.company.roe_ttm,
                "dividend_yield": profile.company.dividend_yield,
            }
            if name in company_map:
                return _metric_status(company_map[name])
            statement_field = {
                "parent_equity": "parent_equity",
                "revenue": "revenue",
                "net_income_parent": "net_income_parent",
            }.get(name)
            if statement_field:
                observed = any(
                    getattr(statement, statement_field) is not None
                    for statement in profile.company.statements
                )
                return (
                    MetricStatus.OBSERVED if observed else MetricStatus.MISSING
                )
            growth_key = {
                "revenue_yoy": "revenue_yoy",
                "revenue_qoq": "revenue_qoq",
                "net_income_yoy": "net_income_yoy",
                "net_income_qoq": "net_income_qoq",
            }.get(name)
            if growth_key:
                growth = profile.company.growth.get(growth_key)
                return growth.status if growth else MetricStatus.MISSING
        if profile.fund is not None:
            direct = getattr(profile.fund, name, None)
            if isinstance(direct, MetricValue):
                return direct.status
            if name == "tracking_index":
                return (
                    MetricStatus.OBSERVED
                    if profile.fund.tracking_index
                    else MetricStatus.MISSING
                )
            if name == "top_holdings":
                return (
                    MetricStatus.OBSERVED
                    if profile.fund.top_holdings
                    else MetricStatus.MISSING
                )
            if name == "look_through":
                observed = any(
                    metric.value.value is not None
                    for metric in profile.fund.look_through.values()
                )
                return (
                    MetricStatus.DERIVED if observed else MetricStatus.MISSING
                )
        return MetricStatus.MISSING

    @staticmethod
    def _diagnostics(profile: InstrumentProfile) -> list[str]:
        result = []
        if profile.latest_price.value is None:
            result.append("current_price_missing")
        failed = [
            attempt
            for attempt in profile.source_attempts
            if attempt.get("status") in {"failed", "unavailable"}
        ]
        result.extend(
            f"{attempt.get('source')}: {attempt.get('reason', attempt.get('status'))}"
            for attempt in failed
        )
        if profile.company and profile.company.statements:
            if all(
                statement.published_at is None
                for statement in profile.company.statements
            ):
                result.append(
                    "statement_publication_dates_missing_current_audit_only"
                )
        if (
            profile.fund is not None
            and profile.instrument_type
            in {InstrumentType.INDEX_ETF, InstrumentType.SECTOR_ETF}
            and not profile.fund.top_holdings
        ):
            result.append("official_top_holdings_missing")
        return result

    @staticmethod
    def _build_summary(
        profiles: list[InstrumentProfile],
    ) -> dict[str, Any]:
        by_type = Counter(profile.instrument_type.value for profile in profiles)
        by_status: Counter[str] = Counter()
        metric_counts: dict[str, Counter[str]] = defaultdict(Counter)
        complete = 0
        applicable = 0
        for profile in profiles:
            completeness = profile.completeness
            complete += int(completeness.get("complete", 0))
            applicable += int(completeness.get("applicable", 0))
            for metric, status in completeness.get("statuses", {}).items():
                by_status[status] += 1
                metric_counts[metric][status] += 1
        return {
            "instrument_count": len(profiles),
            "by_type": dict(sorted(by_type.items())),
            "applicable_metric_count": applicable,
            "complete_metric_count": complete,
            "fill_rate": complete / applicable if applicable else 1.0,
            "by_status": dict(sorted(by_status.items())),
            "metric_coverage": {
                metric: dict(sorted(counts.items()))
                for metric, counts in sorted(metric_counts.items())
            },
        }

    def write_report(
        self,
        report: InstrumentAuditReport,
    ) -> dict[str, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = report.generated_at.strftime("%Y%m%d_%H%M%S")
        json_path = self.output_dir / f"{timestamp}_instrument_audit.json"
        html_path = self.output_dir / f"{timestamp}_instrument_audit.html"
        json_text = report.json(
            ensure_ascii=False,
            indent=2,
            exclude_none=False,
        )
        html_text = render_audit_html(report)
        json_path.write_text(json_text, encoding="utf-8")
        html_path.write_text(html_text, encoding="utf-8")
        (self.output_dir / "latest.json").write_text(json_text, encoding="utf-8")
        (self.output_dir / "latest.html").write_text(html_text, encoding="utf-8")
        logger.info("Instrument audit JSON: %s", json_path)
        logger.info("Instrument audit HTML: %s", html_path)
        return {"json": json_path, "html": html_path}


def load_latest_audit(
    output_dir: str | Path = "data/instrument_audit",
) -> InstrumentAuditReport | None:
    path = Path(output_dir) / "latest.json"
    if not path.exists():
        return None
    try:
        return InstrumentAuditReport.parse_raw(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Unable to load latest instrument audit: %s", exc)
        return None


def _fmt_metric(metric: MetricValue, suffix: str = "") -> str:
    if metric.value is None:
        status = {
            MetricStatus.NOT_APPLICABLE: "不适用",
            MetricStatus.NOT_MEANINGFUL: "无意义",
            MetricStatus.KNOWN_ZERO: "0",
        }.get(metric.status, "—")
        return status
    return f"{metric.value:,.2f}{suffix}"


def render_profile_section(
    report: InstrumentAuditReport | None,
    *,
    compact: bool = True,
) -> str:
    if report is None:
        return ""
    rows = []
    for profile in report.profiles:
        if profile.company is not None:
            company = profile.company
            valuation = (
                f"PE {_fmt_metric(company.pe_ttm)} / "
                f"PB {_fmt_metric(company.pb)} / "
                f"ROE {_fmt_metric(company.roe_ttm, '%')}"
            )
            growth = company.growth.get("net_income_yoy")
            detail = (
                f"利润同比 {growth.value_pct:+.1f}%"
                if growth and growth.value_pct is not None
                else (
                    growth.interpretation
                    if growth and growth.interpretation
                    else "利润同比 —"
                )
            )
        elif profile.fund is not None:
            fund = profile.fund
            if profile.instrument_type == InstrumentType.REIT:
                valuation = (
                    f"P/NAV {_fmt_metric(fund.p_nav)} / "
                    f"P/FFO {_fmt_metric(fund.p_ffo)}"
                )
                detail = f"分派率 {_fmt_metric(fund.distribution_yield, '%')}"
            else:
                valuation = (
                    f"溢价 {_fmt_metric(fund.premium_discount_rate, '%')} / "
                    f"分红率 {_fmt_metric(fund.dividend_yield, '%')}"
                )
                detail = (
                    f"前十大 {fund.top_holdings_weight * 100:.1f}% "
                    f"({len(fund.top_holdings)}只)"
                )
        else:
            valuation = "—"
            detail = "—"
        fill = profile.completeness.get("fill_rate", 0.0) * 100
        rows.append(
            "<tr>"
            f"<td>{html.escape(profile.code)}</td>"
            f"<td>{html.escape(profile.instrument_type.value)}</td>"
            f"<td>{html.escape(valuation)}</td>"
            f"<td>{html.escape(detail)}</td>"
            f"<td>{fill:.0f}%</td>"
            "</tr>"
        )
    title = "标的画像与数据质量" if compact else "标的画像、财务与基金穿透审计"
    return f"""
<tr><td style="padding:16px 24px 4px;border-bottom:2px solid #ecf0f1">
  <div style="font-size:15px;font-weight:600;color:#2c3e50">{title}</div>
</td></tr>
<tr><td style="padding:8px 24px 16px">
  <table role="presentation" style="width:100%;border-collapse:collapse;font-size:12px"
         cellpadding="6" cellspacing="0" border="0">
    <thead><tr style="background:#34495e;color:#fff">
      <th>代码</th><th>类型</th><th>估值/净值</th><th>增长/穿透</th><th>填充率</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</td></tr>
"""


def render_audit_html(report: InstrumentAuditReport) -> str:
    from .reporting import render_detailed_audit_html

    return render_detailed_audit_html(report)
