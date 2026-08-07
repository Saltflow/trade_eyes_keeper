from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import yaml

from src.core.ref_portfolio import Holding, RefPortfolio, RefPortfolioManager
from src.interactive.commands import handlers
from src.search.artifacts import ActiveStrategyRun
from src.search.artifacts import load_strategy_run
from src.strategy import Params, StrategyMarketData, TradePlan


DATES = ["2026-07-28", "2026-07-29", "2026-07-30"]


def _market(
    symbols: list[str],
    closes: np.ndarray,
    highs: np.ndarray | None = None,
    lows: np.ndarray | None = None,
) -> StrategyMarketData:
    values = np.asarray(closes, dtype=float)
    rows, columns = values.shape
    return StrategyMarketData(
        indicator_matrix=np.zeros((rows, columns, 1), dtype=np.float32),
        dates=list(DATES),
        symbols=list(symbols),
        prices=values,
        highs=values if highs is None else np.asarray(highs, dtype=float),
        lows=values if lows is None else np.asarray(lows, dtype=float),
        tradable=np.ones((rows, columns), dtype=bool),
    )


def _plan(
    symbols: list[str],
    buys: np.ndarray,
    sells: np.ndarray | None = None,
    *,
    model: str = "cash_cap",
    target_weights: np.ndarray | None = None,
) -> TradePlan:
    buy_matrix = np.asarray(buys, dtype=bool)
    sell_matrix = (
        np.zeros_like(buy_matrix)
        if sells is None
        else np.asarray(sells, dtype=bool)
    )
    return TradePlan(
        buy_signals=buy_matrix,
        sell_signals=sell_matrix,
        buy_priority=buy_matrix.astype(np.float32),
        sell_priority=sell_matrix.astype(np.float32),
        buy_cash_limit=5000.0,
        sell_cash_limit=50000.0,
        warmup_rows=0,
        dates=list(DATES),
        symbols=list(symbols),
        execution={
            "model": model,
            "per_symbol_cap": 0.5,
            "total_exposure_cap": 1.0,
            "min_holding_calendar_days": 30,
        },
        entry_events=buy_matrix if model == "target_weight" else None,
        exit_events=sell_matrix if model == "target_weight" else None,
        target_weights=target_weights,
    )


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


def test_cash_plan_uses_live_high_and_is_idempotent():
    manager = RefPortfolioManager()
    market = _market(
        ["601088"],
        np.array([[9.0], [10.0], [10.0]]),
        highs=np.array([[10.0], [10.5], [11.0]]),
        lows=np.array([[8.5], [9.5], [9.0]]),
    )
    buys = np.array([[False], [False], [True]])
    plan = _plan(["601088"], buys)

    updated, trades = manager.rebalance_plan(
        _bound(),
        plan,
        market,
        DATES[-1],
        run_id="run-1",
        strategy_id="percentile",
        lot_size=1,
        commission_rate=0.005,
        force=True,
    )

    assert len(trades) == 1
    assert trades[0].price == 11.0
    assert trades[0].shares == int(5000 / (11.0 * 1.005))
    assert updated.holdings["601088"].last_buy_date == DATES[-1]

    repeated, repeated_trades = manager.rebalance_plan(
        updated,
        plan,
        market,
        DATES[-1],
        run_id="run-1",
        strategy_id="percentile",
        lot_size=1,
        commission_rate=0.005,
        force=True,
    )
    assert repeated_trades == []
    assert repeated.cash == updated.cash
    assert repeated.holdings["601088"].shares == trades[0].shares


def test_cash_plan_respects_session_holding_lock_and_sells_at_low():
    manager = RefPortfolioManager()
    market = _market(
        ["601088"],
        np.array([[10.0], [10.0], [10.0]]),
        highs=np.array([[10.0], [10.0], [10.0]]),
        lows=np.array([[9.0], [9.0], [9.0]]),
    )
    sells = np.array([[False], [False], [True]])
    plan = _plan(["601088"], np.zeros_like(sells), sells)
    portfolio = _bound(cash=0.0)
    portfolio.initial_capital = 1000.0
    portfolio.holdings["601088"] = Holding(
        "601088",
        100,
        8.0,
        DATES[0],
    )

    locked, locked_trades = manager.rebalance_plan(
        portfolio,
        plan,
        market,
        DATES[-1],
        run_id="run-1",
        strategy_id="percentile",
        lot_size=1,
        min_holding_days=3,
        force=True,
    )
    assert locked_trades == []
    assert locked.holdings["601088"].shares == 100

    sold, trades = manager.rebalance_plan(
        portfolio,
        plan,
        market,
        DATES[-1],
        run_id="run-1",
        strategy_id="percentile",
        lot_size=1,
        min_holding_days=2,
        commission_rate=0.0,
        force=True,
    )
    assert len(trades) == 1
    assert trades[0].price == 9.0
    assert not sold.holdings
    assert sold.cash == 900.0


