"""Full-universe backfill for point-in-time prices, actions and statements."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src.instruments.classifier import (
    classify_instrument,
    detect_market,
)
from src.instruments.models import InstrumentType
from src.instruments.point_in_time import (
    STATEMENT_VALUE_FIELDS,
    BaostockStatementProvider,
    CninfoAnnualReportProvider,
    HkexStatementProvider,
    PointInTimeFundamentalStore,
    SseXbrlStatementProvider,
    merge_statement_sources,
)
from src.instruments.providers import SecCompanyFactsProvider

from .baostock_access import BaostockAccessBlocked, BaostockTransientError
from .market_history import (
    CorporateAction,
    MarketHistoryProvider,
    PointInTimeMarketStore,
    PriceHistoryBundle,
    corporate_action_issues,
)

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
        hkex_statements: HkexStatementProvider | None = None,
        sec_provider: SecCompanyFactsProvider | None = None,
        market_store: PointInTimeMarketStore | None = None,
        fundamental_store: PointInTimeFundamentalStore | None = None,
    ):
        self.config = config
        settings = config.get("point_in_time_data", {}) or {}
        self.output_dir = Path(settings.get("output_dir", "data/point_in_time"))
        self.history_years = max(1, int(settings.get("history_years", 6)))
        self.use_official_crawlers = bool(
            settings.get("official_statement_crawlers", True)
        )
        self.official_pdf_recent_years = max(
            1, int(settings.get("official_pdf_recent_years", 3))
        )
        self.fx_symbols = {
            str(item).strip()
            for item in (settings.get("fx_symbols", []) or [])
            if str(item).strip()
        }
        self.market_only_symbols = {
            str(item).strip()
            for item in (settings.get("market_only_symbols", []) or [])
            if str(item).strip()
        }
        market_settings = settings.get("market_history", {}) or {}
        self.market_coverage_tolerance_days = max(
            0, int(market_settings.get("coverage_tolerance_days", 31))
        )
        self.market_provider = market_provider or MarketHistoryProvider(config)
        self.a_share_statements = a_share_statements or BaostockStatementProvider(
            config=config
        )
        self.sse_statements = sse_statements or SseXbrlStatementProvider(config)
        self.cninfo_statements = cninfo_statements or CninfoAnnualReportProvider(config)
        self.hkex_statements = hkex_statements or HkexStatementProvider(config)
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
        normalized.extend(
            code
            for code in sorted(self.fx_symbols | self.market_only_symbols)
            if code not in normalized
        )
        rows = []
        for code in normalized:
            row = self._backfill_one(code, start, end)
            rows.append(row)
            if row.get("source_access", {}).get("blocked"):
                break
        summary = self._summary(start, end, rows)
        summary["requested_instrument_count"] = len(normalized)
        summary["pending_codes"] = normalized[len(rows) :]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"{timestamp}_backfill.json"
        latest = self.output_dir / "latest_backfill.json"
        text = json.dumps(summary, ensure_ascii=False, indent=2)
        path.write_text(text, encoding="utf-8")
        latest.write_text(text, encoding="utf-8")
        summary["output_file"] = str(path)
        return summary

    def _market_paths(self, code: str) -> dict[str, Path]:
        stem = PointInTimeMarketStore._safe_code(code)
        directory = self.market_store.market_dir
        return {
            "prices": directory / f"{stem}.csv",
            "actions": directory / f"{stem}.actions.json",
            "metadata": directory / f"{stem}.meta.json",
            "receipt": directory / f"{stem}.request.json",
        }

    def _market_settings_hash(self) -> str:
        settings = (self.config.get("point_in_time_data", {}) or {}).get(
            "market_history", {}
        )
        encoded = json.dumps(settings, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _market_row(
        self, bundle: PriceHistoryBundle, start: date, end: date, price_path: Path
    ) -> dict[str, Any]:
        actual_start = pd.Timestamp(bundle.prices["date"].min()).date()
        actual_end = pd.Timestamp(bundle.prices["date"].max()).date()
        requested_days = max(1, (end - start).days)
        covered_days = max(0, (actual_end - max(start, actual_start)).days)
        coverage = (
            "full"
            if actual_start
            <= start + timedelta(days=self.market_coverage_tolerance_days)
            and actual_end >= end - timedelta(days=self.market_coverage_tolerance_days)
            else "partial"
        )
        return {
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
            "requested_window_coverage": coverage,
            "calendar_coverage_ratio": min(1.0, covered_days / requested_days),
            "output": str(price_path),
            "diagnostics": list(bundle.diagnostics),
            "action_issues": list(corporate_action_issues(bundle.actions)),
        }

    def _validated_market_snapshot(
        self, code: str, start: date, end: date, expected: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Validate the same bytes that will be hashed in the request receipt."""
        paths = self._market_paths(code)
        if (
            expected.get("status") != "success"
            or expected.get("requested_start") != start.isoformat()
            or expected.get("requested_end") != end.isoformat()
            or Path(str(expected.get("output", ""))).resolve()
            != paths["prices"].resolve()
        ):
            raise ValueError("market cache does not match the original request")
        contents = {
            name: paths[name].read_bytes() for name in ("prices", "actions", "metadata")
        }
        metadata = json.loads(contents["metadata"])
        action_rows = json.loads(contents["actions"])
        if (
            not isinstance(metadata, dict)
            or metadata.get("contract") != "raw-and-qfq-1"
            or metadata.get("code") != str(code)
            or not isinstance(action_rows, list)
            or not isinstance(metadata.get("diagnostics"), list)
        ):
            raise ValueError("invalid market cache metadata or actions")
        frame = pd.read_csv(io.BytesIO(contents["prices"]))
        if frame.empty or not set(PriceHistoryBundle.REQUIRED_COLUMNS).issubset(frame):
            raise ValueError("market cache is empty or lacks raw/qfq columns")
        dates = pd.to_datetime(frame["date"], errors="coerce")
        if (
            dates.isna().any()
            or dates.duplicated().any()
            or not dates.is_monotonic_increasing
            or dates.min().date() < start
            or dates.max().date() > end
        ):
            raise ValueError(
                "market cache dates are invalid or outside requested window"
            )
        flags = frame["tradable"].astype(str).str.lower()
        if not flags.isin({"true", "false", "1", "0"}).all():
            raise ValueError("invalid cached tradability flags")
        frame["tradable"] = flags.isin({"true", "1"})
        numeric_columns = list(PriceHistoryBundle.REQUIRED_COLUMNS[1:-1])
        numbers = frame[numeric_columns].apply(pd.to_numeric, errors="coerce")
        if (
            np.isinf(numbers.to_numpy()).any()
            or not np.isfinite(numbers.loc[frame["tradable"]].to_numpy()).all()
        ):
            raise ValueError("market cache contains invalid numeric values")
        price_columns = [column for column in numeric_columns if column != "volume"]
        if not numbers.loc[frame["tradable"], price_columns].gt(0).all().all():
            raise ValueError("tradable cached prices must be positive")
        actions = [CorporateAction(**item) for item in action_rows]
        for action in actions:
            if action.code != str(code) or not start <= action.ex_date <= end:
                raise ValueError(
                    "cached corporate action belongs to another window/code"
                )
            for field in (
                "cash_per_share",
                "share_multiplier",
                "rights_price",
                "raw_adjustment_factor",
            ):
                value = getattr(action, field)
                if value is not None and not math.isfinite(value):
                    raise ValueError("cached corporate action is not finite")
        bundle = PriceHistoryBundle(
            code=code,
            prices=frame,
            actions=actions,
            source=str(metadata.get("source", "")),
            currency=metadata.get("currency"),
            diagnostics=list(metadata["diagnostics"]),
        ).validate()
        row = self._market_row(bundle, start, end, paths["prices"])
        for key in (
            "source",
            "rows",
            "actions",
            "raw_price_rows",
            "qfq_price_rows",
            "actual_start",
            "actual_end",
            "requested_window_coverage",
            "diagnostics",
        ):
            if row[key] != expected.get(key):
                raise ValueError(f"market cache differs from checkpoint field {key}")
        for key, row_key in (
            ("rows", "rows"),
            ("actions", "actions"),
            ("start", "actual_start"),
            ("end", "actual_end"),
        ):
            if metadata.get(key) != row[row_key]:
                raise ValueError(f"market metadata differs from data field {key}")
        return row, {
            name: hashlib.sha256(content).hexdigest()
            for name, content in contents.items()
        }

    def _write_market_receipt(
        self, code: str, row: dict[str, Any], hashes: dict[str, str]
    ) -> None:
        path = self._market_paths(code)["receipt"]
        current_paths = self._market_paths(code)
        current_hashes = {
            name: hashlib.sha256(current_paths[name].read_bytes()).hexdigest()
            for name in ("prices", "actions", "metadata")
        }
        if current_hashes != hashes:
            raise ValueError("market cache changed while validating its receipt")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = {
            "contract": "point-in-time-market-cache-1",
            "code": str(code),
            "market_settings_hash": self._market_settings_hash(),
            "sha256": hashes,
            "market_history": row,
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _cached_market(
        self, code: str, start: date, end: date, previous_market: dict | None
    ) -> dict[str, Any] | None:
        """Reuse only an exact request receipt or validated matching batch evidence."""
        path = self._market_paths(code)["receipt"]
        try:
            if path.exists():
                receipt = json.loads(path.read_text(encoding="utf-8"))
                if (
                    receipt.get("contract") != "point-in-time-market-cache-1"
                    or receipt.get("code") != str(code)
                    or receipt.get("market_settings_hash")
                    != self._market_settings_hash()
                ):
                    return None
                row, hashes = self._validated_market_snapshot(
                    code, start, end, receipt["market_history"]
                )
                if hashes != receipt.get("sha256"):
                    return None
                evidence = "request_receipt"
            elif previous_market is not None:
                row, hashes = self._validated_market_snapshot(
                    code, start, end, previous_market
                )
                try:
                    self._write_market_receipt(code, row, hashes)
                except OSError as exc:
                    logger.warning(
                        "Could not seal validated market cache for %s: %s", code, exc
                    )
                evidence = "reference_checkpoint"
            else:
                return None
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning("Ignoring invalid market cache for %s: %s", code, exc)
            return None
        row["cache_hit"] = True
        row["cache_evidence"] = evidence
        return row

    def _backfill_one(
        self, code: str, start: date, end: date, *, previous_market: dict | None = None
    ) -> dict[str, Any]:
        configured = (self.config.get("instrument_catalog", {}) or {}).get(
            str(code), {}
        ) or {}
        is_fx = code in self.fx_symbols
        is_market_only = code in self.market_only_symbols
        instrument_type = None
        if not is_fx and not is_market_only:
            instrument_type = classify_instrument(
                code,
                configured_type=configured.get("instrument_type"),
                name=configured.get("name"),
            )
        row: dict[str, Any] = {
            "code": code,
            "market": detect_market(code),
            "instrument_type": (
                "fx"
                if is_fx
                else "market_benchmark"
                if is_market_only
                else instrument_type.value
            ),
            "market_history": {"status": "pending"},
            "statements": {"status": "not_applicable"},
        }
        try:
            cached = self._cached_market(code, start, end, previous_market)
            if cached is not None:
                row["market_history"] = cached
            else:
                bundle = self.market_provider.fetch(code, start, end)
                paths = self.market_store.write(bundle)
                row["market_history"] = self._market_row(
                    bundle, start, end, paths["prices"]
                )
                try:
                    validated, hashes = self._validated_market_snapshot(
                        code, start, end, row["market_history"]
                    )
                    self._write_market_receipt(code, validated, hashes)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    logger.warning(
                        "Market cache receipt not created for %s: %s", code, exc
                    )
        except BaostockAccessBlocked as exc:
            row["source_access"] = exc.to_dict()
            row["market_history"] = {"status": "blocked", "reason": str(exc)}
            row["statements"] = {"status": "blocked", "reason": "source access blocked"}
            return row
        except Exception as exc:
            logger.exception("Market-history backfill failed for %s", code)
            row["market_history"] = {
                "status": "failed",
                "reason": str(exc),
            }
            if isinstance(exc, BaostockTransientError):
                row["source_access"] = exc.to_dict()

        if is_fx or is_market_only or instrument_type != InstrumentType.EQUITY:
            return row
        try:
            market = detect_market(code)
            if market == "a_share":
                statements = []
                attempts = []
                try:
                    result = self.a_share_statements.fetch(code, start, end)
                    statements = list(result.statements)
                    attempts = list(result.attempts)
                except BaostockAccessBlocked:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep independent provider failures visible
                    logger.warning("Baostock statements failed for %s: %s", code, exc)
                    attempts.append(
                        {
                            "source": "baostock_profit",
                            "status": "failed",
                            "reason": str(exc),
                        }
                    )
                    if isinstance(exc, BaostockTransientError):
                        row["source_access"] = exc.to_dict()
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
                        provider_specs.append((self.sse_statements, "sse_xbrl", start))
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
                            supplement = provider.fetch(code, provider_start, end)
                            statements = merge_statement_sources(
                                statements, supplement.statements
                            )
                            attempts.extend(supplement.attempts)
                        except BaostockAccessBlocked:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "Official statement crawler failed for %s: %s",
                                code,
                                exc,
                            )
                            attempts.append(
                                {
                                    "source": source,
                                    "status": "failed",
                                    "reason": str(exc),
                                }
                            )
            elif market == "us":
                payload = self.sec_provider.fetch(code, end)
                statements = [
                    item
                    for item in payload.statements
                    if item.period_end >= start and item.published_at is not None
                ]
                attempts = payload.attempts
            elif market == "hk":
                result = self.hkex_statements.fetch(code, start, end)
                statements = list(result.statements)
                attempts = list(result.attempts)
            else:
                raise ValueError(f"unsupported equity market: {market}")
            if statements:
                replace_sources = None
                if market == "us":
                    replace_sources = {"sec_companyfacts"}
                elif market == "hk":
                    replace_sources = {"hkex_results_pdf"}
                elif (
                    market == "a_share"
                    and self.use_official_crawlers
                    and any(
                        "cninfo_annual_report" in item.source.split("+")
                        for item in statements
                    )
                ):
                    replace_sources = {"cninfo_annual_report"}
                output = self.fundamental_store.upsert(
                    code,
                    statements,
                    replace_sources=replace_sources,
                )
                available = self.fundamental_store.as_of(code, end)
                field_availability = self._statement_field_availability(available)
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
                source_failed = any(item.get("status") == "failed" for item in attempts)
                row["statements"] = {
                    "status": "failed" if source_failed else "missing",
                    "stored": 0,
                    "reason": (
                        "no dated financial statements; one or more providers failed"
                        if source_failed
                        else "provider returned no dated financial statements"
                    ),
                    "attempts": attempts,
                }
        except BaostockAccessBlocked as exc:
            row["source_access"] = exc.to_dict()
            row["statements"] = {"status": "blocked", "reason": str(exc)}
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
            row for row in rows if row["statements"].get("status") != "not_applicable"
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
                        row["statements"].get("field_availability", {}).get(field, {})
                    ).get("available")
                )
                for row in applicable
            )
            field_coverage[field] = {
                "filled": filled,
                "total": total,
                "fill_rate": filled / total if total else 0.0,
            }
        summary = {
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
        blocked = next(
            (
                row["source_access"]
                for row in rows
                if row.get("source_access", {}).get("blocked")
            ),
            None,
        )
        if blocked is not None:
            summary["source_access"] = copy.deepcopy(blocked)
        return summary
