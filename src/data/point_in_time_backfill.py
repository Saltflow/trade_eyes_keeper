"""Full-universe backfill for point-in-time prices, actions and statements."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from src.instruments.classifier import (
    classify_instrument,
    detect_market,
)
from src.instruments.models import InstrumentType
from src.instruments.point_in_time import (
    BaostockStatementProvider,
    CninfoAnnualReportProvider,
    PointInTimeFundamentalStore,
    SseXbrlStatementProvider,
    STATEMENT_VALUE_FIELDS,
    merge_statement_sources,
)
from src.instruments.providers import SecCompanyFactsProvider

from .market_history import MarketHistoryProvider, PointInTimeMarketStore

logger = logging.getLogger(__name__)


def _code(item: object) -> str:
    if isinstance(item, dict):
        return str(item.get("code", "")).strip()
    return str(item).strip()


class PointInTimeBackfillService:
    """Backfill every configured instrument without changing active strategy data."""

    def __init__(
        self,
        config: dict,
        *,
        market_provider: MarketHistoryProvider | None = None,
        a_share_statements: BaostockStatementProvider | None = None,
        sse_statements: SseXbrlStatementProvider | None = None,
        cninfo_statements: CninfoAnnualReportProvider | None = None,
        sec_provider: SecCompanyFactsProvider | None = None,
        market_store: PointInTimeMarketStore | None = None,
        fundamental_store: PointInTimeFundamentalStore | None = None,
    ):
        self.config = config
        settings = config.get("point_in_time_data", {}) or {}
        self.output_dir = Path(
            settings.get("output_dir", "data/point_in_time")
        )
        self.history_years = max(1, int(settings.get("history_years", 6)))
        self.use_official_crawlers = bool(
            settings.get("official_statement_crawlers", True)
        )
        self.official_pdf_recent_years = max(
            1, int(settings.get("official_pdf_recent_years", 3))
        )
        market_settings = settings.get("market_history", {}) or {}
        self.market_coverage_tolerance_days = max(
            0, int(market_settings.get("coverage_tolerance_days", 31))
        )
        self.market_provider = market_provider or MarketHistoryProvider(config)
        self.a_share_statements = (
            a_share_statements or BaostockStatementProvider(config=config)
        )
        self.sse_statements = sse_statements or SseXbrlStatementProvider(config)
        self.cninfo_statements = cninfo_statements or CninfoAnnualReportProvider(config)
        self.sec_provider = sec_provider or SecCompanyFactsProvider(config)
        self.market_store = market_store or PointInTimeMarketStore(self.output_dir)
        self.fundamental_store = fundamental_store or PointInTimeFundamentalStore(
            self.output_dir
        )

    def run(
        self,
        codes: Iterable[object] | None = None,
        *,
        evaluation_date: date | None = None,
    ) -> dict[str, Any]:
        end = evaluation_date or date.today()
        start = end - timedelta(days=int(self.history_years * 365.25))
        configured = codes if codes is not None else self.config.get("stocks", [])
        normalized = [code for item in configured if (code := _code(item))]
        rows = [self._backfill_one(code, start, end) for code in normalized]
        summary = self._summary(start, end, rows)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"{timestamp}_backfill.json"
        latest = self.output_dir / "latest_backfill.json"
        text = json.dumps(summary, ensure_ascii=False, indent=2)
        path.write_text(text, encoding="utf-8")
        latest.write_text(text, encoding="utf-8")
        summary["output_file"] = str(path)
        return summary

    def _backfill_one(self, code: str, start: date, end: date) -> dict[str, Any]:
        configured = (
            self.config.get("instrument_catalog", {}) or {}
        ).get(str(code), {}) or {}
        instrument_type = classify_instrument(
            code,
            configured_type=configured.get("instrument_type"),
            name=configured.get("name"),
        )
        row: dict[str, Any] = {
            "code": code,
            "market": detect_market(code),
            "instrument_type": instrument_type.value,
            "market_history": {"status": "pending"},
            "statements": {"status": "not_applicable"},
        }
        try:
            bundle = self.market_provider.fetch(code, start, end)
            paths = self.market_store.write(bundle)
            actual_start = pd.Timestamp(bundle.prices["date"].min()).date()
            actual_end = pd.Timestamp(bundle.prices["date"].max()).date()
            requested_days = max(1, (end - start).days)
            covered_days = max(
                0, (actual_end - max(start, actual_start)).days
            )
            coverage_ratio = min(1.0, covered_days / requested_days)
            coverage_status = (
                "full"
                if actual_start
                <= start + timedelta(days=self.market_coverage_tolerance_days)
                else "partial"
            )
            row["market_history"] = {
                "status": "success",
                "source": bundle.source,
                "rows": len(bundle.prices),
                "actions": len(bundle.actions),
                "raw_price_rows": int(bundle.prices["raw_close"].notna().sum()),
                "qfq_price_rows": int(bundle.prices["qfq_close"].notna().sum()),
                "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
                "actual_start": actual_start.isoformat(),
                "actual_end": actual_end.isoformat(),
                "requested_window_coverage": coverage_status,
                "calendar_coverage_ratio": coverage_ratio,
                "output": str(paths["prices"]),
                "diagnostics": bundle.diagnostics,
            }
        except Exception as exc:
            logger.exception("Market-history backfill failed for %s", code)
            row["market_history"] = {
                "status": "failed",
                "reason": str(exc),
            }

        if instrument_type != InstrumentType.EQUITY:
            return row
        try:
            market = detect_market(code)
            if market == "a_share":
                result = self.a_share_statements.fetch(code, start, end)
                statements = list(result.statements)
                attempts = list(result.attempts)
                if self.use_official_crawlers:
                    is_sse = str(code).startswith(("5", "6", "9"))
                    recent_start = date(
                        max(
                            start.year,
                            end.year - self.official_pdf_recent_years + 1,
                        ),
                        1,
                        1,
                    )
                    provider_specs = []
                    if is_sse:
                        provider_specs.append(
                            (self.sse_statements, "sse_xbrl", start)
                        )
                        provider_specs.append(
                            (
                                self.cninfo_statements,
                                "cninfo_annual_report",
                                recent_start,
                            )
                        )
                    else:
                        provider_specs.append(
                            (
                                self.cninfo_statements,
                                "cninfo_annual_report",
                                recent_start,
                            )
                        )
                    for provider, source, provider_start in provider_specs:
                        try:
                            supplement = provider.fetch(
                                code, provider_start, end
                            )
                            statements = merge_statement_sources(
                                statements, supplement.statements
                            )
                            attempts.extend(supplement.attempts)
                        except Exception as exc:
                            logger.warning(
                                "Official statement crawler failed for %s: %s",
                                code,
                                exc,
                            )
                            attempts.append({
                                "source": source,
                                "status": "failed",
                                "reason": str(exc),
                            })
            elif market == "us":
                payload = self.sec_provider.fetch(code, end)
                statements = [
                    item
                    for item in payload.statements
                    if item.period_end >= start and item.published_at is not None
                ]
                attempts = payload.attempts
            else:
                row["statements"] = {
                    "status": "missing",
                    "reason": (
                        "HKEX structured filing-date adapter is unavailable; "
                        "undated Yahoo statements are deliberately rejected"
                    ),
                    "stored": 0,
                }
                return row
            if statements:
                replace_sources = None
                if market == "us":
                    replace_sources = {"sec_companyfacts"}
                elif market == "a_share" and self.use_official_crawlers:
                    replace_sources = {"cninfo_annual_report"}
                output = self.fundamental_store.upsert(
                    code,
                    statements,
                    replace_sources=replace_sources,
                )
                available = self.fundamental_store.as_of(code, end)
                field_availability = self._statement_field_availability(
                    available
                )
                row["statements"] = {
                    "status": "success",
                    "stored": len(statements),
                    "first_period": min(
                        item.period_end for item in statements
                    ).isoformat(),
                    "last_period": max(
                        item.period_end for item in statements
                    ).isoformat(),
                    "all_have_publication_date": all(
                        item.published_at is not None for item in statements
                    ),
                    "output": str(output),
                    "field_availability": field_availability,
                    "attempts": attempts,
                }
            else:
                row["statements"] = {
                    "status": "missing",
                    "stored": 0,
                    "reason": "provider returned no dated financial statements",
                    "attempts": attempts,
                }
        except Exception as exc:
            logger.exception("Statement backfill failed for %s", code)
            row["statements"] = {"status": "failed", "reason": str(exc)}
        return row

    @staticmethod
    def _statement_field_availability(
        statements: Iterable[Any],
    ) -> dict[str, dict[str, Any]]:
        available = list(statements)
        coverage: dict[str, dict[str, Any]] = {}
        for field in STATEMENT_VALUE_FIELDS:
            periods = [
                item.period_end
                for item in available
                if getattr(item, field) is not None
            ]
            coverage[field] = {
                "available": bool(periods),
                "latest_period": max(periods).isoformat() if periods else None,
            }
        return coverage

    @staticmethod
    def _summary(
        start: date,
        end: date,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        market_success = sum(
            row["market_history"].get("status") == "success" for row in rows
        )
        market_full_window = sum(
            row["market_history"].get("requested_window_coverage") == "full"
            for row in rows
        )
        market_partial_window = sum(
            row["market_history"].get("requested_window_coverage") == "partial"
            for row in rows
        )
        applicable = [
            row
            for row in rows
            if row["statements"].get("status") != "not_applicable"
        ]
        statement_success = sum(
            row["statements"].get("status") == "success" for row in applicable
        )
        field_coverage: dict[str, dict[str, float | int]] = {}
        total = len(applicable)
        for field in STATEMENT_VALUE_FIELDS:
            filled = sum(
                bool(
                    (
                        row["statements"]
                        .get("field_availability", {})
                        .get(field, {})
                    ).get("available")
                )
                for row in applicable
            )
            field_coverage[field] = {
                "filled": filled,
                "total": total,
                "fill_rate": filled / total if total else 0.0,
            }
        return {
            "generated_at": datetime.now().isoformat(),
            "contract": "point-in-time-data-1",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "instrument_count": len(rows),
            "market_history_success": market_success,
            "market_history_failed": len(rows) - market_success,
            "market_history_full_window": market_full_window,
            "market_history_partial_window": market_partial_window,
            "statement_applicable": len(applicable),
            "statement_success": statement_success,
            "statement_incomplete": len(applicable) - statement_success,
            "statement_field_coverage": field_coverage,
            "instruments": rows,
        }
