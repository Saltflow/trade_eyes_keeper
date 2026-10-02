"""Research approval, isolation and shared execution contract regressions."""

from __future__ import annotations

import json
import shutil
import sys
import threading
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from src.data.backtest_data import BacktestDataResult, DataReadinessIssue
from src.data.dataset_catalog import DatasetCatalog
from src.data.market_history import PointInTimeMarketStore, PriceHistoryBundle
from src.interactive.assistant.research import (
    PreparationMemoryError,
    ResearchJobSpec,
    ResearchRunner,
    _evaluation_horizon,
    _json_write,
    _linux_memory_snapshot,
    _safe_path,
    _sha,
)
from src.interactive.assistant.research_worker import prepare_inputs, run_registered
from src.strategy import EvaluationReport


@pytest.fixture
def project(tmp_path, monkeypatch):
    # Preview/unit execution fixtures model the supported Linux deployment.
    # A separate test below exercises the real unsupported-platform boundary.
    monkeypatch.setattr(
        ResearchRunner, "_host_prepare_support", staticmethod(lambda: {"ready": True})
    )
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src/__init__.py").write_text("# trusted source\n", encoding="utf-8")
    (root / "config").mkdir()
    raw = {
        "stocks": ["600036"],
        "point_in_time_data": {"output_dir": "data/pit", "history_years": 9},
        "provider_access": {"baostock": {"state_dir": "data/provider_state/baostock"}},
        "instrument_catalog": {"600036": {"type": "equity"}},
        "instrument_audit": {"timeout_seconds": 17, "api_key": "fixture-audit-secret"},
        "llm": {"api_key": "fixture-secret-key", "model": "deepseek-flash"},
        "interactive": {"feishu": {"app_secret": "fixture-bot-secret"}},
        "optimizer": {
            "markets": {
                market: {
                    "strategy": "technical_ensemble",
                    "solver_id": "local_genetic",
                    "gate_profile": "standard",
                    "walk_forward_profile": f"{market}_84m",
                    "execution_profile": execution,
                    "benchmark_profile": market,
                }
                for market, execution in (
                    ("a_share", "a_share_cny"),
                    ("hk", "hk_hkd"),
                    ("us", "us_usd"),
                )
            }
        },
    }
    (root / "config/config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    (root / "config/.env").write_text(
        "DEEPSEEK_API_KEY=fixture-env-secret", encoding="utf-8"
    )
    (root / ".git").mkdir()
    (root / ".git/config").write_text("secret git metadata", encoding="utf-8")
    repo = Path(__file__).resolve().parents[2]
    for name in ("optimizer_constraints.yaml", "alerts.yaml"):
        shutil.copyfile(repo / "config" / name, root / "config" / name)
    return root


@pytest.fixture
def runner(project, monkeypatch):
    instance = ResearchRunner(project, {})
    monkeypatch.setattr(
        instance,
        "_resolve_spec",
        lambda spec, config: {
            "market": "a_share",
            "strategy_id": "justified_pb_value",
            "parameters": {},
            "execution_snapshot": {"model": "target_weight"},
            "benchmark_codes": ["510300"],
            "data_start": "2020-01-01",
            "fundamental_dependencies": [],
            "approval_files": [],
        },
    )
    return instance


def request(**changes):
    return {
        "script_id": "justified_pb_value",
        "codes": ["600036"],
        "start": "2021-01-01",
        "end": "2024-01-01",
        **changes,
    }


def proposal(runner, **changes):
    return runner.prepare(request(**changes), "abc123def456")


@pytest.mark.parametrize(
    "change",
    [
        {"script_id": "shell"},
        {"codes": ["../.env"]},
        {"codes": [600036]},
        {"codes": []},
        {"start": "yesterday"},
        {"end": "2020-01-01"},
        {"end": "2999-01-01"},
        {"command": "rm -rf /"},
        {"parameters": {"path": {"a": "b"}}},
        {"parameters": {"a": float("nan")}},
        {"generated_code": "print('unexpected')"},
        {"script_id": "generated"},
        {"script_id": "generated", "generated_code": "def ("},
    ],
)
def test_spec_rejects_invalid_or_operational_inputs(change):
    with pytest.raises((ValidationError, SyntaxError)):
        ResearchJobSpec.parse_obj(request(**change))


def test_spec_normalizes_codes_and_exposes_exact_schema():
    spec = ResearchJobSpec.parse_obj(request(codes=["voo", " VOO ", "BRK.B"]))
    assert spec.codes == ["VOO", "BRK.B"]
    assert ResearchJobSpec.schema()["additionalProperties"] is False


def test_prepare_freezes_source_without_executing_python_or_copying_secrets(
    runner, project
):
    marker = project / "must-not-exist"
    payload = proposal(
        runner,
        script_id="generated",
        generated_code=f"open({str(marker)!r}, 'w').write('bad')",
    )
    assert not marker.exists()
    assert payload["preview_files"]
    assert payload["preview"]["formal_acceptance"] is False
    text = "".join(
        path.read_text(encoding="utf-8")
        for path in Path(payload["job_dir"]).rglob("*")
        if path.is_file()
    )
    assert "fixture-secret" not in text
    assert "fixture-bot-secret" not in text
    assert "fixture-env-secret" not in text
    assert not list(Path(payload["job_dir"]).rglob(".env"))
    assert not list(Path(payload["job_dir"]).rglob(".git"))
    runner.check(payload)
    json.dumps(payload)


@pytest.mark.parametrize("filename", ["config/config.yaml", "src/__init__.py"])
def test_confirmation_rejects_changed_live_config_or_source(runner, project, filename):
    payload = proposal(runner)
    path = project / filename
    path.write_text(
        path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="重新预览"):
        runner.check(payload)
    runner._check_frozen(payload)  # Already approved queued jobs retain their inputs.


@pytest.mark.parametrize(
    "filename", ["input/research.py", "input/job.json", "host_config.json"]
)
def test_frozen_input_changes_are_rejected(runner, filename):
    payload = proposal(runner)
    (Path(payload["job_dir"]) / filename).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        runner._check_frozen(payload)


def test_undeclared_input_is_rejected(runner):
    payload = proposal(runner)
    (Path(payload["job_dir"]) / "input/code/evil.py").write_text(
        "pass", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="file set changed"):
        runner.check(payload)


