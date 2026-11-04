from __future__ import annotations

import yaml

from src.core.ref_portfolio import Holding, RefPortfolio
from src.interactive.commands import handlers
from src.search.artifacts import ActiveStrategyRun, load_strategy_run
from src.strategy import Params

DATES = ["2026-07-28", "2026-07-29", "2026-07-30"]


def _bound(cash: float = 100000.0) -> RefPortfolio:
    return RefPortfolio(
        inception_date=DATES[0],
        cash=cash,
        initial_capital=cash,
        market_group="a_share",
        strategy_run_id="run-1",
        strategy_id="percentile",
        strategy_timestamp="2026-07-30T19:00:00",
        params_hash="params",
        execution_hash="execution",
    )


def test_binding_and_event_ids_round_trip():
    portfolio = _bound()
    portfolio.processed_events = ["run-1:2026-07-30:601088:buy"]
    portfolio.holdings["601088"] = Holding(
        "601088",
        100,
        10.0,
        DATES[-1],
    )

    restored = RefPortfolio.from_dict(portfolio.to_dict())

    assert restored.is_bound
    assert restored.strategy_run_id == "run-1"
    assert restored.processed_events == portfolio.processed_events
    assert restored.holdings["601088"].last_buy_date == DATES[-1]


def test_exact_run_loader_ignores_legacy_manifest(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "a_share.yaml"
    artifact.write_text(
        yaml.safe_dump(
            {
                "strategy_id": "percentile",
                "params": {"threshold": 1},
                "execution": {
                    "model": "cash_cap",
                    "buy_cash_limit": 10000,
                    "sell_cash_limit": 10000,
                },
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "run_id": "run-1",
        "strategy": "percentile",
        "timestamp": "2026-07-30T19:00:00",
        "activated": True,
        "groups": {
            "a_share": {
                "artifact": "runs/run-1/a_share.yaml",
            }
        },
    }
    (run_dir / "manifest.yaml").write_text(
        yaml.safe_dump(manifest),
        encoding="utf-8",
    )

    loaded = load_strategy_run(
        "run-1",
        groups=("a_share",),
        root=tmp_path,
    )

    # The v3-style manifest is intentionally not migrated into the current
    # active index.  An explicit v4 search and activation is required.
    assert loaded is None
    assert load_strategy_run(
        "missing",
        groups=("a_share",),
        root=tmp_path,
    ) is None
    manifest["activated"] = False
    (run_dir / "manifest.yaml").write_text(
        yaml.safe_dump(manifest),
        encoding="utf-8",
    )
    assert load_strategy_run(
        "run-1",
        groups=("a_share",),
        root=tmp_path,
    ) is None


def test_manual_ref_reset_pins_all_markets_to_latest_active_run(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "optimizer": {
                    "markets": {
                        group: {
                            "strategy": "percentile",
                            "solver_id": "random",
                            "gate_profile": "standard",
                            "walk_forward_profile": f"{group}_84m",
                            "execution_profile": (
                                "a_share_cny"
                                if group == "a_share"
                                else "hk_hkd"
                                if group == "hk"
                                else "us_usd"
                            ),
                            "benchmark_profile": group,
                        }
                        for group in ("a_share", "hk", "us")
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(handlers, "CONFIG_PATH", config_path)
    params = {
        group: Params(
            {"threshold": index},
            _engine="percentile",
            execution_snapshot={
                "model": "cash_cap",
                "buy_cash_limit": 10000,
                "sell_cash_limit": 10000,
            },
        )
        for index, group in enumerate(("a_share", "hk", "us"), start=1)
    }
    active = ActiveStrategyRun(
        strategy_name="percentile",
        timestamp="2026-07-30T19:00:00",
        params_by_group=params,
        run_id="run-1",
        strategy_by_group={group: "percentile" for group in params},
        solver_by_group={group: "random" for group in params},
        config_hash_by_group={group: f"hash-{group}" for group in params},
        run_ids_by_group={group: f"run-{group}" for group in params},
    )
    monkeypatch.setattr(
        "src.search.artifacts.load_latest_strategy_run",
        lambda: active,
    )
    resets = []

    class FakeManager:
        def __init__(self, file_path):
            self.file_path = file_path

        def load(self):
            return RefPortfolio()

        @staticmethod
        def is_initialized(portfolio):
            return bool(portfolio.inception_date)

        def reset(self, **kwargs):
            resets.append((self.file_path, kwargs))
            return RefPortfolio()

    monkeypatch.setattr(
        "src.core.ref_portfolio.RefPortfolioManager",
        FakeManager,
    )

    response = handlers.handle_ref_date("2026-07-30")

    assert response.startswith("✅")
    assert len(resets) == 3
    assert {values["market_group"] for _, values in resets} == {
        "a_share",
        "hk",
        "us",
    }
    assert {
        values["strategy_run_id"] for _, values in resets
    } == {"run-a_share", "run-hk", "run-us"}
    assert all(values["strategy_id"] == "percentile" for _, values in resets)
    assert all(values["params_hash"] for _, values in resets)
    assert all(values["execution_hash"] for _, values in resets)
