"""One post-selection 24-calendar-month replay and one all-market switch.

This is an additional incumbent comparison, never a search objective or a new
independent OOS claim. Existing search/validation, budgets and index contracts
remain owned by their original modules. No data acquisition or notification
lives here. Activation requires an externally quiesced, exclusive owner: the
ordinary legacy market activator does not participate in this module's lock.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import math
import os
import re
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel

from ..backtest.engine import Backtester, build_trade_plan
from ..core.process_lock import exclusive_process_lock
from ..data.market_history import PriceHistoryBundle, corporate_action_issues
from ..markets import _detect_fine_group
from ..strategy import Params

MARKETS = ("a_share", "hk", "us")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ROOT = Path(__file__).resolve().parents[2]
_CORE_FILES = (
    "src/experiments/technical_trial.py",
    "src/backtest/engine.py",
    "src/backtest/execution.py",
    "src/backtest/in_kind.py",
    "src/strategy/api.py",
    "src/strategy/registry.py",
    "src/markets.py",
    "src/data/technical_indicators.py",
    "src/data/market_history.py",
    "src/data/corporate_action_currency.py",
    "src/data/in_kind_distributions.py",
    "src/data/in_kind_market_inputs.py",
    "src/data/price_adjustments.py",
    "config/optimizer_constraints.yaml",
    "config/promotion_policy.yaml",
)


@dataclass
class TrialMarketInput:
    symbols: Sequence[str]
    stocks_data: Mapping[str, pd.DataFrame]
    market_bundles: Mapping[str, PriceHistoryBundle]
    execution: object
    expected_sessions: Sequence[str]
    context_enricher: object = None


@dataclass
class ReplaySpec:
    strategy: object
    params: Params
    artifact_path: str | Path


def _canonical(value):
    """Lossless, deterministic input/audit encoding, including normal NaNs."""
    if isinstance(value, pd.DataFrame):
        return {
            "dataframe": {
                "columns": list(map(str, value.columns)),
                "dtypes": list(map(str, value.dtypes)),
                "index": _canonical(value.index.tolist()),
                "rows": _canonical(value.to_numpy().tolist()),
            }
        }
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            return {
                "array": _canonical(value.tolist()),
                "shape": list(value.shape),
                "dtype": str(value.dtype),
            }
        return {
            "array_bytes": base64.b64encode(
                np.ascontiguousarray(value).tobytes()
            ).decode("ascii"),
            "shape": list(value.shape),
            "dtype": str(value.dtype),
        }
    if isinstance(value, np.generic):
        return _canonical(value.item())
    if value is pd.NaT or value is pd.NA:
        return {"missing": str(value)}
    if isinstance(value, BaseModel):
        return _canonical(value.dict())
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("trial mappings require string keys")
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_input": str(value)}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError(f"unsupported trial value: {type(value).__name__}")


def _bytes(value) -> bytes:
    return json.dumps(
        _canonical(value),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def _hash(value) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha(value) -> bool:
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def _period(start_date, end_exclusive) -> dict:
    start, end = pd.Timestamp(start_date), pd.Timestamp(end_exclusive)
    if (
        start.tzinfo is not None
        or end.tzinfo is not None
        or start != start.normalize()
        or end != end.normalize()
        or start + pd.DateOffset(months=24) != end
    ):
        raise ValueError(
            "trial requires exactly 24 calendar months [start,end_exclusive)"
        )
    return {"start": str(start.date()), "end_exclusive": str(end.date())}


def _sessions(values, period) -> list[str]:
    dates = pd.DatetimeIndex(pd.to_datetime(list(values), errors="raise"))
    if (
        len(dates) < 2
        or dates.hasnans
        or dates.tz is not None
        or not dates.is_monotonic_increasing
        or dates.has_duplicates
        or not dates.equals(dates.normalize())
    ):
        raise ValueError("expected sessions must be unique and strictly increasing")
    if dates[0] < pd.Timestamp(period["start"]) or dates[-1] >= pd.Timestamp(
        period["end_exclusive"]
    ):
        raise ValueError("sessions fall outside the frozen period")
    # A supplied calendar axis may have holidays, never entire missing months.
    expected_months = pd.period_range(
        period["start"],
        pd.Timestamp(period["end_exclusive"]) - pd.Timedelta(days=1),
        freq="M",
    )
    if list(dates.to_period("M").unique()) != list(expected_months):
        raise ValueError("all 24 calendar months require verified market sessions")
    return dates.strftime("%Y-%m-%d").tolist()


def _execution(value, group) -> dict:
    names = (
        "initial_capital",
        "commission_rate",
        "min_holding_days",
        "lot_sizes",
        "fx_rates",
        "withholding_rates",
    )
    result = {name: deepcopy(getattr(value, name)) for name in names}
    for name in ("lot_sizes", "fx_rates", "withholding_rates"):
        if group not in result[name]:
            raise ValueError(f"{group}:missing execution {name}")
    numeric = [
        result["initial_capital"],
        result["commission_rate"],
        result["min_holding_days"],
        result["lot_sizes"][group],
        result["fx_rates"][group],
        result["withholding_rates"][group],
    ]
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(item)
        for item in numeric
    ):
        raise ValueError("execution requires finite numeric values")
    if (
        result["initial_capital"] <= 0
        or result["commission_rate"] < 0
        or result["min_holding_days"] < 0
        or result["lot_sizes"][group] < 1
        or result["fx_rates"][group] <= 0
        or not 0 <= result["withholding_rates"][group] <= 1
    ):
        raise ValueError("invalid execution/FX/tax contract")
    return result


def _identity(spec: ReplaySpec) -> dict:
    path = Path(spec.artifact_path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    from ..search.artifacts import _parse_params, execution_contract_compatibility

    strategy_id = str(spec.strategy.name)
    params = _parse_params(raw or {}, strategy_id)
    if (
        params is None
        or _hash(params) != _hash(spec.params)
        or not execution_contract_compatibility(raw, raw.get("group"))["compatible"]
    ):
        raise ValueError("replay params/strategy do not match the frozen artifact")
    return {
        "strategy_id": strategy_id,
        "params_sha256": _hash(spec.params),
        "artifact_sha256": file_sha256(path),
        "artifact_path": str(path),
    }


def _source_files(specs, additional) -> dict[str, str]:
    paths = {_ROOT / name for name in _CORE_FILES}
    paths.update(Path(path).resolve() for path in additional)
    for spec in specs:
        path = inspect.getsourcefile(type(spec.strategy))
        if not path:
            raise ValueError("strategy source file is not inspectable")
        paths.add(Path(path).resolve())
    return {str(path.resolve()): file_sha256(path) for path in sorted(paths, key=str)}


def _safety_contract() -> dict:
    from ..search.promotion import PromotionPolicy

    policy = PromotionPolicy.load(_ROOT / "config/promotion_policy.yaml")
    if (
        not math.isfinite(policy.safety_max_drawdown_pct)
        or policy.safety_max_drawdown_pct > 0
    ):
        raise ValueError("invalid existing relative-promotion safety policy")
    return {
        "max_drawdown_floor_pct": policy.safety_max_drawdown_pct,
        "minimum_trade_count": policy.minimum_trade_count,
    }


def check_trial_gate(
    candidate_by_market, incumbent_by_market, *, expected_contract
) -> dict:
    """Pure all-three incumbent gate. No index or majority criterion is used."""
    failures, comparisons = [], {}
    if set(candidate_by_market) != set(MARKETS) or set(incumbent_by_market) != set(
        MARKETS
    ):
        failures.append("incomplete_markets")
    try:
        period = _period(
            start_date=expected_contract["period"]["start"],
            end_exclusive=expected_contract["period"]["end_exclusive"],
        )
        if set(expected_contract["markets"]) != set(MARKETS):
            raise ValueError("incomplete expected markets")
        if (
            any(
                set(expected_contract[role]) != set(MARKETS)
                for role in ("candidate", "incumbent")
            )
            or not expected_contract["source_files"]
            or _hash(expected_contract["source_files"])
            != expected_contract["source_sha256"]
        ):
            raise ValueError("incomplete frozen identities")
        if set(expected_contract["selection_qualified_by_market"]) != set(
            MARKETS
        ) or any(
            value is not True
            for value in expected_contract["selection_qualified_by_market"].values()
        ):
            raise ValueError("candidate was not qualified before the 24-month replay")
        safety = expected_contract["safety"]
        if (
            not math.isfinite(safety["max_drawdown_floor_pct"])
            or safety["max_drawdown_floor_pct"] > 0
            or type(safety["minimum_trade_count"]) is not int
            or safety["minimum_trade_count"] < 0
        ):
            raise ValueError("invalid relative-promotion safety contract")
        if (
            not _sha(expected_contract["selection_sha256"])
            or not _sha(expected_contract["incumbent_pointer_sha256"])
            or not _sha(expected_contract["source_sha256"])
        ):
            raise ValueError("missing frozen identity")
    except (KeyError, TypeError, ValueError):
        return {
            "passed": False,
            "reasons": [*failures, "invalid_contract"],
            "comparisons": {},
            "activation_groups": [],
        }
    for group in MARKETS:
        try:
            common = expected_contract["markets"][group]
            dates = _sessions(common["expected_sessions"], period)
            if not common["symbols"] or len(common["symbols"]) != len(
                set(common["symbols"])
            ):
                raise ValueError("invalid universe")
            returns = {}
            market_hashes = []
            for role, reports in (
                ("candidate", candidate_by_market),
                ("incumbent", incumbent_by_market),
            ):
                report = reports[group]
                if (
                    report["market"] != group
                    or report["role"] != role
                    or report["period"] != period
                    or report["symbols"] != common["symbols"]
                    or report["nav_dates"] != dates
                ):
                    raise ValueError("period/universe/date_axis_mismatch")
                for key in ("data_sha256", "execution_sha256", "universe_sha256"):
                    if not _sha(common[key]) or report[key] != common[key]:
                        raise ValueError(key + "_mismatch")
                if (
                    report["source_sha256"] != expected_contract["source_sha256"]
                    or report["selection_sha256"]
                    != expected_contract["selection_sha256"]
                ):
                    raise ValueError("source_or_selection_mismatch")
                identity = expected_contract[role][group]
                for key in ("params_sha256", "artifact_sha256"):
                    if not _sha(identity[key]) or report[key] != identity[key]:
                        raise ValueError(role + "_" + key + "_mismatch")
                if (
                    type(report["evaluation_count"]) is not int
                    or report["evaluation_count"] != 1
                    or report["evaluation_kind"] != "post_selection_continuous_24m"
                ):
                    raise ValueError("not_a_single_post_selection_replay")
                if not _sha(report["market_input_sha256"]):
                    raise ValueError("missing_actual_market_input_hash")
                market_hashes.append(report["market_input_sha256"])
                nav = np.asarray(report["raw_nav"], dtype=float)
                initial = report["initial_capital"]
                metrics = [
                    report[key]
                    for key in (
                        "net_return_pct",
                        "max_drawdown_pct",
                        "sharpe_ratio",
                        "total_trades",
                    )
                ]
                if (
                    nav.ndim != 1
                    or len(nav) != len(dates)
                    or not np.isfinite(nav).all()
                    or (nav < 0).any()
                    or isinstance(initial, bool)
                    or not math.isfinite(initial)
                    or initial <= 0
                    or initial != common["initial_capital"]
                    or type(report["total_trades"]) is not int
                    or report["total_trades"] < 0
                    or any(
                        isinstance(value, bool) or not math.isfinite(value)
                        for value in metrics
                    )
                ):
                    raise ValueError("invalid_metrics")
                actual = float((nav[-1] / initial - 1) * 100)
                if not math.isclose(
                    actual, report["net_return_pct"], rel_tol=0, abs_tol=1e-10
                ):
                    raise ValueError("nav_return_mismatch")
                returns[role] = actual
                if role == "candidate":
                    with_initial = np.concatenate(([initial], nav))
                    raw_drawdown = float(
                        np.min(
                            (with_initial / np.maximum.accumulate(with_initial) - 1)
                            * 100
                        )
                    )
                    if (
                        min(raw_drawdown, report["max_drawdown_pct"])
                        < safety["max_drawdown_floor_pct"]
                        or report["total_trades"] < safety["minimum_trade_count"]
                    ):
                        raise ValueError("relative_promotion_safety_failed")
            if len(set(market_hashes)) != 1:
                raise ValueError("actual_market_input_mismatch")
            passed = (
                returns["candidate"] > 0 and returns["candidate"] > returns["incumbent"]
            )
            comparisons[group] = {
                "candidate_net_return_pct": returns["candidate"],
                "incumbent_net_return_pct": returns["incumbent"],
                "passed": passed,
            }
            if not passed:
                failures.append(group + ":not_positive_and_better_than_incumbent")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            failures.append(group + ":" + str(exc))
    return {
        "passed": not failures,
        "reasons": failures,
        "comparisons": comparisons,
        "activation_groups": list(MARKETS) if not failures else [],
    }


def _write_json(path, value) -> str:
    with Path(path).open("xb") as handle:
        handle.write(_bytes(value))
    return file_sha256(path)


def run_continuous_trial(
    inputs_by_market,
    candidate_by_market,
    incumbent_by_market,
    *,
    start_date,
    end_exclusive,
    incumbent_pointer_sha256,
    selection_id,
    selection_qualified_by_market,
    output_dir,
    source_files=(),
) -> dict:
    """Run exactly six canonical simulations; never select or activate anything.

    Supply expected_sessions from the already verified market calendar. Input
    preparation and original ranking/validation finish BEFORE this call.
    A consumed output directory cannot be reused with different finalists.
    """
    if any(
        set(mapping) != set(MARKETS)
        for mapping in (inputs_by_market, candidate_by_market, incumbent_by_market)
    ):
        raise ValueError("trial requires exactly a_share, hk and us")
    if (
        not isinstance(selection_id, str)
        or not selection_id.strip()
        or not _sha(incumbent_pointer_sha256)
    ):
        raise ValueError("fixed selection id and incumbent pointer SHA are required")
    if (
        not isinstance(selection_qualified_by_market, Mapping)
        or set(selection_qualified_by_market) != set(MARKETS)
        or any(value is not True for value in selection_qualified_by_market.values())
    ):
        raise ValueError("all candidates must be fixed qualified ranking selections")
    period = _period(start_date, end_exclusive)
    frozen = deepcopy(inputs_by_market)
    sources = _source_files(
        [*candidate_by_market.values(), *incumbent_by_market.values()], source_files
    )
    contract = {
        "period": period,
        "source_files": sources,
        "source_sha256": _hash(sources),
        "incumbent_pointer_sha256": incumbent_pointer_sha256,
        "selection_sha256": _hash(selection_id),
        "selection_qualified_by_market": dict(selection_qualified_by_market),
        "safety": _safety_contract(),
        "markets": {},
        "candidate": {},
        "incumbent": {},
    }
    for group in MARKETS:
        data = frozen[group]
        symbols = sorted(map(str, data.symbols))
        if (
            not symbols
            or len(symbols) != len(set(symbols))
            or set(data.stocks_data) != set(symbols)
            or set(data.market_bundles) != set(symbols)
            or any(_detect_fine_group(code) != group for code in symbols)
        ):
            raise ValueError(group + ":incomplete_or_wrong_market_universe")
        sessions = _sessions(data.expected_sessions, period)
        for code in symbols:
            frame, bundle = data.stocks_data[code], data.market_bundles[code]
            if (
                not isinstance(frame, pd.DataFrame)
                or frame.empty
                or "date" not in frame
                or "close" not in frame
                or not isinstance(bundle, PriceHistoryBundle)
                or bundle.code != code
            ):
                raise ValueError(group + ":missing_history:" + code)
            axis = pd.DatetimeIndex(pd.to_datetime(frame["date"], errors="raise"))
            if axis.has_duplicates or not axis.is_monotonic_increasing:
                raise ValueError(group + ":invalid_history_dates:" + code)
            bundle.validate()
            if corporate_action_issues(bundle.actions):
                raise ValueError(group + ":unresolved_corporate_actions:" + code)
        execution = _execution(data.execution, group)
        contract["markets"][group] = {
            "symbols": symbols,
            "expected_sessions": sessions,
            "data_sha256": _hash(
                {"stocks": data.stocks_data, "bundles": data.market_bundles}
            ),
            "execution_sha256": _hash(execution),
            "universe_sha256": _hash(symbols),
            "initial_capital": execution["initial_capital"],
        }
        contract["candidate"][group] = _identity(candidate_by_market[group])
        contract["incumbent"][group] = _identity(incumbent_by_market[group])
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    files = {
        "trial_inputs.json": _write_json(
            output / "trial_inputs.json",
            {
                "contract": contract,
                "markets": {
                    group: {
                        "stocks": frozen[group].stocks_data,
                        "bundles": frozen[group].market_bundles,
                        "execution": _execution(frozen[group].execution, group),
                    }
                    for group in MARKETS
                },
            },
        )
    }
    reports = {"candidate": {}, "incumbent": {}}
    for group in MARKETS:
        data, common = frozen[group], contract["markets"][group]
        for role, specs in (
            ("candidate", candidate_by_market),
            ("incumbent", incumbent_by_market),
        ):
            spec = specs[group]
            plan, market, prices, codes = build_trade_plan(
                data.stocks_data,
                common["symbols"],
                spec.strategy,
                spec.params,
                period["start"],
                str(
                    (
                        pd.Timestamp(period["end_exclusive"]) - pd.Timedelta(days=1)
                    ).date()
                ),
                context_enricher=data.context_enricher,
                market_bundles=data.market_bundles,
            )
            if (
                plan is None
                or market is None
                or codes != common["symbols"]
                or list(market.dates) != common["expected_sessions"]
            ):
                raise ValueError(group + ":incomplete_continuous_trade_plan")
            trace = Backtester(data.execution, group).simulate(plan, market, prices)
            nav = np.asarray(trace.daily_values, dtype=float)
            identity = contract[role][group]
            report = {
                "market": group,
                "role": role,
                "period": period,
                "symbols": common["symbols"],
                "nav_dates": list(trace.daily_dates),
                "raw_nav": nav.tolist(),
                "initial_capital": common["initial_capital"],
                "net_return_pct": float((nav[-1] / common["initial_capital"] - 1) * 100)
                if len(nav)
                else float("nan"),
                "max_drawdown_pct": trace.max_drawdown_pct,
                "sharpe_ratio": trace.sharpe_ratio,
                "total_trades": trace.total_trades,
                "evaluation_count": 1,
                "evaluation_kind": "post_selection_continuous_24m",
                "market_input_sha256": _hash(market),
                "source_sha256": contract["source_sha256"],
                "selection_sha256": contract["selection_sha256"],
                **{
                    key: common[key]
                    for key in ("data_sha256", "execution_sha256", "universe_sha256")
                },
                **{key: identity[key] for key in ("params_sha256", "artifact_sha256")},
            }
            audit = {
                "market_data": market,
                "trade_plan_not_actual_fills": plan,
                "execution_prices": prices,
                "raw_nav": nav,
                "trace": trace,
            }
            for suffix, value in (("report", report), ("audit", audit)):
                name = f"{group}_{role}_{suffix}.json"
                files[name] = _write_json(output / name, value)
            reports[role][group] = report
            if (
                _hash({"stocks": data.stocks_data, "bundles": data.market_bundles})
                != common["data_sha256"]
                or _hash(_execution(data.execution, group))
                != common["execution_sha256"]
            ):
                raise ValueError("frozen inputs changed during replay")
    if any(file_sha256(path) != digest for path, digest in sources.items()):
        raise ValueError("trial source changed during replay")
    for role, specs in (
        ("candidate", candidate_by_market),
        ("incumbent", incumbent_by_market),
    ):
        if any(_identity(specs[group]) != contract[role][group] for group in MARKETS):
            raise ValueError("selected artifact changed during replay")
    receipt = {
        "schema_version": 1,
        "label": "post-selection continuous replay; may overlap earlier ranking history, not independent OOS",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output),
        "contract": contract,
        "candidate": reports["candidate"],
        "incumbent": reports["incumbent"],
        "files": files,
        "gate": check_trial_gate(
            reports["candidate"], reports["incumbent"], expected_contract=contract
        ),
    }
    _write_json(output / "trial_receipt.json", receipt)
    return receipt


def _validated_candidate(root, run_id, group, identity):
    from ..search import artifacts as api

    if not isinstance(run_id, str) or Path(run_id).name != run_id:
        raise ValueError("invalid candidate run id")
    manifest = api._load_yaml(root / api.RUNS_DIRNAME / run_id / "manifest.yaml")
    entry = api._manifest_entry(manifest or {}, group)
    if not entry or manifest.get("market_group") != group:
        raise ValueError(group + ":invalid candidate manifest")
    path = api._artifact_path(root, str(entry["artifact"]))
    data = api._load_yaml(path) if path else None
    if (
        path is None
        or not data
        or not api._artifact_matches_entry(data, entry, group)
        or not api.execution_contract_compatibility(data, group)["compatible"]
    ):
        raise ValueError(group + ":incompatible candidate artifact")
    params = api._parse_params(data, str(entry["strategy"]))
    if (
        params is None
        or file_sha256(path) != identity["artifact_sha256"]
        or _hash(params) != identity["params_sha256"]
    ):
        raise ValueError(group + ":candidate identity changed")
    activation = data.get("activation", {})
    if (
        not isinstance(activation, dict)
        or activation.get("benchmarks_complete") is not True
    ):
        raise ValueError(group + ":original benchmark data incomplete")
    readiness = data.get("data_readiness") or {}
    if (
        not isinstance(readiness, dict)
        or readiness.get("ready") is False
        or readiness.get("issues")
    ):
        raise ValueError(group + ":original data readiness incomplete")
    return dict(entry)


def _promoted_entry(root, entry, receipt, group):
    """Stage a promoted COPY. Never rewrite the selected raw validation flags."""
    from ..search.artifacts import _artifact_path

    raw_path = _artifact_path(root, entry["artifact"])
    value = yaml.safe_load(raw_path.read_text(encoding="utf-8"))
    activation = value["activation"]
    activation["absolute_eligible"] = bool(activation.get("eligible"))
    activation["relative_promotion_passed"] = True
    activation["eligible"] = True
    value["promotion"] = {
        "passed": True,
        "comparison": "all3_post_selection_continuous_24m_vs_incumbent",
        "raw_selected_artifact_sha256": receipt["contract"]["candidate"][group][
            "artifact_sha256"
        ],
        "incumbent_artifact_sha256": receipt["contract"]["incumbent"][group][
            "artifact_sha256"
        ],
        "trial_receipt_sha256": file_sha256(
            Path(receipt["output_dir"]) / "trial_receipt.json"
        ),
        "comparisons": receipt["gate"]["comparisons"],
    }
    path = raw_path.with_name(
        raw_path.stem
        + ".trial_"
        + receipt["contract"]["selection_sha256"][:12]
        + ".yaml"
    )
    encoded = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError("staged promoted artifact already differs") from None
    return {**entry, "artifact": path.relative_to(root).as_posix()}


def _verify_evidence(receipt):
    output = Path(receipt["output_dir"]).resolve()
    disk = json.loads((output / "trial_receipt.json").read_text(encoding="utf-8"))
    if _hash(disk) != _hash(receipt):
        raise ValueError("trial receipt changed")
    required = {"trial_inputs.json"} | {
        f"{group}_{role}_{kind}.json"
        for group in MARKETS
        for role in ("candidate", "incumbent")
        for kind in ("report", "audit")
    }
    if set(receipt["files"]) != required:
        raise ValueError("incomplete trial evidence files")
    for name, digest in receipt["files"].items():
        if (
            Path(name).name != name
            or not _sha(digest)
            or file_sha256(output / name) != digest
        ):
            raise ValueError("trial evidence changed")
    inputs = json.loads((output / "trial_inputs.json").read_text(encoding="utf-8"))
    contract = receipt["contract"]
    if _hash(inputs["contract"]) != _hash(contract):
        raise ValueError("frozen input contract changed")
    required_sources = {str((_ROOT / name).resolve()) for name in _CORE_FILES}
    if not required_sources.issubset(contract["source_files"]):
        raise ValueError("incomplete runtime source evidence")
    for group in MARKETS:
        stored = inputs["markets"][group]
        common = contract["markets"][group]
        if (
            _hash({"stocks": stored["stocks"], "bundles": stored["bundles"]})
            != common["data_sha256"]
            or _hash(stored["execution"]) != common["execution_sha256"]
            or _hash(common["symbols"]) != common["universe_sha256"]
        ):
            raise ValueError("frozen data/execution/universe hash mismatch")
        for role in ("candidate", "incumbent"):
            report = json.loads(
                (output / f"{group}_{role}_report.json").read_text(encoding="utf-8")
            )
            audit = json.loads(
                (output / f"{group}_{role}_audit.json").read_text(encoding="utf-8")
            )
            if (
                _hash(report) != _hash(receipt[role][group])
                or _hash(audit["market_data"]) != report["market_input_sha256"]
            ):
                raise ValueError("report differs from replay evidence")
            raw = audit["raw_nav"]
            nav = np.frombuffer(
                base64.b64decode(raw["array_bytes"], validate=True), dtype=raw["dtype"]
            ).reshape(raw["shape"])
            if not np.array_equal(nav, np.asarray(report["raw_nav"], dtype=float)):
                raise ValueError("raw NAV audit mismatch")
    return output


def activate_trial(
    receipt,
    optimizer_root,
    candidate_run_ids,
    expected_pointer_sha256,
    *,
    exclusive_owner=False,
) -> dict:
    """One atomic pointer replacement, only under externally exclusive ownership.

    The caller MUST stop the normal service and ensure no optimizer/activator is
    running. This lock serializes this API only; it does not falsely claim to
    lock legacy writers. Run manifests are annotated only AFTER pointer commit;
    annotation failure cannot falsely report that the batch was not activated.
    """
    from ..search.artifacts import ACTIVE_SCHEMA_VERSION, LATEST_MANIFEST

    if exclusive_owner is not True:
        raise ValueError("activation requires explicit externally exclusive owner")
    contract = receipt["contract"]
    gate = check_trial_gate(
        receipt["candidate"], receipt["incumbent"], expected_contract=contract
    )
    if not gate["passed"] or set(candidate_run_ids) != set(MARKETS):
        return {
            "activated": False,
            "reasons": gate["reasons"] or ["incomplete_candidates"],
        }
    if expected_pointer_sha256 != contract["incumbent_pointer_sha256"] or not _sha(
        expected_pointer_sha256
    ):
        return {"activated": False, "reasons": ["incumbent_pointer_mismatch"]}
    root = Path(optimizer_root).resolve()
    pointer = root / LATEST_MANIFEST
    with exclusive_process_lock(root / ".technical_trial.activation.lock") as acquired:
        if not acquired:
            return {"activated": False, "reasons": ["activation_lock_busy"]}
        temporary = root / f".technical_trial.{uuid4().hex}.tmp"
        try:
            if file_sha256(pointer) != expected_pointer_sha256:
                raise ValueError("active pointer changed")
            output = _verify_evidence(receipt)
            for path, digest in contract["source_files"].items():
                if file_sha256(path) != digest:
                    raise ValueError("trial source changed")
            for identity in contract["incumbent"].values():
                if (
                    file_sha256(identity["artifact_path"])
                    != identity["artifact_sha256"]
                ):
                    raise ValueError("incumbent artifact changed")
            entries = {
                group: _validated_candidate(
                    root, candidate_run_ids[group], group, contract["candidate"][group]
                )
                for group in MARKETS
            }
            entries = {
                group: _promoted_entry(root, entry, receipt, group)
                for group, entry in entries.items()
            }
            now = datetime.now(timezone.utc).isoformat()
            for entry in entries.values():
                entry["activated_at"] = now
            manifest = {
                "schema_version": ACTIVE_SCHEMA_VERSION,
                "timestamp": now,
                "activated_at": now,
                "activated": True,
                "candidate": False,
                "groups": entries,
                "technical_trial": {
                    "selection_sha256": contract["selection_sha256"],
                    "receipt_sha256": file_sha256(output / "trial_receipt.json"),
                },
            }
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                yaml.safe_dump(manifest, handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            # Candidate validation/source reads may be slow: CAS again at commit.
            if file_sha256(pointer) != expected_pointer_sha256:
                raise ValueError("active pointer changed before commit")
            committed_sha256 = file_sha256(temporary)
            os.replace(temporary, pointer)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return {"activated": False, "reasons": [str(exc)]}
        finally:
            temporary.unlink(missing_ok=True)
        # The pointer is now committed for all three markets. Only bookkeeping
        # follows; never turn a later annotation exception into "not activated".
        notes = []
        for group in MARKETS:
            path = root / "runs" / candidate_run_ids[group] / "manifest.yaml"
            annotation = path.with_name(f".trial_annotation.{uuid4().hex}.tmp")
            try:
                value = yaml.safe_load(path.read_text(encoding="utf-8"))
                value.update(activated=True, candidate=False, activated_at=now)
                value["groups"] = {group: entries[group]}
                annotation.write_text(
                    yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                    newline="\n",
                )
                os.replace(annotation, path)
            except (OSError, ValueError, TypeError) as exc:
                notes.append(
                    group + ":run_manifest_annotation_pending:" + type(exc).__name__
                )
            finally:
                try:
                    annotation.unlink(missing_ok=True)
                except OSError:
                    notes.append(group + ":annotation_cleanup_pending")
        return {
            "activated": True,
            "activation_groups": list(MARKETS),
            "pointer_sha256": committed_sha256,
            "notes": notes,
        }