def test_job_paths_and_duplicate_job_are_rejected(runner):
    with pytest.raises(ValueError):
        runner.prepare(request(), "../outside")
    proposal(runner)
    with pytest.raises(ValueError, match="already exists"):
        proposal(runner)


def test_real_market_resolution_requires_one_market_and_two_years(project):
    instance = ResearchRunner(project, {})
    config = yaml.safe_load(
        (project / "config/config.yaml").read_text(encoding="utf-8")
    )
    for spec, message in (
        (request(codes=["600036", "VOO"]), "按市场"),
        (request(start="2023-12-01"), "至少 24"),
    ):
        with pytest.raises(ValueError, match=message):
            instance._resolve_spec(ResearchJobSpec.parse_obj(spec), config)
    resolved = instance._resolve_spec(
        ResearchJobSpec.parse_obj(request(codes=["600036", "000333"])), config
    )
    assert resolved["strategy_id"] == "justified_pb_value"
    assert resolved["fundamental_dependencies"] == ["roe_ttm", "book_yield"]


def test_capm_requires_installed_policy_before_fetching(project):
    instance = ResearchRunner(project, {})
    with pytest.raises(ValueError, match="CAPM/DCF 缺少"):
        instance.prepare(request(script_id="capm_dcf_value"), "abc123def456")


def test_fundamental_preview_declares_shared_longer_backfill_range(project):
    instance = ResearchRunner(project, {})
    payload = instance.prepare(request(codes=["600036", "000333"]), "abc123def456")
    preparation = payload["preview"]["data_preparation"]
    expected_start = date(2024, 1, 1) - timedelta(days=int(9 * 365.25))
    assert preparation["fundamental_backfill_start"] == expected_start.isoformat()
    assert preparation["fundamental_backfill_end"] == "2024-01-01"
    assert preparation["history_years"] == 9
    assert preparation["market_backfill_check_start"] == expected_start.isoformat()
    assert preparation["container_price_start"] == preparation["start"]
    assert preparation["container_price_start"] > expected_start.isoformat()


def test_capm_input_preserves_actual_capital_cost_horizons(project, monkeypatch):
    monkeypatch.setattr(
        "src.fundamental_embedding.capital_cost.CapitalCostConfig",
        lambda: SimpleNamespace(beta_horizons_years=(2, 3, 7)),
    )
    config = yaml.safe_load(
        (project / "config/config.yaml").read_text(encoding="utf-8")
    )
    resolved = ResearchRunner(project, {})._resolve_spec(
        ResearchJobSpec.parse_obj(request(script_id="capm_dcf_value")), config
    )
    assert (
        resolved["data_start"]
        == (
            date.fromisoformat(resolved["history_lookback"]["state_start"])
            - timedelta(days=550 + round(7 * 365.25))
        ).isoformat()
    )
    assert resolved["history_lookback"]["beta_horizons_years"] == [2, 3, 7]
    assert resolved["history_lookback"]["beta_lookback_days"] == round(7 * 365.25)


@pytest.mark.parametrize("maximum_age", [550, 800])
def test_capm_history_preserves_beta_for_prior_quarter_and_oldest_valid_snapshot(
    project, maximum_age
):
    from src.fundamental_embedding.capital_cost import CapitalCostConfig

    config = yaml.safe_load(
        (project / "config/config.yaml").read_text(encoding="utf-8")
    )
    config["optimizer"]["capm_dcf_value"] = {
        "maximum_snapshot_age_days": 120,
        "markets": {"a_share": {"maximum_snapshot_age_days": maximum_age}},
    }
    spec = ResearchJobSpec.parse_obj(
        request(script_id="capm_dcf_value", start="2024-03-01", end="2026-03-01")
    )
    resolved = ResearchRunner(project, {})._resolve_spec(spec, config)
    lookback = resolved["history_lookback"]
    window = timedelta(
        days=round(max(CapitalCostConfig().beta_horizons_years) * 365.25)
    )
    oldest_snapshot = date.fromisoformat(lookback["state_start"]) - timedelta(
        days=maximum_age
    )
    frozen_start = date.fromisoformat(resolved["data_start"])
    assert frozen_start == oldest_snapshot - window
    assert frozen_start <= date(2023, 12, 29) - window
    assert lookback["earliest_snapshot_date"] == oldest_snapshot.isoformat()
    assert lookback["maximum_snapshot_age_days"] == maximum_age


def test_capm_requires_explicit_pit_symbol_alias(project):
    config = yaml.safe_load(
        (project / "config/config.yaml").read_text(encoding="utf-8")
    )
    with pytest.raises(ValueError, match="BRK.B → BRK-B"):
        ResearchRunner(project, {})._resolve_spec(
            ResearchJobSpec.parse_obj(
                request(script_id="capm_dcf_value", codes=["BRK.B"])
            ),
            config,
        )


@pytest.mark.parametrize(
    "script_id", ["justified_pb_value", "current_strategy", "generated"]
)
def test_justified_pb_rejects_one_stock_before_creating_preview(
    project, monkeypatch, script_id
):
    from src.strategy import Params, get_strategy

    strategy = get_strategy("justified_pb_value")
    active = SimpleNamespace(
        strategy_for=lambda market: strategy,
        params_by_group={"a_share": Params(values={}, _engine=strategy.name)},
    )
    pointer = project / "data/optimizer/latest_strategy.yaml"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("groups: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "src.search.artifacts.load_latest_strategy_run", lambda **kwargs: active
    )
    spec = request(script_id=script_id)
    if script_id == "generated":
        spec["generated_code"] = "print('approved custom code')"
    instance = ResearchRunner(project, {})
    with pytest.raises(ValueError, match="横截面策略至少需要2只"):
        instance.prepare(spec, "abc123def456")
    assert not (instance.workspace_root / "abc123def456").exists()