def test_target_weight_plan_allocates_shared_cash_without_symbol_order_bias():
    manager = RefPortfolioManager()
    symbols = ["510300", "601088"]
    closes = np.array([[10.0, 20.0], [10.0, 20.0], [10.0, 20.0]])
    market = _market(symbols, closes)
    entries = np.array(
        [[False, False], [False, False], [True, True]],
        dtype=bool,
    )
    targets = np.zeros_like(closes)
    targets[-1] = [0.5, 0.5]
    plan = _plan(
        symbols,
        entries,
        model="target_weight",
        target_weights=targets,
    )

    updated, trades = manager.rebalance_plan(
        _bound(),
        plan,
        market,
        DATES[-1],
        run_id="run-1",
        strategy_id="percentile",
        lot_size=1,
        commission_rate=0.0,
        force=True,
    )

    assert len(trades) == 2
    assert updated.holdings["510300"].shares == 5000
    assert updated.holdings["601088"].shares == 2500
    assert updated.cash == pytest.approx(0.0)


def test_unbound_legacy_portfolio_cannot_trade():
    manager = RefPortfolioManager()
    market = _market(["601088"], np.ones((3, 1)) * 10)
    buys = np.array([[False], [False], [True]])

    original = RefPortfolio(inception_date=DATES[0])
    updated, trades = manager.rebalance_plan(
        original,
        _plan(["601088"], buys),
        market,
        DATES[-1],
        run_id="run-1",
        strategy_id="percentile",
        force=True,
    )

    assert updated is original
    assert trades == []


def test_legacy_alert_adapter_accepts_strategy_scan_dicts():
    manager = RefPortfolioManager()
    portfolio = RefPortfolio(
        inception_date=DATES[0],
        cash=10000.0,
        initial_capital=10000.0,
    )

    updated, trades = manager.rebalance(
        portfolio,
        [
            {
                "stock_code": "601088",
                "side": "buy",
                "label": "技术策略买入",
            }
        ],
        {"601088": 10.0},
        DATES[-1],
        lot_size=1,
        commission_rate=0.0,
        force=True,
    )

    assert len(trades) == 1
    assert updated.holdings["601088"].shares > 0


def test_live_scan_reads_last_row_from_full_market_trade_plan(monkeypatch):
    import main

    buys = np.array(
        [[False, False], [False, False], [True, False]],
        dtype=bool,
    )
    sells = np.array(
        [[False, False], [False, False], [False, True]],
        dtype=bool,
    )
    plan = _plan(["510300", "601088"], buys, sells)
    monkeypatch.setattr(
        main,
        "build_trade_plan",
        lambda *_args, **_kwargs: (plan, None, None, list(plan.symbols)),
    )
    session = SimpleNamespace(
        _historical={
            "510300": pd.DataFrame({"close": [10.0]}),
            "601088": pd.DataFrame({"close": [20.0]}),
        },
        get_all_dataframe=lambda: pd.DataFrame(
            {"stock_code": ["510300", "601088"]}
        ),
    )
    strategy = SimpleNamespace(name="test_strategy", label="测试策略")

    alerts = main._scan_group(
        session,
        strategy,
        "a_share",
        params=Params({"threshold": 1}),
    )

    assert [(alert["stock_code"], alert["side"]) for alert in alerts] == [
        ("510300", "buy"),
        ("601088", "sell"),
    ]
    assert {alert["signal_date"] for alert in alerts} == {DATES[-1]}
    # 通知契约字段：日报/简报/PDF/飞书统一读取 rule_label + current_value
    for alert in alerts:
        assert alert["rule_label"] == f"测试策略 {alert['side'].upper()}"
        assert alert["current_value"].startswith("评分 ")


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


def test_exact_run_loader_never_falls_back_to_latest_or_legacy(tmp_path):
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

    assert loaded is not None
    assert loaded.run_id == "run-1"
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
        yaml.safe_dump({"optimizer": {}}),
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
    assert all(values["strategy_run_id"] == "run-1" for _, values in resets)
    assert all(values["strategy_id"] == "percentile" for _, values in resets)
    assert all(values["params_hash"] for _, values in resets)
    assert all(values["execution_hash"] for _, values in resets)
