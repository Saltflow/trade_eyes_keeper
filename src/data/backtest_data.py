"""外层回测数据准备与 raw/qfq 严格校验。

回测引擎只接受已经准备好的行情包。本模块负责在命令编排层读取本地
Point-in-Time store、补齐缺失行情，并在交给指标/仿真代码前再次验证
raw、qfq 和公司行为合同。
"""

from __future__ import annotations

import email.utils
import json
import logging
import time
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

from src.instruments.classifier import detect_market

from ..backtest.execution import build_corporate_action_schedule
from .baostock_access import BaostockAccessBlocked, BaostockTransientError
from .listing_dates import ListingDateEvidence, ListingDateStore
from .market_calendar import resolve_market_data_cutoff
from .market_history import (
    MarketHistoryProvider,
    PointInTimeMarketStore,
    PriceHistoryBundle,
    corporate_action_issues,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataReadinessIssue:
    """One symbol-level reason why a strict data package is not ready."""

    code: str
    source: str
    missing_start: str
    missing_end: str
    reason: str
    category: str = "missing_data"

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "source": self.source,
            "missing_start": self.missing_start,
            "missing_end": self.missing_end,
            "reason": self.reason,
            "category": self.category,
        }


@dataclass(frozen=True)
class DataFetchAttempt:
    """Auditable single attempt to reuse or acquire a strict market bundle."""

    code: str
    source: str
    attempt: int
    status: str
    reason: str = ""
    retry_after_seconds: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "source": self.source,
            "attempt": self.attempt,
            "status": self.status,
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass
class BacktestDataResult:
    """Validated market inputs and an auditable readiness report."""

    purpose: str
    requested_start: date
    requested_end: date
    bundles: dict[str, PriceHistoryBundle] = field(default_factory=dict)
    issues: list[DataReadinessIssue] = field(default_factory=list)
    fetched_codes: list[str] = field(default_factory=list)
    reused_codes: list[str] = field(default_factory=list)
    fundamental_backfill: dict[str, object] = field(default_factory=dict)
    market_cutoffs: dict[str, dict[str, str]] = field(default_factory=dict)
    fetch_attempts: list[DataFetchAttempt] = field(default_factory=list)
    listing_dates: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def ready_codes(self) -> tuple[str, ...]:
        return tuple(sorted(self.bundles))

    @property
    def ready(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, object]:
        return {
            "purpose": self.purpose,
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "ready_codes": list(self.ready_codes),
            "fetched_codes": list(self.fetched_codes),
            "reused_codes": list(self.reused_codes),
            "issues": [item.as_dict() for item in self.issues],
            "fundamental_backfill": dict(self.fundamental_backfill),
            "market_cutoffs": dict(self.market_cutoffs),
            "fetch_attempts": [item.as_dict() for item in self.fetch_attempts],
            "listing_dates": dict(self.listing_dates),
        }

    def write_report(self, path: Path | str) -> Path:
        """Persist readiness evidence without changing optimizer pointers."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(destination)
        return destination


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _code(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("code", "")).strip()
    return str(value).strip()


def _coverage_tolerance(config: dict) -> int:
    settings = config.get("point_in_time_data", {}) or {}
    market = settings.get("market_history", {}) or {}
    return max(0, int(market.get("coverage_tolerance_days", 31)))


def validate_market_bundle(
    bundle: PriceHistoryBundle,
    code: str,
    start: date,
    end: date,
    *,
    require_current: bool = False,
    now: date | None = None,
    tolerance_days: int = 31,
) -> PriceHistoryBundle:
    """Validate coverage, aligned prices and executable corporate actions."""
    if not isinstance(bundle, PriceHistoryBundle):
        raise TypeError(f"{code}: not a point-in-time market bundle")
    bundle = bundle.validate()
    frame = bundle.prices
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    dates = dates.loc[dates.dt.date <= end]
    if dates.empty:
        raise ValueError(f"{code}: bundle has no valid dates through {end.isoformat()}")
    first = dates.min().date()
    last = dates.max().date()
    listing = None
    if bundle.listing_evidence is not None:
        listing = ListingDateEvidence.from_dict(bundle.listing_evidence).validate(code)
        if first < listing.listing_date:
            raise ValueError(
                f"{code}: prices precede official listing date "
                f"{listing.listing_date.isoformat()}"
            )
        if first != listing.listing_date:
            raise ValueError(
                f"{code}: first price {first.isoformat()} differs from official "
                f"listing date {listing.listing_date.isoformat()}"
            )
    if first > start + timedelta(days=tolerance_days) and (
        listing is None or listing.listing_date <= start
    ):
        raise ValueError(
            f"coverage starts at {first.isoformat()}, requested "
            f"{start.isoformat()}; official listing evidence required"
        )
    if last < end:
        raise ValueError(
            f"coverage ends at {last.isoformat()}, requested {end.isoformat()}"
        )
    if require_current:
        current = now or datetime.now(timezone.utc).date()
        if last < current - timedelta(days=14):
            raise ValueError(
                f"coverage ends at {last.isoformat()}, current data is stale"
            )
    for column in PriceHistoryBundle.REQUIRED_COLUMNS[1:]:
        if column not in frame:
            raise ValueError(f"missing required column {column}")
        if column == "tradable":
            continue
        if pd.to_numeric(frame[column], errors="coerce").isna().any():
            raise ValueError(f"column {column} contains missing values")
    issues = corporate_action_issues(bundle.actions)
    if issues:
        raise ValueError("; ".join(issues))
    build_corporate_action_schedule(bundle.actions, frame["date"], [str(code)])
    # Cached bundles can contain newer or still-trading daily bars. Keep the
    # complete source contract checks above, then expose only the accepted
    # window to downstream indicators and simulations without rewriting cache.
    return replace(
        bundle,
        prices=frame.loc[pd.to_datetime(frame["date"]).dt.date <= end].copy(),
        actions=[action for action in bundle.actions if action.ex_date <= end],
        diagnostics=list(bundle.diagnostics),
    )


def _issue(
    code: str,
    source: str,
    start: date,
    end: date,
    reason: object,
) -> DataReadinessIssue:
    text = str(reason)
    lowered = text.lower()
    if "403" in lowered or "blocked" in lowered or "forbidden" in lowered:
        category = "source_blocked"
    elif (
        "in-kind distribution is not supported" in lowered
        or "rights issue is not supported" in lowered
    ):
        category = "corporate_action_unsupported"
    elif "corporate action" in lowered or "adjustment factor" in lowered:
        category = "corporate_action_evidence_missing"
    elif (
        "coverage starts" in lowered
        or "insufficient history" in lowered
        or "listing date" in lowered
        or "listing-date" in lowered
        or "listing route" in lowered
    ):
        category = "insufficient_history_or_listing_evidence"
    elif source == "market_calendar":
        category = "market_calendar_unavailable"
    else:
        category = "fetch_or_validation_failed"
    return DataReadinessIssue(
        code=str(code),
        source=str(source or "point_in_time"),
        missing_start=start.isoformat(),
        missing_end=end.isoformat(),
        reason=text,
        category=category,
    )


def _fundamental_dependencies(strategy) -> tuple[str, ...]:
    return tuple(
        getattr(strategy, "fundamental_feature_dependencies", ()) or ()
    ) if strategy is not None else ()


def _planned_source(code: str, purpose: str) -> str:
    if purpose != "optimizer":
        return "market_provider"
    market = detect_market(code)
    if market == "us":
        return "nasdaq_disclosed"
    if market == "a_share" and code.startswith(("0", "3", "6")):
        return "baostock_disclosed_actions"
    return "tencent_disclosed_actions"


def _retry_delay(exc: Exception, attempt: int, now: datetime) -> float | None:
    """Return a safe delay only for transient transport/service failures."""
    if isinstance(exc, BaostockAccessBlocked):
        return None
    if isinstance(exc, BaostockTransientError):
        retry_at = exc.retry_at
        if retry_at:
            try:
                retry_time = datetime.fromisoformat(retry_at)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_time - now).total_seconds())
            except ValueError:
                return None
        return min(60.0, 2.0 ** max(0, attempt - 1))
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        status = getattr(response, "status_code", None)
        if status == 403 or status not in (429, 500, 502, 503, 504):
            return None
        headers = getattr(response, "headers", {}) or {}
        value = headers.get("Retry-After")
        if value:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                try:
                    retry_time = email.utils.parsedate_to_datetime(value)
                    if retry_time.tzinfo is None:
                        retry_time = retry_time.replace(tzinfo=timezone.utc)
                    return max(0.0, (retry_time - now).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(60.0, 2.0 ** max(0, attempt - 1))
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, TimeoutError)):
        return min(60.0, 2.0 ** max(0, attempt - 1))
    return None


def prepare_backtest_data(
    config: dict,
    codes: Iterable[object],
    start_date: date | datetime | str,
    end_date: date | datetime | str,
    *,
    purpose: str = "backtest",
    benchmark_codes: Iterable[object] = (),
    strategy=None,
    require_current: bool = False,
    readiness_path: Path | str | None = None,
    as_of: datetime | None = None,
) -> BacktestDataResult:
    """Read or fetch one strict package for instruments and benchmarks.

    Existing valid bundles are reused. Missing, incomplete, stale, or
    unexplained bundles are fetched once through ``MarketHistoryProvider`` and
    validated again. A failed fetch is reported per code; no adjusted-only
    legacy cache is consulted.
    """
    start = _as_date(start_date)
    end = _as_date(end_date)
    if end < start:
        raise ValueError("backtest end date precedes start date")
    if "point_in_time_data" not in config:
        raise ValueError("point_in_time_data is required for strict backtest data")

    settings = config.get("point_in_time_data", {}) or {}
    root = Path(settings.get("output_dir", "data/point_in_time"))
    store = PointInTimeMarketStore(root)
    listing_store = ListingDateStore(root)
    # Optimization uses the independently disclosed price/action routes where
    # available. Ordinary interactive/backtest callers retain their configured
    # source routing. Never let a provider silently turn an adjusted-only feed
    # into a raw/qfq research input.
    provider_config = config
    if purpose == "optimizer":
        provider_config = deepcopy(config)
        market_settings = provider_config.setdefault(
            "point_in_time_data", {}
        ).setdefault("market_history", {})
        market_settings.setdefault("optimizer_auto_backfill", {})
        market_settings["disclosed_sources"] = True
    provider = MarketHistoryProvider(provider_config)
    tolerance = _coverage_tolerance(config)
    observed = as_of or datetime.now(timezone.utc)
    result = BacktestDataResult(str(purpose), start, end)
    requested_codes = [_code(item) for item in codes if _code(item)]
    requested_benchmarks = [
        _code(item)
        for item in benchmark_codes
        if _code(item) and _code(item) != "risk_free"
    ]
    all_codes = list(dict.fromkeys([*requested_codes, *requested_benchmarks]))
    market_settings = (provider_config.get("point_in_time_data", {}) or {}).get(
        "market_history", {}
    ) or {}
    backfill_settings = market_settings.get("optimizer_auto_backfill", {}) or {}
    max_attempts = 2 if purpose == "optimizer" else 1
    if purpose == "optimizer":
        max_attempts = max(1, min(2, int(backfill_settings.get("max_attempts", 2))))
    market_budget_seconds = max(
        1, min(3600, int(backfill_settings.get("market_budget_seconds", 3600)))
    )
    market_deadlines: dict[str, float] = {}

    for code in all_codes:
        try:
            cutoff = resolve_market_data_cutoff(code, end, as_of=observed)
            result.market_cutoffs[code] = cutoff.as_dict()
            effective_end = cutoff.effective_end
            if effective_end < start:
                raise ValueError("no completed session within requested window")
        except ValueError as exc:
            result.issues.append(_issue(code, "market_calendar", start, end, exc))
            continue
        loaded = None
        load_reason = "bundle is absent"
        try:
            loaded = store.read(code)
            if loaded is not None:
                cached_bundle = loaded
                listing_added = False
                first_loaded = pd.Timestamp(loaded.prices["date"].min()).date()
                if (
                    purpose == "optimizer"
                    and first_loaded > start + timedelta(days=tolerance)
                    and loaded.listing_evidence is None
                ):
                    loaded.listing_evidence = listing_store.resolve(code).as_dict()
                    listing_added = True
                loaded = validate_market_bundle(
                    loaded,
                    code,
                    start,
                    effective_end,
                    require_current=require_current,
                    now=observed.date(),
                    tolerance_days=tolerance,
                )
                if listing_added:
                    # Validation exposes only the requested window. Persist the
                    # original complete cache with its new evidence instead.
                    store.write(cached_bundle)
                result.bundles[code] = loaded
                if loaded.listing_evidence:
                    result.listing_dates[code] = dict(loaded.listing_evidence)
                result.reused_codes.append(code)
                result.fetch_attempts.append(
                    DataFetchAttempt(
                        code,
                        loaded.source or "point_in_time_cache",
                        0,
                        "reused",
                    )
                )
                continue
        except Exception as exc:  # noqa: BLE001 - corrupt cache must be repaired
            load_reason = str(exc)
            if loaded is None:
                result.fetch_attempts.append(
                    DataFetchAttempt(
                        code, "point_in_time_cache", 0, "miss", load_reason
                    )
                )
            else:
                result.fetch_attempts.append(
                    DataFetchAttempt(
                        code,
                        loaded.source or "point_in_time_cache",
                        0,
                        "rejected",
                        load_reason,
                    )
                )

        last_error = None
        market = detect_market(code)
        market_deadline = market_deadlines.setdefault(
            market, time.monotonic() + market_budget_seconds
        )
        for attempt in range(1, max_attempts + 1):
            if time.monotonic() >= market_deadline:
                last_error = TimeoutError(
                    "optimizer market backfill exceeded "
                    f"{market_budget_seconds}s budget"
                )
                result.fetch_attempts.append(
                    DataFetchAttempt(
                        code, "market_budget", attempt, "stopped", str(last_error)
                    )
                )
                break
            try:
                fetched = None
                fetched = provider.fetch(code, start, effective_end)
                first_fetched = pd.Timestamp(fetched.prices["date"].min()).date()
                if (
                    purpose == "optimizer"
                    and first_fetched > start + timedelta(days=tolerance)
                    and fetched.listing_evidence is None
                ):
                    fetched.listing_evidence = listing_store.resolve(code).as_dict()
                fetched = validate_market_bundle(
                    fetched,
                    code,
                    start,
                    effective_end,
                    require_current=require_current,
                    now=observed.date(),
                    tolerance_days=tolerance,
                )
                # Persist only after every raw/qfq/action/currentness check passes.
                store.write(fetched)
                result.bundles[code] = fetched
                if fetched.listing_evidence:
                    result.listing_dates[code] = dict(fetched.listing_evidence)
                result.fetched_codes.append(code)
                result.fetch_attempts.append(
                    DataFetchAttempt(
                        code,
                        fetched.source or "market_provider",
                        attempt,
                        "fetched",
                    )
                )
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - provider failure is audited
                last_error = exc
                delay = _retry_delay(exc, attempt, datetime.now(timezone.utc))
                result.fetch_attempts.append(
                    DataFetchAttempt(
                        code,
                        getattr(fetched, "source", "")
                        or _planned_source(code, purpose),
                        attempt,
                        (
                            "retryable_failure"
                            if delay is not None and attempt < max_attempts
                            else "rejected"
                        ),
                        str(exc),
                        delay,
                    )
                )
                if delay is None or attempt >= max_attempts:
                    break
                remaining = market_deadline - time.monotonic()
                if delay > remaining:
                    last_error = TimeoutError(
                        f"retry delay {delay:.1f}s exceeds optimizer market budget"
                    )
                    break
                time.sleep(delay)
        if last_error is not None:
            exc = last_error
            reason = f"local={load_reason}; fetch={exc}"
            logger.warning("Backtest data is not ready for %s: %s", code, reason)
            result.issues.append(
                _issue(code, getattr(loaded, "source", ""), start, end, reason)
            )

    dependencies = _fundamental_dependencies(strategy)
    if dependencies and all_codes:
        # The existing PIT service owns statement-provider routing and keeps
        # published_at/period_end semantics. It is invoked only when a
        # strategy declares fundamental inputs; technical strategies stay
        # market-only and cheap.
        try:
            from src.data.point_in_time_backfill import PointInTimeBackfillService

            result.fundamental_backfill = PointInTimeBackfillService(config).run(
                codes=requested_codes,
                evaluation_date=end,
            )
            receipt = result.fundamental_backfill
            instruments = receipt.get("instruments")
            by_code = {
                _code(item.get("code")): item
                for item in instruments
                if isinstance(item, dict) and _code(item.get("code"))
            } if isinstance(instruments, list) else {}
            for code in requested_codes:
                item = by_code.get(code, {})
                statement = item.get("statements", {}) or {}
                if not isinstance(statement, dict):
                    statement = {}
                status = str(statement.get("status", "missing"))
                if receipt.get("status") == "failed":
                    reason = receipt.get("reason", "fundamental backfill failed")
                elif status in {"success", "not_applicable"}:
                    continue
                else:
                    reason = statement.get(
                        "reason", f"fundamental statements are {status}"
                    )
                result.issues.append(
                    _issue(code, "fundamental", start, end, reason)
                )
        except Exception as exc:  # noqa: BLE001 - readiness must fail closed
            result.fundamental_backfill = {
                "status": "failed",
                "reason": str(exc),
                "dependencies": list(dependencies),
            }
            for code in requested_codes:
                result.issues.append(
                    _issue(code, "fundamental", start, end, exc)
                )

    if readiness_path is not None:
        result.write_report(readiness_path)
    return result