def test_capm_declared_fx_dependency_uses_real_policy_parser(project):
    from src.fundamental_embedding.dcf_entry_calibration import (
        DCF_ENTRY_CALIBRATION_CONTRACT,
        DEFAULT_GROWTH_PROFILES,
        CapmDcfEntryConfig,
        CapmDcfEntryParameters,
    )

    rates = project / "risk_free.json"
    _json_write(rates, {"2020-01-01": 0.02})
    policy_path = project / "approved-policy.json"
    _json_write(
        policy_path,
        {
            "contract": DCF_ENTRY_CALIBRATION_CONTRACT,
            "dataset": {"market": "hk", "validation_start": "2020-01-01"},
            "acceptance": {"candidate_eligible_for_manual_strategy_experiment": True},
            "config": asdict(CapmDcfEntryConfig()),
            "selection": {
                "parameters": {
                    "fcfe_dcf": CapmDcfEntryParameters(
                        DEFAULT_GROWTH_PROFILES[0], 0.0, 0.10, 0.85
                    ).to_dict()
                },
                "policy_beta_reference": {"value": 0.8, "method": "training_only"},
            },
        },
    )
    config = {
        "optimizer": {
            "capm_dcf_value": {
                "markets": {
                    "hk": {
                        "data_root": "data/pit",
                        "risk_free_rates_json": rates.name,
                        "frozen_policy_report": policy_path.name,
                        "benchmark_symbol": "02800",
                        "market_currency": "HKD",
                        "currency_conversion_symbols": {"CNY": "CNYHKD=X"},
                    }
                }
            }
        }
    }
    resolved = {"strategy_id": "capm_dcf_value", "market": "hk", "benchmark_codes": []}
    input_dir = project / "frozen-input"
    input_dir.mkdir()
    files = ResearchRunner(project, {})._freeze_value_dependencies(
        config, resolved, input_dir
    )
    assert set(resolved["benchmark_codes"]) == {"02800", "CNYHKD=X"}
    assert set(files) == {rates, policy_path}
    assert (
        config["optimizer"]["capm_dcf_value"]["markets"]["hk"]["data_root"]
        == "/input/data"
    )
    with pytest.raises(ValidationError):
        ResearchJobSpec.parse_obj(request(codes=["CNYHKD=X"]))


def test_docker_command_has_only_owned_mounts_and_fixed_limits(runner):
    payload = proposal(runner)
    command = runner.docker_command(
        payload, docker="docker", image_id="sha256:" + "a" * 64
    )
    for flag in (
        "--network=none",
        "--read-only",
        "--user=65532:65532",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--memory=4096m",
        "--memory-swap=4096m",
        "--cpus=2",
        "--pids-limit=128",
        "--pull=never",
    ):
        assert flag in command
    mounts = [
        command[index + 1] for index, value in enumerate(command) if value == "--mount"
    ]
    assert len(mounts) == 2
    assert mounts[0].endswith("target=/input,readonly")
    assert mounts[1].endswith("target=/output")
    assert all(payload["job_dir"] in value for value in mounts)
    assert not any(".env" in value or "docker.sock" in value for value in command)
    assert command[-1] == "execute"
    custom = runner.docker_command(
        payload, docker="docker", image_id="sha256:" + "a" * 64, custom=True
    )
    assert custom[-1] == "custom"
    assert any(
        "output" in item and "custom" in item and "target=/output" in item
        for item in custom
    )


def test_preflight_fails_without_docker(runner, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert runner.preflight()["ready"] is False


def test_preflight_rejects_insufficient_host_memory(runner, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "docker")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"OSType": "linux", "MemTotal": 1024**3, "NCPU": 2}),
        ),
    )
    result = runner.preflight()
    assert result["ready"] is False
    assert result["host_memory_mb"] == 1024


def test_preflight_pins_image_id(runner, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "docker")
    monkeypatch.setattr(runner, "project_compatibility", lambda *args: {"ready": True})
    calls = []

    def docker(command, **kwargs):
        calls.append(command)
        text = (
            json.dumps({"OSType": "linux", "MemTotal": 8 * 1024**3, "NCPU": 4})
            if "info" in command
            else "sha256:" + "a" * 64
        )
        return SimpleNamespace(returncode=0, stdout=text)

    monkeypatch.setattr("subprocess.run", docker)
    assert runner.preflight()["image_id"] == "sha256:" + "a" * 64
    assert len(calls) == 2


def test_artifact_symlink_escape_is_rejected(runner, project):
    payload = proposal(runner)
    outside = project / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = Path(payload["job_dir"]) / "output/leak.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ValueError, match="escapes|symbolic"):
        runner.safe_artifacts(payload)


def test_artifact_budget_covers_combined_size_and_empty_directories(runner):
    payload = proposal(runner)
    runner.settings["max_output_mb"] = 1
    output = Path(payload["job_dir"]) / "output"
    for name in ("one", "two"):
        (output / name).write_bytes(b"a" * 600000)
    with pytest.raises(ValueError, match="size limit"):
        runner.safe_artifacts(payload)
    for path in output.iterdir():
        path.unlink()
    for index in range(1001):
        (output / f"dir{index}").mkdir()
    with pytest.raises(ValueError, match="filesystem entries"):
        runner.safe_artifacts(payload)


def bundle(code):
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2021-01-04", "2021-01-05"]),
            "volume": [100, 100],
            "qfq_factor": [1.0, 1.0],
            "tradable": [True, True],
        }
    )
    for prefix in ("raw", "qfq"):
        for field in ("open", "high", "low", "close"):
            frame[f"{prefix}_{field}"] = [10.0, 11.0]
    return PriceHistoryBundle(
        code=code, prices=frame, actions=[], source="mock-provider", currency="CNY"
    )


def selected_dataset(project, codes=("600036",)):
    from src.instruments.models import FinancialStatementSnapshot
    from src.instruments.point_in_time import PointInTimeFundamentalStore

    root = project / "data/reference_universe/fixture_batch/dataset"
    market = PointInTimeMarketStore(root)
    fundamentals = PointInTimeFundamentalStore(root)
    for code in codes:
        market.write(bundle(code))
        fundamentals.upsert(
            code,
            [
                FinancialStatementSnapshot(
                    period_end=date(2020, 12, 31),
                    published_at=date(2021, 3, 1),
                    period_type="year",
                    source="fixture_statement",
                    currency="CNY",
                    parent_equity=500.0,
                    book_value_per_share=5.0,
                    average_parent_equity=1000.0,
                    net_income_parent=120.0,
                    reported_roe=12.0,
                )
            ],
        )
    catalog = DatasetCatalog(project)
    catalog.build()
    item = next(
        item
        for item in catalog.list_datasets(limit=100)["items"]
        if item["logical_root"] == "data/reference_universe/fixture_batch/dataset"
    )
    return item["dataset_id"], root


