"""Synthetic, offline 24-month gate and all-three pointer transaction contracts."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.experiments import technical_trial as trial

DATES = pd.bdate_range("2024-01-01", "2025-12-31").strftime("%Y-%m-%d").tolist()
PERIOD = {"start": "2024-01-01", "end_exclusive": "2026-01-01"}
CODES = {"a_share": "601088", "hk": "00700", "us": "VOO"}


def pure_receipt():
    source = {"synthetic-source": "a" * 64}
    contract = {
        "period": PERIOD,
        "source_files": source,
        "source_sha256": trial._hash(source),
        "selection_sha256": "b" * 64,
        "incumbent_pointer_sha256": "c" * 64,
        "markets": {},
        "selection_qualified_by_market": {group: True for group in trial.MARKETS},
        "safety": {"max_drawdown_floor_pct": -40.0, "minimum_trade_count": 1},
        "candidate": {},
        "incumbent": {},
    }
    reports = {"candidate": {}, "incumbent": {}}
    for group in trial.MARKETS:
        common = {
            "symbols": [CODES[group]],
            "expected_sessions": DATES,
            "initial_capital": 100000.0,
            "data_sha256": "d" * 64,
            "execution_sha256": "e" * 64,
            "universe_sha256": trial._hash([CODES[group]]),
        }
        contract["markets"][group] = common
        for role, final in (("candidate", 103000.0), ("incumbent", 102000.0)):
            identity = {
                "params_sha256": trial._hash(role + group),
                "artifact_sha256": trial._hash(role + "artifact" + group),
            }
            contract[role][group] = identity
            nav = np.linspace(100000.0, final, len(DATES)).tolist()
            reports[role][group] = {
                "market": group,
                "role": role,
                "period": PERIOD,
                "symbols": [CODES[group]],
                "nav_dates": DATES,
                "raw_nav": nav,
                "initial_capital": 100000.0,
                "net_return_pct": (final / 100000.0 - 1) * 100,
                "max_drawdown_pct": -1.0,
                "sharpe_ratio": 1.0,
                "total_trades": 4,
                "evaluation_count": 1,
                "evaluation_kind": "post_selection_continuous_24m",
                "market_input_sha256": "f" * 64,
                "source_sha256": contract["source_sha256"],
                "selection_sha256": contract["selection_sha256"],
                **{
                    key: common[key]
                    for key in ("data_sha256", "execution_sha256", "universe_sha256")
                },
                **identity,
            }
    return contract, reports


def gate(contract, reports):
    return trial.check_trial_gate(
        reports["candidate"], reports["incumbent"], expected_contract=contract
    )


def test_gate_compares_all_three_incumbents_with_distinct_artifacts_not_indices():
    contract, reports = pure_receipt()
    for role in reports:
        for report in reports[role].values():
            report["benchmark_returns"] = {"ignored_index": float("nan")}
    decision = gate(contract, reports)
    assert decision["passed"] and decision["activation_groups"] == list(trial.MARKETS)
    assert (
        contract["candidate"]["hk"]["artifact_sha256"]
        != contract["incumbent"]["hk"]["artifact_sha256"]
    )


@pytest.mark.parametrize("final", [100000.0, 99000.0, 102000.0, 101000.0])
def test_any_market_nonpositive_tied_or_losing_blocks_entire_switch(final):
    contract, reports = pure_receipt()
    row = reports["candidate"]["us"]
    row["raw_nav"][-1] = final
    row["net_return_pct"] = (final / 100000.0 - 1) * 100
    decision = gate(contract, reports)
    assert not decision["passed"] and decision["activation_groups"] == []


@pytest.mark.parametrize(
    "role,key,value",
    [
        ("candidate", "net_return_pct", float("nan")),
        ("incumbent", "sharpe_ratio", float("inf")),
        ("candidate", "total_trades", True),
        ("incumbent", "data_sha256", "0" * 64),
        ("candidate", "execution_sha256", "0" * 64),
        ("candidate", "source_sha256", "0" * 64),
        ("incumbent", "market_input_sha256", "0" * 64),
        ("candidate", "evaluation_count", 2),
        ("candidate", "evaluation_kind", "stitched_walk_forward"),
        ("candidate", "params_sha256", "0" * 64),
    ],
)
def test_invalid_metrics_identity_or_reselection_never_allows_partial_activation(
    role, key, value
):
    contract, reports = pure_receipt()
    reports[role]["hk"][key] = value
    result = gate(contract, reports)
    assert not result["passed"] and result["activation_groups"] == []


@pytest.mark.parametrize("role", ["candidate", "incumbent"])
def test_missing_market_is_not_silently_dropped(role):
    contract, reports = pure_receipt()
    del reports[role]["hk"]
    assert not gate(contract, reports)["passed"]


def test_overlapping_nine_month_nav_and_missing_dates_are_not_a_continuous_replay():
    for transform in (
        lambda values: values[:190] + values[60:250] + values[120:],
        lambda values: values[:50] + values[51:],
    ):
        contract, reports = pure_receipt()
        row = reports["candidate"]["hk"]
        row["nav_dates"] = transform(list(DATES))
        row["raw_nav"] = [100001.0] * len(row["nav_dates"])
        assert not gate(contract, reports)["passed"]


def test_twelve_months_cannot_masquerade_as_twenty_four():
    contract, reports = pure_receipt()
    contract["period"] = {"start": "2025-01-01", "end_exclusive": "2026-01-01"}
    assert not gate(contract, reports)["passed"]


@pytest.mark.parametrize(
    "key,value", [("total_trades", 0), ("max_drawdown_pct", -40.01)]
)
def test_relative_promotion_never_overrides_no_trade_or_catastrophic_safety(key, value):
    contract, reports = pure_receipt()
    reports["candidate"]["hk"][key] = value
    assert not gate(contract, reports)["passed"]


def test_unqualified_raw_best_candidate_cannot_use_replay_as_a_selection_objective():
    contract, reports = pure_receipt()
    contract["selection_qualified_by_market"]["hk"] = False
    assert not gate(contract, reports)["passed"]


def _artifact(root, run_id, group, level, eligible=True):
    from src.data.corporate_action_currency import CORPORATE_ACTION_CURRENCY_CONTRACT
    from src.strategy import Params, get_strategy

    directory = root / "runs" / run_id
    directory.mkdir(parents=True)
    artifact = directory / "params.yaml"
    config_hash = trial._hash(group)
    body = {
        "schema_version": 2,
        "group": group,
        "strategy_id": "percentile",
        "solver_id": "random",
        "gate_profile": "standard",
        "market_config_hash": config_hash,
        "params": {"trial_fixture_level": level},
        "execution": {"model": "cash_cap"},
        "execution_contracts": {
            "currency_cash_version": CORPORATE_ACTION_CURRENCY_CONTRACT
        },
        "activation": {
            "eligible": eligible,
            "holdout_passed": eligible,
            "benchmarks_complete": True,
            "universe_robustness_passed": False,
        },
        "holdout_windows": [{"majority_benchmark_excess": 1.0 if eligible else -1.0}],
    }
    artifact.write_text(yaml.safe_dump(body), encoding="utf-8")
    entry = {
        "group": group,
        "run_id": run_id,
        "artifact": artifact.relative_to(root).as_posix(),
        "strategy": "percentile",
        "solver_id": "random",
        "gate_profile": "standard",
        "config_hash": config_hash,
    }
    manifest = {
        "schema_version": 4,
        "run_id": run_id,
        "market_group": group,
        "activated": level == 0,
        "candidate": level != 0,
        "groups": {group: entry},
    }
    (directory / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return trial.ReplaySpec(
        get_strategy("percentile"),
        Params(
            {"trial_fixture_level": level},
            _engine="percentile",
            execution_snapshot={"model": "cash_cap"},
        ),
        artifact,
    ), entry


@pytest.fixture
def replay_env(tmp_path, monkeypatch):
    from src.backtest.execution import ExecutionPriceSlice
    from src.data.market_history import PriceHistoryBundle
    from src.search.config import ExecutionConfig
    from src.strategy import PortfolioTrace, StrategyMarketData, TradePlan

    root = tmp_path / "optimizer"
    candidate, incumbent, entries, run_ids, inputs = {}, {}, {}, {}, {}
    for group, code in CODES.items():
        candidate[group], _ = _artifact(root, "new_" + group, group, 1)
        incumbent[group], entries[group] = _artifact(root, "old_" + group, group, 0)
        run_ids[group] = "new_" + group
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(DATES),
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.0,
                "volume": 1000.0,
            }
        )
        pit = pd.DataFrame(
            {
                "date": pd.to_datetime(DATES),
                **{
                    prefix + field: value
                    for prefix in ("raw_", "qfq_")
                    for field, value in (
                        ("open", 10.0),
                        ("high", 11.0),
                        ("low", 9.0),
                        ("close", 10.0),
                    )
                },
                "qfq_factor": 1.0,
                "volume": 1000.0,
                "tradable": True,
            }
        )
        inputs[group] = trial.TrialMarketInput(
            [code],
            {code: frame},
            {
                code: PriceHistoryBundle(
                    code, pit, source="explicit_synthetic_unit_fixture"
                )
            },
            ExecutionConfig(),
            DATES,
        )
    pointer = root / "latest_strategy.yaml"
    pointer.write_text(
        yaml.safe_dump({"schema_version": 4, "activated": True, "groups": entries}),
        encoding="utf-8",
    )
    calls, profits = [], {group: {0: 1000.0, 1: 2000.0001} for group in trial.MARKETS}

    def build(stocks, symbols, strategy, params, start, end, **kwargs):
        assert start == PERIOD["start"] and end == "2025-12-31"
        assert set(kwargs["market_bundles"]) == set(symbols)
        shape = (len(DATES), len(symbols))
        matrix = np.ones((*shape, 1), dtype=np.float32)
        prices = np.full(shape, 10.0, dtype=np.float32)
        market = StrategyMarketData(
            matrix,
            dates=sim_dates.copy(),
            symbols=list(symbols),
            prices=prices,
            highs=prices + 1,
            lows=prices - 1,
            tradable=np.ones(shape, dtype=bool),
        )
        plan = TradePlan(
            np.zeros(shape, bool),
            np.zeros(shape, bool),
            prices.copy(),
            prices.copy(),
            10000,
            10000,
            1,
            dates=sim_dates.copy(),
            symbols=list(symbols),
            strategy_metadata={"fixture_level": params.values["trial_fixture_level"]},
        )
        return (
            plan,
            market,
            ExecutionPriceSlice(prices, prices + 1, prices - 1, np.ones(shape, bool)),
            list(symbols),
        )

    class SimulateOnly:
        def __init__(self, execution, group):
            self.execution, self.group = execution, group

        def simulate(self, plan, market, execution_prices):
            level = plan.strategy_metadata["fixture_level"]
            calls.append(
                (
                    self.group,
                    level,
                    self.execution.commission_rate,
                    self.execution.withholding_rates[self.group],
                )
            )
            nav = np.linspace(
                self.execution.initial_capital,
                self.execution.initial_capital + profits[self.group][level],
                len(DATES),
            )
            return PortfolioTrace(
                nav,
                list(DATES),
                4,
                20,
                -1,
                1,
                0.0,
                20,
                [],
                list(market.symbols),
                final_cash=100.0,
            )

    sim_dates = list(DATES)
    monkeypatch.setattr(trial, "build_trade_plan", build)
    monkeypatch.setattr(trial, "Backtester", SimulateOnly)

    def run(output="trial"):
        return trial.run_continuous_trial(
            inputs,
            candidate,
            incumbent,
            start_date=PERIOD["start"],
            end_exclusive=PERIOD["end_exclusive"],
            incumbent_pointer_sha256=trial.file_sha256(pointer),
            selection_id="fixed-after-original-ranking-and-validation",
            selection_qualified_by_market={group: True for group in trial.MARKETS},
            output_dir=tmp_path / output,
        )

    return {
        "root": root,
        "pointer": pointer,
        "candidate": candidate,
        "incumbent": incumbent,
        "inputs": inputs,
        "run_ids": run_ids,
        "calls": calls,
        "profits": profits,
        "run": run,
        "sim_dates": sim_dates,
    }


def test_runner_simulates_once_per_market_and_role_with_raw_unrounded_nav(replay_env):
    env = replay_env
    receipt = env["run"]()
    assert receipt["gate"]["passed"]
    assert (
        len(env["calls"]) == 6 and len({(row[0], row[1]) for row in env["calls"]}) == 6
    )
    row = receipt["candidate"]["us"]
    assert row["net_return_pct"] > 2.0 and row["net_return_pct"] != round(
        row["net_return_pct"], 2
    )
    assert len(row["raw_nav"]) == len(DATES)
    assert (
        receipt["contract"]["candidate"]["us"]["artifact_sha256"]
        != receipt["contract"]["incumbent"]["us"]["artifact_sha256"]
    )
    before = len(env["calls"])
    with pytest.raises(FileExistsError):
        env["run"]()
    assert len(env["calls"]) == before


def test_missing_symbol_or_pit_bundle_fails_before_any_simulation(replay_env):
    env = replay_env
    env["inputs"]["hk"].market_bundles.clear()
    with pytest.raises(ValueError, match="incomplete"):
        env["run"]()
    assert env["calls"] == []


def test_runner_does_not_accept_a_shortened_plan_date_axis(replay_env):
    env = replay_env
    env["sim_dates"].pop(20)
    with pytest.raises(ValueError, match="continuous_trade_plan"):
        env["run"]()
    assert env["calls"] == []


def activate(env, receipt):
    return trial.activate_trial(
        receipt,
        env["root"],
        env["run_ids"],
        receipt["contract"]["incumbent_pointer_sha256"],
        exclusive_owner=True,
    )


def test_third_market_failure_never_activates_first_two(replay_env):
    env = replay_env
    env["profits"]["us"][1] = -1.0
    original = env["pointer"].read_bytes()
    result = activate(env, env["run"]())
    assert not result["activated"] and env["pointer"].read_bytes() == original


def test_third_candidate_original_validation_failure_does_not_change_pointer(
    replay_env,
):
    env = replay_env
    path = Path(env["candidate"]["us"].artifact_path)
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value["activation"]["benchmarks_complete"] = False
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    receipt, original = env["run"](), env["pointer"].read_bytes()
    result = activate(env, receipt)
    assert not result["activated"] and "benchmark data incomplete" in str(result)
    assert env["pointer"].read_bytes() == original


def test_success_changes_all_three_entries_once_and_keeps_pinned_runs_readable(
    replay_env, monkeypatch
):
    from src.search.artifacts import load_strategy_run

    env, writes = replay_env, []
    receipt = env["run"]()
    real = trial.os.replace

    def replace(source, target):
        if Path(target) == env["pointer"]:
            writes.append(yaml.safe_load(Path(source).read_text(encoding="utf-8")))
        return real(source, target)

    monkeypatch.setattr(trial.os, "replace", replace)
    result = activate(env, receipt)
    assert result["activated"] and result["notes"] == []
    assert len(writes) == 1 and set(writes[0]["groups"]) == set(trial.MARKETS)
    for group in trial.MARKETS:
        assert writes[0]["groups"][group]["run_id"] == env["run_ids"][group]
        assert (
            load_strategy_run(env["run_ids"][group], groups=(group,), root=env["root"])
            is not None
        )


def test_raw_holdout_index_underperformance_is_preserved_but_not_an_extra_trial_gate(
    replay_env,
):
    env = replay_env
    for spec in env["candidate"].values():
        path = Path(spec.artifact_path)
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        value["activation"].update(
            eligible=False, holdout_passed=False, universe_robustness_passed=False
        )
        value["holdout_windows"] = [{"majority_benchmark_excess": -10.0}]
        path.write_text(yaml.safe_dump(value), encoding="utf-8")
    original = {
        group: Path(spec.artifact_path).read_bytes()
        for group, spec in env["candidate"].items()
    }
    receipt = env["run"]()
    result = activate(env, receipt)
    assert result["activated"]
    pointer = yaml.safe_load(env["pointer"].read_text(encoding="utf-8"))
    for group, entry in pointer["groups"].items():
        promoted = yaml.safe_load(
            (env["root"] / entry["artifact"]).read_text(encoding="utf-8")
        )
        assert promoted["activation"]["eligible"] is True
        assert promoted["activation"]["relative_promotion_passed"] is True
        assert promoted["activation"]["absolute_eligible"] is False
        assert promoted["activation"]["holdout_passed"] is False
        assert promoted["activation"]["universe_robustness_passed"] is False
        assert promoted["holdout_windows"] == [{"majority_benchmark_excess": -10.0}]
        assert (
            Path(env["candidate"][group].artifact_path).read_bytes() == original[group]
        )


def test_pointer_changed_after_validation_is_not_overwritten(replay_env, monkeypatch):
    env, real = replay_env, trial._validated_candidate
    receipt = env["run"]()
    replacement = b"external-writer-wins\n"

    def validate(*args):
        result = real(*args)
        if args[2] == "us":
            env["pointer"].write_bytes(replacement)
        return result

    monkeypatch.setattr(trial, "_validated_candidate", validate)
    result = activate(env, receipt)
    assert not result["activated"] and env["pointer"].read_bytes() == replacement


def test_annotation_failure_reports_committed_batch_and_never_rolls_back_one_market(
    replay_env, monkeypatch
):
    env = replay_env
    receipt, real = env["run"](), trial.os.replace

    def replace(source, target):
        if Path(target).name == "manifest.yaml":
            raise PermissionError("synthetic annotation failure")
        return real(source, target)

    monkeypatch.setattr(trial.os, "replace", replace)
    result = activate(env, receipt)
    assert result["activated"] and len(result["notes"]) == 3
    pointer = yaml.safe_load(env["pointer"].read_text(encoding="utf-8"))
    assert {
        group: entry["run_id"] for group, entry in pointer["groups"].items()
    } == env["run_ids"]


def test_activation_requires_exclusive_owner_and_unchanged_evidence(replay_env):
    env = replay_env
    receipt = env["run"]()
    with pytest.raises(ValueError, match="exclusive"):
        trial.activate_trial(
            receipt,
            env["root"],
            env["run_ids"],
            receipt["contract"]["incumbent_pointer_sha256"],
        )
    original = env["pointer"].read_bytes()
    report = Path(receipt["output_dir"]) / "hk_candidate_report.json"
    report.write_bytes(b"tampered")
    assert not activate(env, receipt)["activated"]
    assert env["pointer"].read_bytes() == original
