"""Causal CAPM-DCF value-policy context for the unified strategy pipeline.

The calibrator selects a policy on a broad, completed universe.  This module
applies that frozen policy to a receiving stock pool without reading the
receiving pool's future prices.  It turns each dated valuation into a daily
fundamental panel so the regular ``TradingStrategy -> TradePlan -> Backtester``
path remains the only execution path.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from src.data.market_history import PointInTimeMarketStore, PriceHistoryBundle
from src.fundamental_embedding.dcf_entry_calibration import (
    DCF_ENTRY_CALIBRATION_CONTRACT,
    CapmDcfEntryCalibrator,
    CapmDcfEntryConfig,
    CapmDcfEntryParameters,
)
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)

from .api import StrategyMarketData
from .fundamental_context import FundamentalStrategyMarketData

CAPM_DCF_VALUE_CONTEXT_CONTRACT = "capm-dcf-value-context-1"
CAPM_DCF_VALUE_PANEL_CONTRACT = "capm-dcf-value-panel-1"
CAPM_DCF_VALUE_FEATURE_NAMES = (
    "value:fair_price",
    "value:entry_price",
    "value:entry_fraction",
    "value:company_beta",
    "value:pool_beta",
)


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(frozen=True)
class CapmDcfValuePolicy:
    """A passed broad-universe policy with its causal availability boundary."""

    parameters: dict[str, CapmDcfEntryParameters]
    available_from: date
    beta_reference: float
    beta_reference_method: str
    source_report: str
    source_hash: str

    @classmethod
    def from_report(
        cls,
        path: str | Path,
        *,
        expected_market: str | None = None,
    ) -> CapmDcfValuePolicy:
        source = Path(path)
        raw = source.read_bytes()
        report = json.loads(raw.decode("utf-8"))
        if report.get("contract") != DCF_ENTRY_CALIBRATION_CONTRACT:
            raise ValueError("frozen value policy has an unknown DCF contract")
        report_market = str((report.get("dataset") or {}).get("market") or "")
        if expected_market and report_market != str(expected_market):
            raise ValueError(
                "frozen value policy market does not match receiver policy: "
                f"expected={expected_market}; actual={report_market or 'missing'}"
            )
        if not report.get("acceptance", {}).get(
            "candidate_eligible_for_manual_strategy_experiment"
        ):
            raise ValueError("frozen value policy did not pass its holdout gate")
        if _canonical_json(report.get("config")) != _canonical_json(
            asdict(CapmDcfEntryConfig())
        ):
            raise ValueError(
                "frozen value policy uses another economic/config contract"
            )
        raw_parameters = report.get("selection", {}).get("parameters", {})
        if not isinstance(raw_parameters, Mapping) or not raw_parameters:
            raise ValueError("frozen value policy has no enabled valuation route")
        parameters = {
            str(model): CapmDcfEntryParameters.from_dict(value)
            for model, value in raw_parameters.items()
            if isinstance(value, Mapping)
        }
        if not parameters:
            raise ValueError("frozen value policy has no valid route parameters")
        available_raw = report.get("dataset", {}).get("validation_start")
        if not available_raw:
            raise ValueError("frozen value policy has no availability date")
        beta_reference = report.get("selection", {}).get("policy_beta_reference")
        if not isinstance(beta_reference, Mapping):
            raise TypeError(
                "frozen value policy lacks a train-only beta reference; "
                "regenerate the broad policy report"
            )
        beta = float(beta_reference.get("value", 0.0))
        if not np.isfinite(beta) or beta <= 0:
            raise ValueError("frozen value policy beta reference is invalid")
        return cls(
            parameters=parameters,
            available_from=_as_date(available_raw),
            beta_reference=beta,
            beta_reference_method=str(beta_reference.get("method", "")),
            source_report=str(source.resolve()),
            source_hash=sha256(raw).hexdigest(),
        )


@dataclass(frozen=True)
class CapmDcfValueContextConfig:
    """Fixed policy-application inputs, not receiver-pool search parameters."""

    equity_risk_premium: float = 0.06
    beta_margin_gamma: float = 0.32
    minimum_entry_fraction: float = 0.75
    maximum_entry_fraction: float = 0.95
    maximum_snapshot_age_days: int = 550

    def validate(self) -> CapmDcfValueContextConfig:
        if not 0.0 < self.equity_risk_premium < 0.20:
            raise ValueError("equity_risk_premium must be between zero and 20%")
        if not 0.0 <= self.beta_margin_gamma <= 2.0:
            raise ValueError("beta_margin_gamma must be between zero and two")
        if not 0.0 < self.minimum_entry_fraction <= self.maximum_entry_fraction:
            raise ValueError("entry-fraction range is invalid")
        if self.maximum_entry_fraction > 1.0:
            raise ValueError("entry fraction cannot exceed fair value")
        if int(self.maximum_snapshot_age_days) < 1:
            raise ValueError("maximum_snapshot_age_days must be positive")
        return self


def beta_adjusted_entry_fraction(
    base_fraction: float,
    *,
    pool_beta: float,
    beta_reference: float,
    gamma: float,
    minimum: float,
    maximum: float,
) -> float:
    """Adjust margin of safety for pool risk without double-counting CAPM.

    Individual cost of equity remains ``rf + company_beta * ERP``.  This
    portfolio-level adjustment only changes how close to estimated fair value
    the strategy is willing to initiate a position.  A lower-beta pool can
    therefore move the base 0.85 fraction towards 0.95.
    """

    inputs = (base_fraction, pool_beta, beta_reference, minimum, maximum)
    if not all(np.isfinite(item) and item > 0 for item in inputs):
        raise ValueError("entry-fraction inputs must be finite and positive")
    if minimum > maximum or maximum > 1.0 or gamma < 0:
        raise ValueError("entry-fraction configuration is invalid")
    adjusted = float(base_fraction) * (float(pool_beta) / float(beta_reference)) ** (
        -float(gamma)
    )
    return float(np.clip(adjusted, minimum, maximum))


@dataclass(frozen=True)
class CapmDcfValueSnapshot:
    """One causal company valuation visible from ``feature_date`` onward."""

    feature_date: date
    symbol: str
    fair_price: float
    entry_price: float
    entry_fraction: float
    company_beta: float
    pool_beta: float
    valuation_model: str

    def values(self) -> tuple[float, ...]:
        return (
            self.fair_price,
            self.entry_price,
            self.entry_fraction,
            self.company_beta,
            self.pool_beta,
        )


@dataclass(frozen=True)
class CapmDcfValueContextEnricher:
    """Picklable daily-panel adapter for a frozen CAPM-DCF value policy."""

    snapshots: tuple[CapmDcfValueSnapshot, ...]
    policy: CapmDcfValuePolicy
    config: CapmDcfValueContextConfig
    skipped: Mapping[str, int]
    # DCF is calculated in raw per-share currency.  The shared portfolio
    # simulator uses a forward-adjusted price series so it can preserve wealth
    # across dividends and share actions without mutating historical shares.
    # Each `(date, factor)` maps raw price into that execution series.
    # Applying the same factor to both the observed price and the fair/entry
    # threshold preserves the raw-price decision exactly.
    price_scales: Mapping[str, tuple[tuple[date, float], ...]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        self.config.validate()
        seen: set[tuple[date, str]] = set()
        for snapshot in self.snapshots:
            key = (snapshot.feature_date, snapshot.symbol)
            if key in seen:
                raise ValueError(f"duplicate CAPM-DCF value snapshot: {key}")
            seen.add(key)
            if not all(np.isfinite(value) and value > 0 for value in snapshot.values()):
                raise ValueError("CAPM-DCF value snapshot contains an invalid value")
            if snapshot.entry_price > snapshot.fair_price:
                raise ValueError("entry price cannot exceed fair price")
        for symbol, history in self.price_scales.items():
            previous = date.min
            for scale_date, factor in history:
                if str(symbol) == "":
                    raise ValueError("CAPM-DCF price scale has an empty symbol")
                if scale_date < previous:
                    raise ValueError("CAPM-DCF price scales must be chronological")
                if not np.isfinite(factor) or factor <= 0:
                    raise ValueError("CAPM-DCF price scale must be positive")
                previous = scale_date

    @property
    def contract(self) -> str:
        return CAPM_DCF_VALUE_CONTEXT_CONTRACT

    @property
    def contract_hash(self) -> str:
        payload = {
            "contract": self.contract,
            "policy": {
                "source_hash": self.policy.source_hash,
                "available_from": self.policy.available_from.isoformat(),
                "beta_reference": self.policy.beta_reference,
            },
            "config": asdict(self.config),
            "snapshots": [
                {
                    "date": item.feature_date.isoformat(),
                    "symbol": item.symbol,
                    "values": item.values(),
                    "model": item.valuation_model,
                }
                for item in self.snapshots
            ],
            "skipped": dict(sorted(self.skipped.items())),
            "price_scales": {
                str(symbol): [
                    (scale_date.isoformat(), float(factor))
                    for scale_date, factor in history
                ]
                for symbol, history in sorted(self.price_scales.items())
            },
        }
        return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def __call__(
        self, market_data: StrategyMarketData
    ) -> FundamentalStrategyMarketData:
        dates = tuple(_as_date(item) for item in market_data.dates)
        symbols = tuple(str(item) for item in market_data.symbols)
        if any(left > right for left, right in zip(dates, dates[1:])):
            raise ValueError("strategy market dates must be chronological")
        if len(set(dates)) != len(dates) or len(set(symbols)) != len(symbols):
            raise ValueError("CAPM-DCF value context requires unique axes")
        values = np.zeros(
            (len(dates), len(symbols), len(CAPM_DCF_VALUE_FEATURE_NAMES)),
            dtype=np.float64,
        )
        mask = np.zeros_like(values, dtype=bool)
        source_dates = np.empty((len(dates), len(symbols)), dtype=object)
        source_dates[:, :] = None
        by_symbol: dict[str, list[CapmDcfValueSnapshot]] = {
            symbol: [] for symbol in symbols
        }
        for snapshot in self.snapshots:
            if snapshot.symbol in by_symbol:
                by_symbol[snapshot.symbol].append(snapshot)
        scale_by_symbol: dict[str, list[tuple[date, float]]] = {
            symbol: sorted(
                [
                    (_as_date(scale_date), float(factor))
                    for scale_date, factor in self.price_scales.get(symbol, ())
                ],
                key=lambda item: item[0],
            )
            for symbol in symbols
        }
        for column, symbol in enumerate(symbols):
            history = sorted(
                by_symbol[symbol], key=lambda item: item.feature_date
            )
            cursor = 0
            latest: CapmDcfValueSnapshot | None = None
            scale_history = scale_by_symbol[symbol]
            scale_cursor = 0
            latest_scale: float | None = None
            for row, market_date in enumerate(dates):
                while (
                    cursor < len(history)
                    and history[cursor].feature_date <= market_date
                ):
                    latest = history[cursor]
                    cursor += 1
                while (
                    scale_cursor < len(scale_history)
                    and scale_history[scale_cursor][0] <= market_date
                ):
                    latest_scale = scale_history[scale_cursor][1]
                    scale_cursor += 1
                if latest is None or latest_scale is None:
                    continue
                if (market_date - latest.feature_date).days > int(
                    self.config.maximum_snapshot_age_days
                ):
                    continue
                values[row, column] = (
                    latest.fair_price * latest_scale,
                    latest.entry_price * latest_scale,
                    latest.entry_fraction,
                    latest.company_beta,
                    latest.pool_beta,
                )
                mask[row, column] = True
                source_dates[row, column] = latest.feature_date
        panel = FundamentalStrategyMarketData(
            indicator_matrix=market_data.indicator_matrix,
            dates=list(market_data.dates),
            symbols=list(market_data.symbols),
            prices=market_data.prices,
            highs=market_data.highs,
            lows=market_data.lows,
            tradable=market_data.tradable,
            date_ordinals=market_data.date_ordinals,
            market=market_data.market,
            observation_counts=market_data.observation_counts,
            fundamental_features=values,
            fundamental_availability_mask=mask,
            fundamental_feature_names=CAPM_DCF_VALUE_FEATURE_NAMES,
            fundamental_feature_contract=CAPM_DCF_VALUE_PANEL_CONTRACT,
            fundamental_as_of_dates=source_dates,
            fundamental_historical_walk_forward_eligible=True,
        )
        return panel.validate_fundamental_panel()


def build_capm_dcf_value_context_enricher(
    *,
    data_root: str | Path,
    benchmark_bundle: PriceHistoryBundle,
    risk_free_rates: Mapping[date, float],
    industry_history: IndustryClassificationHistoryStore | None,
    frozen_policy_report: str | Path,
    market: str,
    market_currency: str,
    currency_conversion_bundles: Mapping[str, PriceHistoryBundle] | None,
    symbols: Iterable[str],
    config: CapmDcfValueContextConfig | None = None,
) -> CapmDcfValueContextEnricher:
    """Build a receiver-pool context from a pre-approved broad policy.

    Receiver-pool prices after the snapshot date are never read.  The pool's
    contemporaneous mean beta only selects an entry margin; it does not select
    policy parameters or modify individual CAPM discount rates.
    """

    settings = (config or CapmDcfValueContextConfig()).validate()
    policy = CapmDcfValuePolicy.from_report(
        frozen_policy_report, expected_market=market
    )
    calibrator = CapmDcfEntryCalibrator(
        data_root,
        benchmark_bundle,
        risk_free_rates,
        config=CapmDcfEntryConfig(),
        market=market,
        industry_history=industry_history,
        market_currency=market_currency,
        currency_conversion_bundles=currency_conversion_bundles,
    )
    episodes, skipped = calibrator.build_valuation_snapshots(symbols)
    eligible = [
        item for item in episodes if item.evaluation_date >= policy.available_from
    ]
    skipped = dict(skipped)
    skipped["before_frozen_policy_available"] = len(episodes) - len(eligible)
    beta_by_date: dict[date, float] = {}
    for evaluation_date in {item.evaluation_date for item in eligible}:
        values = np.asarray(
            [
                item.beta
                for item in eligible
                if item.evaluation_date == evaluation_date
                and np.isfinite(item.beta)
                and item.beta > 0
            ],
            dtype=np.float64,
        )
        if len(values):
            beta_by_date[evaluation_date] = float(np.mean(values))
    snapshots: list[CapmDcfValueSnapshot] = []
    price_scales: dict[str, tuple[tuple[date, float], ...]] = {}
    for symbol in sorted({str(item) for item in symbols}):
        bundle = calibrator._bundle(symbol)
        if bundle is None:
            skipped["market_history_missing"] = (
                skipped.get("market_history_missing", 0) + 1
            )
            continue
        dates = pd.to_datetime(bundle.prices["date"], errors="coerce")
        factors = pd.to_numeric(bundle.prices["qfq_factor"], errors="coerce")
        valid_scales = (
            dates.notna()
            & np.isfinite(factors)
            & (factors > 0)
        )
        price_scales[symbol] = tuple(
            (timestamp.date(), float(factor))
            for timestamp, factor in zip(
                dates[valid_scales], factors[valid_scales]
            )
        )
    for episode in eligible:
        pool_beta = beta_by_date.get(episode.evaluation_date)
        if pool_beta is None:
            skipped["pool_beta_unavailable"] = (
                skipped.get("pool_beta_unavailable", 0) + 1
            )
            continue
        row = calibrator._evaluate_episode(
            episode, policy.parameters, settings.equity_risk_premium
        )
        fair_price = row.get("fair_value")
        if not row.get("eligible") or fair_price is None:
            reason = str(row.get("reason") or "valuation_unavailable")
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        fair = float(fair_price)
        if not np.isfinite(fair) or fair <= 0:
            skipped["fair_value_invalid"] = skipped.get("fair_value_invalid", 0) + 1
            continue
        parameters = policy.parameters.get(episode.valuation_model.value)
        if parameters is None:
            skipped["valuation_route_not_supported_by_training_gate"] = (
                skipped.get("valuation_route_not_supported_by_training_gate", 0) + 1
            )
            continue
        fraction = beta_adjusted_entry_fraction(
            parameters.entry_fair_value_fraction,
            pool_beta=pool_beta,
            beta_reference=policy.beta_reference,
            gamma=settings.beta_margin_gamma,
            minimum=settings.minimum_entry_fraction,
            maximum=settings.maximum_entry_fraction,
        )
        snapshots.append(
            CapmDcfValueSnapshot(
                feature_date=episode.evaluation_date,
                symbol=episode.symbol,
                fair_price=fair,
                entry_price=fair * fraction,
                entry_fraction=fraction,
                company_beta=episode.beta,
                pool_beta=pool_beta,
                valuation_model=episode.valuation_model.value,
            )
        )
    return CapmDcfValueContextEnricher(
        snapshots=tuple(
            sorted(snapshots, key=lambda item: (item.feature_date, item.symbol))
        ),
        policy=policy,
        config=settings,
        skipped=skipped,
        price_scales=price_scales,
    )


def _load_benchmark_csv(path: Path) -> PriceHistoryBundle:
    """Load a real local benchmark cache for point-in-time beta estimation."""

    raw = pd.read_csv(path)
    required = {"date", "open", "high", "low", "close"}
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError("benchmark is missing columns: " + ", ".join(missing))
    volume_source = (
        raw["volume"]
        if "volume" in raw.columns
        else pd.Series(1.0, index=raw.index, dtype=np.float64)
    )
    volume = pd.to_numeric(volume_source, errors="coerce").fillna(0.0)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["date"], errors="coerce"),
            "raw_open": pd.to_numeric(raw["open"], errors="coerce"),
            "raw_high": pd.to_numeric(raw["high"], errors="coerce"),
            "raw_low": pd.to_numeric(raw["low"], errors="coerce"),
            "raw_close": pd.to_numeric(raw["close"], errors="coerce"),
            "volume": volume,
        }
    ).dropna(subset=["date", "raw_close"])
    for price_field in ("open", "high", "low", "close"):
        frame[f"qfq_{price_field}"] = frame[f"raw_{price_field}"]
    frame["qfq_factor"] = 1.0
    frame["tradable"] = frame["volume"] > 0
    return PriceHistoryBundle(
        code=path.stem,
        prices=frame.sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True),
        source=f"local_real_price_cache:{path.resolve()}",
        diagnostics=["beta benchmark cache excludes dividend adjustment"],
    ).validate()


def _load_benchmark_bundle(
    raw: Mapping[str, object],
    *,
    data_root: Path,
) -> PriceHistoryBundle:
    """Load an explicit causal beta benchmark without market defaults.

    A point-in-time market-store symbol is preferred for HK/US because it
    carries the same raw-price/action contract as the receiver universe.  The
    legacy A-share CSV remains supported so existing frozen experiments stay
    reproducible.
    """

    symbol = str(raw.get("benchmark_symbol") or "").strip()
    if symbol:
        bundle = PointInTimeMarketStore(data_root).read(symbol)
        if bundle is None:
            raise ValueError(
                "capm_dcf_value benchmark_symbol is absent from point-in-time "
                f"market store: {symbol}"
            )
        return bundle
    path = raw.get("benchmark_prices")
    if not path:
        raise ValueError(
            "capm_dcf_value requires benchmark_symbol or benchmark_prices"
        )
    return _load_benchmark_csv(Path(str(path)))


def _load_currency_conversion_bundles(
    raw: Mapping[str, object],
    *,
    data_root: Path,
) -> Mapping[str, PriceHistoryBundle]:
    """Read explicit source-currency to market-currency FX price histories."""

    declared = raw.get("currency_conversion_symbols", {})
    if declared is None:
        return {}
    if not isinstance(declared, Mapping):
        raise TypeError(
            "capm_dcf_value.currency_conversion_symbols must be a mapping"
        )
    store = PointInTimeMarketStore(data_root)
    result: dict[str, PriceHistoryBundle] = {}
    for currency, symbol in declared.items():
        source_currency = str(currency).upper().strip()
        fx_symbol = str(symbol).strip()
        if not source_currency or not fx_symbol:
            raise ValueError(
                "currency_conversion_symbols requires non-empty currency and symbol"
            )
        bundle = store.read(fx_symbol)
        if bundle is None:
            raise ValueError(
                "capm_dcf_value FX history is absent from point-in-time market "
                f"store: {fx_symbol}"
            )
        result[source_currency] = bundle
    return result


def _market_settings(
    raw: Mapping[str, object], market: str
) -> Mapping[str, object]:
    """Resolve an explicit market policy, retaining A-share legacy config.

    Policies cannot be shared implicitly across currencies or market regimes.
    A legacy flat payload is therefore valid only for the original A-share
    policy; HK and US must be named under ``markets``.
    """

    by_market = raw.get("markets")
    if by_market is None:
        if market == "a_share":
            return raw
        raise ValueError(
            "capm_dcf_value requires a dedicated market policy for "
            f"{market}; an A-share policy cannot be reused"
        )
    if not isinstance(by_market, Mapping):
        raise TypeError("capm_dcf_value.markets must be a mapping")
    selected = by_market.get(market)
    if not isinstance(selected, Mapping):
        raise ValueError(
            "capm_dcf_value has no configured causal policy for market: "
            f"{market}"
        )
    # Global settings only provide common *application* controls.  The data,
    # benchmark, curve and frozen-policy provenance are market-local.
    return {**raw, **selected}


def _value_settings(app_config: Mapping[str, object]) -> Mapping[str, object]:
    """Read the value-strategy block from the canonical optimizer config.

    Strategy context factories receive the full application configuration,
    while user-facing strategy settings live under ``optimizer``.  Accept a
    direct top-level block only for standalone/research callers; the nested
    block is the production path and must never be silently missed.
    """

    nested = app_config.get("optimizer", {})
    if nested is not None and not isinstance(nested, Mapping):
        raise TypeError("optimizer configuration must be a mapping")
    configured = (nested or {}).get("capm_dcf_value")
    if configured is None:
        configured = app_config.get("capm_dcf_value", {})
    if not isinstance(configured, Mapping):
        raise TypeError("capm_dcf_value configuration must be a mapping")
    return configured


def make_capm_dcf_value_context_from_config(
    app_config: Mapping[str, object],
    *,
    market: str,
    symbols: Iterable[str],
) -> CapmDcfValueContextEnricher:
    """Create the value context declared by ``config/config.yaml``.

    Values are intentionally explicit paths rather than hidden defaults.  A
    selected value strategy therefore fails closed if its causal data and
    frozen-policy evidence have not been installed on a machine.
    """

    raw = _value_settings(app_config)
    market_raw = _market_settings(raw, market)
    required = (
        "data_root",
        "risk_free_rates_json",
        "frozen_policy_report",
    )
    missing = [name for name in required if not market_raw.get(name)]
    if not market_raw.get("market_currency") and market != "a_share":
        missing.append("market_currency")
    if not (
        market_raw.get("benchmark_symbol")
        or market_raw.get("benchmark_prices")
    ):
        missing.append("benchmark_symbol|benchmark_prices")
    if missing:
        raise ValueError(
            "capm_dcf_value requires configured paths: " + ", ".join(missing)
        )
    rates_raw = json.loads(
        Path(str(market_raw["risk_free_rates_json"])).read_text(encoding="utf-8")
    )
    rate_values = rates_raw.get("risk_free_rates", rates_raw)
    if not isinstance(rate_values, Mapping):
        raise TypeError("capm_dcf_value risk-free source must be a mapping")
    rates = {_as_date(key): float(value) for key, value in rate_values.items()}
    settings = CapmDcfValueContextConfig(
        equity_risk_premium=float(market_raw.get("equity_risk_premium", 0.06)),
        beta_margin_gamma=float(market_raw.get("beta_margin_gamma", 0.32)),
        minimum_entry_fraction=float(
            market_raw.get("minimum_entry_fraction", 0.75)
        ),
        maximum_entry_fraction=float(
            market_raw.get("maximum_entry_fraction", 0.95)
        ),
        maximum_snapshot_age_days=int(
            market_raw.get("maximum_snapshot_age_days", 550)
        ),
    )
    data_root = Path(str(market_raw["data_root"]))
    industry_path = market_raw.get("industry_history")
    return build_capm_dcf_value_context_enricher(
        data_root=data_root,
        benchmark_bundle=_load_benchmark_bundle(market_raw, data_root=data_root),
        risk_free_rates=rates,
        industry_history=(
            IndustryClassificationHistoryStore(str(industry_path))
            if industry_path
            else None
        ),
        frozen_policy_report=Path(str(market_raw["frozen_policy_report"])),
        market=market,
        market_currency=str(market_raw.get("market_currency", "CNY")),
        currency_conversion_bundles=_load_currency_conversion_bundles(
            market_raw, data_root=data_root
        ),
        symbols=tuple(str(item) for item in symbols),
        config=settings,
    )