def test_missing_stock_query_uses_shared_single_code_backfill_and_indexes_result(
    runner, project, monkeypatch
):
    from src.data.dataset_catalog import DatasetCatalog
    from src.instruments.models import FinancialStatementSnapshot
    from src.instruments.point_in_time import PointInTimeFundamentalStore

    calls = []

    class Backfill:
        def __init__(self, config):
            self.config = config

        def run(self, *, codes, evaluation_date):
            calls.append((list(codes), evaluation_date))
            data_root = Path(self.config["point_in_time_data"]["output_dir"])
            PointInTimeMarketStore(data_root).write(bundle(codes[0]))
            PointInTimeFundamentalStore(data_root).upsert(
                codes[0],
                [
                    FinancialStatementSnapshot(
                        period_end=date(2020, 12, 31),
                        published_at=date(2021, 3, 1),
                        period_type="year",
                        source="fixture_backfill",
                        currency="CNY",
                        parent_equity=500.0,
                        book_value_per_share=5.0,
                        average_parent_equity=1000.0,
                        net_income_parent=120.0,
                        reported_roe=12.0,
                    )
                ],
            )
            return {
                "instruments": [
                    {
                        "code": codes[0],
                        "market_history": {
                            "status": "success",
                            "source": "fixture-market",
                            "actual_end": "2021-01-05",
                        },
                        "statements": {
                            "status": "success",
                            "last_period": "2020-12-31",
                        },
                    }
                ]
            }

    monkeypatch.setattr(
        "src.data.point_in_time_backfill.PointInTimeBackfillService", Backfill
    )

    result = runner.query_stock_fundamentals("600150")

    assert calls == [(["600150"], date.fromisoformat(result["as_of"]))]
    assert result["selected"]["status"] == "available"
    assert result["selected"]["price_date"] == "2021-01-05"
    assert result["automatic_backfill"]["status"] == "completed"
    assert result["automatic_backfill"]["indexed_files"] == 4
    indexed = DatasetCatalog(project).files(code="600150", limit=10)
    assert indexed["total"] == 4
    assert all(
        "assistant_ad_hoc" in item["relative_path"] for item in indexed["items"]
    )
    assert (
        DatasetCatalog(project).index_status()["index_status"] == "incremental"
    )


def test_failed_stock_backfill_is_cooled_down_per_code(runner, monkeypatch):
    calls = []

    class Backfill:
        def __init__(self, _config):
            pass

        def run(self, *, codes, evaluation_date):
            calls.append((list(codes), evaluation_date))
            return {
                "instruments": [
                    {
                        "code": codes[0],
                        "market_history": {"status": "failed"},
                        "statements": {"status": "missing"},
                    }
                ]
            }

    monkeypatch.setattr(
        "src.data.point_in_time_backfill.PointInTimeBackfillService", Backfill
    )

    first = runner.query_stock_fundamentals("600150")
    second = runner.query_stock_fundamentals("600150")

    assert len(calls) == 1
    assert first["automatic_backfill"]["status"] == "failed"
    assert second["automatic_backfill"]["status"] == "cooldown"


def test_selected_dataset_is_frozen_into_job_and_used_for_preparation(
    runner, project, monkeypatch
):
    dataset_id, dataset_root = selected_dataset(project, ("600036",))
    original = (dataset_root / "market/600036.csv").read_bytes()
    payload = proposal(runner, dataset_id=dataset_id)
    preview = payload["preview"]
    assert preview["source_dataset_id"] == dataset_id
    assert preview["source_market_codes"] == ["600036"]
    assert preview["source_statement_codes"] == ["600036"]
    assert preview["source_missing_market_codes"] == []
    frozen = Path(payload["job_dir"]) / "input/data_seed/market/600036.csv"
    assert frozen.is_file()
    assert _sha(frozen) == _sha(dataset_root / "market/600036.csv")
    runner.check(payload)

    called = []

    def prepare(config, codes, start, end, **kwargs):
        called.append(config["point_in_time_data"]["output_dir"])
        store = PointInTimeMarketStore(config["point_in_time_data"]["output_dir"])
        assert store.read("600036") is not None
        return BacktestDataResult(
            "test",
            date(2020, 1, 1),
            date(2024, 1, 1),
            bundles={code: bundle(code) for code in ("600036", "510300")},
        )

    monkeypatch.setattr("src.data.backtest_data.prepare_backtest_data", prepare)
    monkeypatch.chdir(project)
    manifest = prepare_inputs(Path(payload["job_dir"]), project)
    assert manifest["ready"]
    assert manifest["source_dataset_id"] == dataset_id
    assert called == [str(Path(payload["job_dir"]) / "preparation_cache")]
    prepared_root = Path(payload["job_dir"]) / "input/data"
    from src.instruments.point_in_time import PointInTimeFundamentalStore

    assert PointInTimeMarketStore(prepared_root).read("600036") is not None
    assert PointInTimeFundamentalStore(prepared_root).as_of(
        "600036", date(2024, 1, 1)
    )
    assert (dataset_root / "market/600036.csv").read_bytes() == original


def test_gordon_ke_sensitivity_uses_selected_pit_dataset_and_each_growth_rate(
    runner, project
):
    dataset_id, _ = selected_dataset(project)
    result = runner.calculate_gordon_ke_sensitivity(
        dataset_id,
        ["600036"],
        "2021-04-01",
        [0.02, 0.04],
    )
    item = result["instruments"][0]
    assert result["point_in_time_only"]
    assert item["status"] == "available"
    assert item["price_date"] == "2021-01-05"
    assert item["statement_published_at"] == "2021-03-01"
    assert item["roe"] == pytest.approx(0.12)
    assert item["pb"] == pytest.approx(2.2)
    assert item["scenarios"] == [
        {"g": 0.02, "implied_ke": pytest.approx(0.0654545454545)},
        {"g": 0.04, "implied_ke": pytest.approx(0.0763636363636)},
    ]


def test_gordon_ke_sensitivity_does_not_use_late_disclosures(runner, project):
    dataset_id, _ = selected_dataset(project)
    result = runner.calculate_gordon_ke_sensitivity(
        dataset_id,
        ["600036"],
        "2021-02-01",
        [0.02],
    )
    assert result["instruments"][0]["status"] == "unavailable"
    assert "no statements published" in result["instruments"][0]["reason"]


