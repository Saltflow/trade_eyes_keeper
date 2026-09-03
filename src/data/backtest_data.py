"""外层回测数据准备与 raw/qfq 严格校验。

回测引擎只接受已经准备好的行情包。本模块负责在命令编排层读取本地
Point-in-Time store、补齐缺失行情，并在交给指标/仿真代码前再次验证
raw、qfq 和公司行为合同。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.backtest.execution import build_corporate_action_schedule
from src.data.market_history import (
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

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "source": self.source,
            "missing_start": self.missing_start,
            "missing_end": self.missing_end,
            "reason": self.reason,
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
        }

    def write_report(self, path: Path | str) -> Path:
        """Persist readiness evidence without changing optimizer pointers."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
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
        raise ValueError(f"{code}: not a point-in-time market bundle")
    bundle = bundle.validate()
    frame = bundle.prices
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    if dates.empty:
        raise ValueError(f"{code}: bundle has no valid dates")
    first = dates.min().date()
    last = dates.max().date()
    if first > start + timedelta(days=tolerance_days):
        raise ValueError(
            f"coverage starts at {first.isoformat()}, requested {start.isoformat()}"
        )
    if last < end:
        raise ValueError(
            f"coverage ends at {last.isoformat()}, requested {end.isoformat()}"
        )
    if require_current:
        current = now or date.today()
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
    return bundle


def _issue(
    code: str,
    source: str,
    start: date,
    end: date,
    reason: object,
) -> DataReadinessIssue:
    return DataReadinessIssue(
        code=str(code),
        source=str(source or "point_in_time"),
        missing_start=start.isoformat(),
        missing_end=end.isoformat(),
        reason=str(reason),
    )


def _fundamental_dependencies(strategy) -> tuple[str, ...]:
    return tuple(
        getattr(strategy, "fundamental_feature_dependencies", ()) or ()
    ) if strategy is not None else ()


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
    provider = MarketHistoryProvider(config)
    tolerance = _coverage_tolerance(config)
    result = BacktestDataResult(str(purpose), start, end)
    requested_codes = [_code(item) for item in codes if _code(item)]
    requested_benchmarks = [
        _code(item)
        for item in benchmark_codes
        if _code(item) and _code(item) != "risk_free"
    ]
    all_codes = list(dict.fromkeys([*requested_codes, *requested_benchmarks]))

    for code in all_codes:
        loaded = None
        load_reason = "bundle is absent"
        try:
            loaded = store.read(code)
            if loaded is not None:
                loaded = validate_market_bundle(
                    loaded,
                    code,
                    start,
                    end,
                    require_current=require_current,
                    tolerance_days=tolerance,
                )
                result.bundles[code] = loaded
                result.reused_codes.append(code)
                continue
        except Exception as exc:
            load_reason = str(exc)

        try:
            fetched = provider.fetch(code, start, end)
            validate_market_bundle(
                fetched,
                code,
                start,
                end,
                require_current=require_current,
                tolerance_days=tolerance,
            )
            store.write(fetched)
            result.bundles[code] = fetched
            result.fetched_codes.append(code)
        except Exception as exc:
            reason = f"local={load_reason}; fetch={exc}"
            logger.warning("Backtest data is not ready for %s: %s", code, reason)
            result.issues.append(_issue(code, getattr(loaded, "source", ""), start, end, reason))

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
            instruments = result.fundamental_backfill.get("instruments", [])
            if isinstance(instruments, list):
                for item in instruments:
                    if not isinstance(item, dict):
                        continue
                    statement = item.get("statements", {}) or {}
                    if not isinstance(statement, dict):
                        statement = {}
                    status = str(statement.get("status", "missing"))
                    if status in {"success", "not_applicable"}:
                        continue
                    code = _code(item.get("code"))
                    if not code:
                        continue
                    result.issues.append(
                        _issue(
                            code,
                            "fundamental",
                            start,
                            end,
                            statement.get(
                                "reason",
                                f"fundamental statements are {status}",
                            ),
                        )
                    )
            elif result.fundamental_backfill.get("status") == "failed":
                reason = result.fundamental_backfill.get(
                    "reason", "fundamental backfill failed"
                )
                for code in requested_codes:
                    result.issues.append(
                        _issue(code, "fundamental", start, end, reason)
                    )
        except Exception as exc:
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