def test_gordon_ke_sensitivity_rejects_invalid_dataset_identity(runner):
    with pytest.raises(ValueError, match="dataset_id"):
        runner.calculate_gordon_ke_sensitivity(
            "../outside", ["600036"], "2021-04-01", [0.02]
        )


@pytest.mark.parametrize("provider_settings", [True, False])
def test_trusted_prep_fetches_only_approved_inputs_and_freezes_inventory(
    runner, monkeypatch, provider_settings
):
    if not provider_settings:
        path = runner.project_root / "config/config.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw.pop("provider_access")
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    payload = proposal(
        runner,
        script_id="generated",
        generated_code="raise AssertionError('host execution forbidden')",
    )
    called = []

    def prepare(config, codes, start, end, **kwargs):
        called.append((config, codes, start, kwargs))
        return BacktestDataResult(
            "test",
            date(2020, 1, 1),
            date(2024, 1, 1),
            bundles={code: bundle(code) for code in ("600036", "510300")},
        )

    monkeypatch.setattr("src.data.backtest_data.prepare_backtest_data", prepare)
    monkeypatch.chdir(runner.project_root)
    result = prepare_inputs(Path(payload["job_dir"]), runner.project_root)
    assert result["ready"] is True
    assert called[0][1] == ["600036"]
    assert called[0][3]["benchmark_codes"] == ["510300"]
    assert called[0][0]["instrument_catalog"] == {"600036": {"type": "equity"}}
    assert called[0][0]["instrument_audit"] == {"timeout_seconds": 17}
    assert called[0][0]["provider_access"]["baostock"]["state_dir"] == str(
        runner.project_root / "data/provider_state/baostock"
    )
    assert "provider_access" not in yaml.safe_load(
        (Path(payload["job_dir"]) / "input/code/config/config.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert len(result["files"]) == 6
    assert set(result["requested_codes"]) == {"600036"}
    assert result["raw_prices"] and result["explicit_dividends_and_withholding"]


def test_failed_backfill_is_not_marked_ready(runner, monkeypatch):
    payload = proposal(runner)
    monkeypatch.setattr(
        "src.data.backtest_data.prepare_backtest_data",
        lambda *args, **kwargs: BacktestDataResult(
            "test",
            date(2020, 1, 1),
            date(2024, 1, 1),
            issues=[
                DataReadinessIssue(
                    "600036", "provider", "2020-01-01", "2024-01-01", "offline"
                )
            ],
        ),
    )
    monkeypatch.chdir(runner.project_root)
    with pytest.raises(ValueError, match="readiness failed"):
        prepare_inputs(Path(payload["job_dir"]), runner.project_root)
    manifest = json.loads(
        (Path(payload["job_dir"]) / "input/data_manifest.json").read_text()
    )
    assert manifest["ready"] is False
    assert {item["code"] for item in manifest["issues"]} == {"600036", "510300"}


def mock_pipeline(runner, monkeypatch):
    monkeypatch.setattr(
        runner,
        "preflight",
        lambda: {"ready": True, "docker": "docker", "image_id": "sha256:" + "a" * 64},
    )
    monkeypatch.setattr(runner, "cleanup_recovered", lambda _: None)
    calls = []

    def execute(command, payload, event, **kwargs):
        calls.append(command)
        job_dir = Path(payload["job_dir"])
        if "prepare" in command:
            data = job_dir / "input/data"
            data.mkdir()
            (data / "fixture.csv").write_text("data", encoding="utf-8")
            _json_write(
                job_dir / "input/data_manifest.json",
                {"ready": True, "files": {"fixture.csv": _sha(data / "fixture.csv")}},
            )
        elif command[-1] == "custom":
            _json_write(
                job_dir / "output/custom/report.json",
                {"metrics": {"total_return": 999999}},
            )
        else:
            _json_write(
                job_dir / "output/report.json",
                {
                    "contract": "feishu-research-shared-execution/1",
                    "job_id": payload["job_id"],
                    "metrics": {"total_return": 4.2},
                },
            )
        return 0

    monkeypatch.setattr(runner, "_run_process", execute)
    return calls


def test_approved_job_uses_frozen_config_after_later_edits(runner, monkeypatch):
    payload = proposal(runner)
    mock_pipeline(runner, monkeypatch)
    (runner.project_root / "config/config.yaml").write_text(
        "changed: true\n", encoding="utf-8"
    )
    result = runner.run(payload, threading.Event(), lambda _: None)
    assert result["status"] == "completed"
    assert "4.2" in result["summary"]


def test_failed_progress_delivery_does_not_abort_research(runner, monkeypatch):
    payload = proposal(runner)
    mock_pipeline(runner, monkeypatch)

    def notify(message):
        raise RuntimeError("chat temporarily unavailable")

    result = runner.run(payload, threading.Event(), notify)
    assert result["status"] == "completed"
    assert "4.2" in result["summary"]


def test_generated_report_cannot_replace_baseline_metrics(runner, monkeypatch):
    payload = proposal(
        runner, script_id="generated", generated_code="print(CONTEXT['baseline'])"
    )
    calls = mock_pipeline(runner, monkeypatch)
    result = runner.run(payload, threading.Event(), lambda _: None)
    assert result["status"] == "completed"
    assert "4.2" in result["summary"] and "999999" not in result["summary"]
    assert len(calls) == 3
    assert any(
        path.endswith(("custom\\report.json", "custom/report.json"))
        for path in result["artifacts"]
    )
    assert (Path(payload["job_dir"]) / "input/baseline/report.json").is_file()


def test_cancel_before_execution_never_fetches(runner, monkeypatch):
    payload = proposal(runner)
    calls = mock_pipeline(runner, monkeypatch)
    event = threading.Event()
    event.set()
    result = runner.run(payload, event, lambda _: None)
    assert result["status"] == "cancelled"
    assert calls == []


def test_data_preparation_failure_never_starts_container(runner, monkeypatch):
    payload = proposal(runner)
    calls = mock_pipeline(runner, monkeypatch)
    monkeypatch.setattr(runner, "_run_process", lambda *args, **kwargs: 1)
    result = runner.run(payload, threading.Event(), lambda _: None)
    assert result["status"] == "failed"
    assert "数据准备失败" in result["summary"]
    assert not calls


def test_recovery_removes_only_owned_container(runner, monkeypatch):
    payload = proposal(runner)
    commands = []
    monkeypatch.setattr(shutil, "which", lambda _: "docker")
    monkeypatch.setattr(
        "subprocess.run", lambda command, **kwargs: commands.append(command)
    )
    runner.cleanup_recovered(payload)
    assert commands == [
        ["docker", "rm", "-f", runner._container_name(payload["job_id"])]
    ]


def test_recovery_returns_completed_host_receipt_without_rerun(runner, monkeypatch):
    payload = proposal(runner)
    calls = mock_pipeline(runner, monkeypatch)
    expected = runner.run(payload, threading.Event(), lambda _: None)
    before = len(calls)
    recovered = runner.recover_result(payload)
    assert recovered == expected
    assert len(calls) == before


def test_recovery_rejects_modified_output_and_preserves_unfinished_logs(
    runner, monkeypatch
):
    payload = proposal(runner)
    mock_pipeline(runner, monkeypatch)
    job_dir = Path(payload["job_dir"])
    (job_dir / "prepare.log").write_text("started", encoding="utf-8")
    partial = runner.recover_result(payload)
    assert partial["status"] == "interrupted"
    assert str(job_dir / "prepare.log") in partial["artifacts"]
    runner.run(payload, threading.Event(), lambda _: None)
    (job_dir / "output/report.json").write_text("changed", encoding="utf-8")
    rejected = runner.recover_result(payload)
    assert rejected["status"] == "interrupted"
    assert rejected["artifacts"] == []
    assert "evidence changed" in rejected["errors"][0]


def test_process_timeout_reaps_host_child(runner):
    payload = proposal(runner)
    with pytest.raises(TimeoutError):
        runner._run_process(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            payload,
            threading.Event(),
            timeout=1,
            log_name="timeout.log",
        )
    assert not runner._processes


def memory_sample(*, rss_mb=20, swap_mb=0, available_mb=1024):
    return {
        "rss_bytes": rss_mb * 1024 * 1024,
        "swap_bytes": swap_mb * 1024 * 1024,
        "rss_plus_swap_bytes": (rss_mb + swap_mb) * 1024 * 1024,
        "available_bytes": available_mb * 1024 * 1024,
        "process_ids": [123],
    }


def test_linux_memory_reader_counts_only_owned_group_rss_and_swap(tmp_path):
    (tmp_path / "meminfo").write_text("MemAvailable: 2048 kB\nMemTotal: 4096 kB\n")
    for pid, group, rss, swap in [
        (20, 20, 100, 50),
        (21, 20, 200, 25),
        (22, 22, 999, 999),
    ]:
        folder = tmp_path / str(pid)
        folder.mkdir()
        (folder / "stat").write_text(
            f"{pid} (name with ) parentheses) S 1 {group} {group} 0 0\n"
        )
        (folder / "status").write_text(
            f"State:\tS (sleeping)\nVmRSS: {rss} kB\nVmSwap: {swap} kB\n"
        )
    sample = _linux_memory_snapshot(20, tmp_path)
    assert sample["process_ids"] == [20, 21]
    assert sample["rss_bytes"] == 300 * 1024
    assert sample["swap_bytes"] == 75 * 1024
    assert sample["available_bytes"] == 2048 * 1024


@pytest.mark.parametrize(
    "setting,value",
    [
        ("prepare_memory_mb", 511),
        ("prepare_memory_mb", 65537),
        ("prepare_min_available_mb", 63),
    ],
)
def test_host_memory_settings_enforce_deployment_bounds(project, setting, value):
    with pytest.raises(ValueError, match=setting):
        ResearchRunner(project, {setting: value})
    runner = ResearchRunner(project, {"memory_mb": 768, "prepare_memory_mb": None})
    assert runner.settings["prepare_memory_mb"] == 768


def test_unsupported_host_is_rejected_before_preview_or_execution(runner, monkeypatch):
    monkeypatch.setattr(
        runner,
        "_host_prepare_support",
        lambda: {
            "ready": False,
            "error": "Linux /proc is required; Windows unsupported",
        },
    )
    assert runner.preflight()["ready"] is False
    with pytest.raises(ValueError, match="Windows unsupported"):
        proposal(runner)
    assert not (runner.workspace_root / "abc123def456").exists()


@pytest.mark.parametrize("low_available", [False, True])
def test_host_memory_limit_stops_real_child_and_records_peak(
    runner, monkeypatch, low_available
):
    runner.settings["prepare_memory_mb"] = 512
    payload = proposal(runner)
    samples = []

    def observe(pgid):
        samples.append(pgid)
        return (
            memory_sample()
            if pgid is None
            else memory_sample(
                rss_mb=200 if low_available else 400,
                swap_mb=20 if low_available else 120,
                available_mb=70 if low_available else 1024,
            )
        )

    monkeypatch.setattr(
        "src.interactive.assistant.research._linux_memory_snapshot", observe
    )
    terminated = []
    original = runner._terminate

    def terminate(process, **kwargs):
        original(process, **kwargs)
        terminated.append(process.poll())

    monkeypatch.setattr(runner, "_terminate", terminate)
    with pytest.raises(PreparationMemoryError, match="内存"):
        runner._run_process(
            [
                sys.executable,
                "-c",
                "import time; print('started', flush=True); time.sleep(20)",
            ],
            payload,
            threading.Event(),
            timeout=10,
            log_name="prepare.log",
            protect_host_memory=True,
        )
    report = json.loads(
        (Path(payload["job_dir"]) / "prepare_resources.json").read_text()
    )
    assert report["status"] == "failed"
    assert (
        report["peak_rss_plus_swap_bytes"]
        == (220 if low_available else 520) * 1024 * 1024
    )
    assert (
        report["minimum_observed_available_bytes"]
        == (70 if low_available else 1024) * 1024 * 1024
    )
    assert terminated and terminated[0] is not None
    assert not runner._processes


def test_low_host_memory_does_not_launch_preparation(runner, monkeypatch):
    payload = proposal(runner)
    monkeypatch.setattr(
        "src.interactive.assistant.research._linux_memory_snapshot",
        lambda pgid: memory_sample(available_mb=64),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("must not start child when memory is already low")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    with pytest.raises(PreparationMemoryError, match="可用内存"):
        runner._run_process(
            ["unused"],
            payload,
            threading.Event(),
            timeout=10,
            log_name="prepare.log",
            protect_host_memory=True,
        )


def test_missing_memory_accounting_fails_closed_before_launch(runner, monkeypatch):
    payload = proposal(runner)

    def unavailable(pgid):
        raise OSError("MemAvailable denied")

    monkeypatch.setattr(
        "src.interactive.assistant.research._linux_memory_snapshot", unavailable
    )
    with pytest.raises(PreparationMemoryError, match="无法读取可靠统计"):
        runner._run_process(
            ["must-not-start"],
            payload,
            threading.Event(),
            timeout=10,
            log_name="prepare.log",
            protect_host_memory=True,
        )
    report = json.loads(
        (Path(payload["job_dir"]) / "prepare_resources.json").read_text()
    )
    assert report["status"] == "failed"
    assert report["samples"] == 0


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="real Linux process group cleanup"
)
def test_host_memory_guard_cleans_descendant_process_group(runner, monkeypatch):
    payload = proposal(runner)
    runner.settings["prepare_memory_mb"] = 512
    child_file = Path(payload["job_dir"]) / "descendant.pid"

    def observe(pgid):
        return memory_sample(
            rss_mb=600 if pgid is not None and child_file.exists() else 20
        )

    monkeypatch.setattr(
        "src.interactive.assistant.research._linux_memory_snapshot", observe
    )
    script = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
    )
    with pytest.raises(PreparationMemoryError):
        runner._run_process(
            [sys.executable, "-c", script, str(child_file)],
            payload,
            threading.Event(),
            timeout=10,
            log_name="prepare.log",
            protect_host_memory=True,
        )
    child = int(child_file.read_text())
    status = Path(f"/proc/{child}/status")
    assert not status.exists() or "State:\tZ" in status.read_text()


def test_normal_host_preparation_records_memory_and_flushes_logs(runner, monkeypatch):
    payload = proposal(runner)
    monkeypatch.setattr(
        "src.interactive.assistant.research._linux_memory_snapshot",
        lambda pgid: memory_sample(),
    )
    assert (
        runner._run_process(
            [
                sys.executable,
                "-c",
                "import time; print('ready', flush=True); time.sleep(.2)",
            ],
            payload,
            threading.Event(),
            timeout=10,
            log_name="prepare.log",
            protect_host_memory=True,
        )
        == 0
    )
    report = json.loads(
        (Path(payload["job_dir"]) / "prepare_resources.json").read_text()
    )
    assert report["status"] == "completed"
    assert report["peak_rss_bytes"] == 20 * 1024 * 1024
    assert (Path(payload["job_dir"]) / "prepare.log").read_text().strip() == "ready"


def test_memory_guard_failure_persists_failed_result_and_readiness(runner, monkeypatch):
    payload = proposal(runner)
    mock_pipeline(runner, monkeypatch)

    def exhausted(*args, **kwargs):
        assert kwargs["protect_host_memory"] is True
        raise PreparationMemoryError("宿主补数进程组内存超过限额")

    monkeypatch.setattr(runner, "_run_process", exhausted)
    result = runner.run(payload, threading.Event(), lambda _: None)
    assert result["status"] == "failed"
    assert "内存超过限额" in result["summary"]
    saved = json.loads((Path(payload["job_dir"]) / "result.json").read_text())
    readiness = json.loads(
        (Path(payload["job_dir"]) / "data_readiness.json").read_text()
    )
    assert saved["status"] == "failed"
    assert readiness["ready"] is False
    assert readiness["issues"][0]["source"] == "host_memory_monitor"


def test_shared_adapter_preserves_market_and_benchmark_bundles(runner, monkeypatch):
    payload = proposal(runner)
    job_dir = Path(payload["job_dir"])
    store = PointInTimeMarketStore(job_dir / "input/data")
    for code in ("600036", "510300"):
        store.write(bundle(code))
    files = {
        path.relative_to(job_dir / "input/data").as_posix(): _sha(path)
        for path in (job_dir / "input/data").rglob("*")
        if path.is_file()
    }
    _json_write(job_dir / "input/data_manifest.json", {"ready": True, "files": files})
    calls = []
    report = EvaluationReport(
        "a_share",
        "justified_pb_value",
        "test",
        "2024-01-01",
        3.0,
        1.0,
        -2.0,
        1.2,
        2,
        30.0,
        gross_dividend_cash=100,
        dividend_tax_cost=20,
        net_dividend_cash=80,
    )

    def evaluate(*args, **kwargs):
        calls.append((args, kwargs))
        return {"a_share": report}

    monkeypatch.setattr("src.backtest.engine.evaluate_all_groups", evaluate)
    monkeypatch.setattr(
        "src.interactive.assistant.research_worker._enricher_with_coverage",
        lambda *args: None,
    )
    result = run_registered(job_dir / "input", job_dir / "output")
    arguments = calls[0][1]
    assert set(arguments["market_bundles"]) == {"600036"}
    assert set(arguments["benchmark_bundles"]) == {"510300"}
    assert "raw_close" in arguments["market_bundles"]["600036"].prices
    assert arguments["start_date"] == "2021-01-01"
    assert arguments["market_constraints"].market_group == "a_share"
    assert result["evaluation_report"]["net_dividend_cash"] == 80
    assert (job_dir / "output/nav.csv").is_file()


def test_safe_path_rejects_relative_traversal(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        _safe_path(tmp_path / "inside/../../outside", tmp_path)


def test_preflight_cli_uses_flat_assistant_settings(project, monkeypatch, capsys):
    from scripts.check_feishu_research import main

    path = project / "config/config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["interactive"]["feishu"]["assistant"] = {"cpus": 1, "memory_mb": 768}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    seen = []

    def preflight(self):
        seen.append(self.settings)
        return {"ready": True}

    monkeypatch.setattr(ResearchRunner, "preflight", preflight)
    monkeypatch.setattr(sys, "argv", ["check", "--project-root", str(project)])
    assert main() == 0
    assert seen[0]["cpus"] == 1 and seen[0]["memory_mb"] == 768
    assert "true" in capsys.readouterr().out


def test_project_probe_reports_exact_incompatible_engine_contract(monkeypatch):
    monkeypatch.setattr("src.backtest.engine.evaluate_all_groups", lambda stocks: {})
    result = ResearchRunner.project_compatibility()
    assert result["ready"] is False
    assert any(
        issue["module"] == "src.backtest.engine" and "market_bundles" in issue["reason"]
        for issue in result["issues"]
    )
    assert "src/data/backtest_data.py" in result["required_files_sha256"]


def test_project_probe_rejects_unguarded_market_provider(monkeypatch):
    monkeypatch.setattr("src.data.market_history.baostock_session", lambda *args: None)
    result = ResearchRunner.project_compatibility()
    assert result["ready"] is False
    assert any(
        "shared provider limits" in issue["reason"] for issue in result["issues"]
    )


def test_horizon_uses_existing_modern_policy_without_legacy_fallback(tmp_path):
    constraints = SimpleNamespace(
        evaluation_horizon=SimpleNamespace(minimum_evaluation_months=30),
        walk_forward=SimpleNamespace(test_months=24, state_lookback_months=6),
    )
    result = _evaluation_horizon(constraints, tmp_path / "not-needed.yaml")
    assert result["minimum_evaluation_months"] == 30
    assert result["source"] == "shared_constraints.evaluation_horizon"
    constraints.evaluation_horizon = SimpleNamespace(minimum_evaluation_months=12)
    with pytest.raises(ValueError, match=">= 24"):
        _evaluation_horizon(constraints, tmp_path / "not-needed.yaml")


@pytest.mark.parametrize("test_months,minimum", [(9, 24), (36, 36)])
def test_legacy_horizon_has_explicit_minimum_and_preserves_test_window(
    tmp_path, test_months, minimum
):
    path = tmp_path / "constraints.yaml"
    path.write_text("walk_forward: {}\n", encoding="utf-8")
    constraints = SimpleNamespace(
        walk_forward=SimpleNamespace(test_months=test_months, state_lookback_months=6)
    )
    result = _evaluation_horizon(constraints, path)
    assert result["minimum_evaluation_months"] == minimum
    assert result["source"] == "assistant_legacy_minimum_24_months"


def test_legacy_horizon_honors_declared_yaml_policy(tmp_path):
    path = tmp_path / "constraints.yaml"
    path.write_text(
        "evaluation_horizon_policy:\n  minimum_evaluation_months: 30\n",
        encoding="utf-8",
    )
    constraints = SimpleNamespace(
        walk_forward=SimpleNamespace(test_months=9, state_lookback_months=6)
    )
    result = _evaluation_horizon(constraints, path)
    assert result["minimum_evaluation_months"] == 30
    assert result["source"].endswith("(legacy core)")


@pytest.mark.parametrize(
    "declaration",
    [
        None,
        "bad-policy",
        {"minimum_evaluation_months": 12},
        {"minimum_evaluation_months": 24.5},
        {"minimum_evaluation_months": True},
        {"default_backtest_months": 9},
        {"minimum_eval_months": 12},
        {"contract": ""},
    ],
)
def test_legacy_horizon_never_ignores_damaged_declaration(tmp_path, declaration):
    path = tmp_path / "constraints.yaml"
    path.write_text(
        yaml.safe_dump({"evaluation_horizon_policy": declaration}), encoding="utf-8"
    )
    constraints = SimpleNamespace(
        walk_forward=SimpleNamespace(test_months=9, state_lookback_months=6)
    )
    with pytest.raises(ValueError):
        _evaluation_horizon(constraints, path)


def test_cached_only_smoke_never_invokes_network_preparation(runner, monkeypatch):
    payload = proposal(runner)
    store = PointInTimeMarketStore(runner.project_root / "data/pit")
    for code in ("600036", "510300"):
        item = bundle(code)
        item.prices["date"] = pd.to_datetime(["2020-01-02", "2023-12-29"])
        store.write(item)

    def forbidden(*args, **kwargs):
        raise AssertionError("network preparation is forbidden in smoke checks")

    monkeypatch.setattr("src.data.backtest_data.prepare_backtest_data", forbidden)
    monkeypatch.setattr(
        "src.data.market_calendar.resolve_market_data_cutoff",
        lambda *args, **kwargs: SimpleNamespace(
            effective_end=date(2023, 12, 29),
            as_dict=lambda: {"effective_end": "2023-12-29"},
        ),
    )
    monkeypatch.chdir(runner.project_root)
    manifest = prepare_inputs(
        Path(payload["job_dir"]), runner.project_root, cached_only=True
    )
    assert manifest["ready"] and manifest["cached_only"]
    assert manifest["market_cutoffs"]["600036"]["effective_end"] == "2023-12-29"


def test_worker_redacts_provider_failure_details():
    from src.interactive.assistant.research_worker import _configure_redaction, _redact

    _configure_redaction({"llm": {"api_key": "private-test-credential"}})
    assert _redact({"reason": "HTTP rejected private-test-credential"}) == {
        "reason": "HTTP rejected [REDACTED]"
    }


def test_describe_data_reports_actual_cache_without_claiming_completeness(
    runner, monkeypatch
):
    store = PointInTimeMarketStore(runner.project_root / "data/pit")
    store.write(bundle("600036"))
    monkeypatch.setattr(
        "src.instruments.point_in_time.PointInTimeFundamentalStore.read_all",
        lambda self, code: (
            [
                SimpleNamespace(published_at=date(2021, 3, 31)),
                SimpleNamespace(published_at=date(2022, 3, 30)),
            ]
            if code == "600036"
            else []
        ),
    )
    result = runner.describe_data(["600036", "VOO"])
    assert result["completeness_checked"] is False
    available, missing = result["instruments"]
    assert available["market"]["rows"] == 2
    assert available["market"]["start"] == "2021-01-04"
    assert available["market"]["corporate_actions"] == 0
    assert available["fundamentals"]["published_start"] == "2021-03-31"
    assert available["fundamentals"]["statements"] == 2
    assert missing["market"]["status"] == "missing"
    assert missing["fundamentals"]["statements"] == 0


@pytest.mark.parametrize("codes", [[], ["600036"] * 101, ["../secrets"], [600036]])
def test_describe_data_rejects_invalid_queries(runner, codes):
    with pytest.raises((TypeError, ValueError)):
        runner.describe_data(codes)
