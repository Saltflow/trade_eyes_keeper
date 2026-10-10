"""Replay blocked/failed live scans without market or outbound calls."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import main
from src.notification.email_notifier import EmailNotifier


@pytest.fixture
def scan_fixture(monkeypatch):
    codes = ["600036", "09999", "VOO"]
    session = SimpleNamespace(
        get_all_dataframe=lambda: pd.DataFrame({"stock_code": codes}),
        _historical={code: pd.DataFrame({"close": [1.0]}) for code in codes},
    )
    strategy = SimpleNamespace(
        name="test", label="测试策略", supports_market=lambda group: True
    )
    monkeypatch.setattr(main, "_strategy_context_enricher", lambda *args: None)
    monkeypatch.setattr(main, "load_latest_strategy_run", lambda **kwargs: None)
    config = {"stocks": codes}
    return session, strategy, config


def plan_for(codes, *, trigger=False):
    shape = (1, len(codes))
    return SimpleNamespace(
        dates=["2026-10-09"],
        symbols=codes,
        entry_events=np.full(shape, trigger),
        exit_events=np.zeros(shape, dtype=bool),
        force_exit_signals=None,
        buy_priority=np.ones(shape),
        sell_priority=np.ones(shape),
    )


def notifier(config):
    instance = EmailNotifier.__new__(EmailNotifier)
    instance.config = config
    return instance


@pytest.mark.parametrize("us_trigger", [False, True])
def test_market_failure_keeps_other_market_signal_and_reason(
    scan_fixture, monkeypatch, us_trigger
):
    session, strategy, config = scan_fixture

    def build(history, codes, *args, **kwargs):
        if codes == ["09999"]:
            raise ValueError("09999 listing_date_unavailable")
        return (
            plan_for(codes, trigger=us_trigger and codes == ["VOO"]),
            None,
            None,
            codes,
        )

    monkeypatch.setattr(main, "build_trade_plan", build)
    scan = main._collect_signal_scan(
        session,
        dict.fromkeys(main.OPTIMIZER_GROUPS, strategy),
        {group: {} for group in main.OPTIMIZER_GROUPS},
        config,
    )
    assert scan.group_outcomes["hk"]["status"] == "failed"
    assert scan.group_outcomes["a_share"]["status"] == "completed_no_signal"
    assert scan.group_outcomes["us"]["status"] == (
        "completed" if us_trigger else "completed_no_signal"
    )
    assert [alert["stock_code"] for alert in scan.alerts] == (
        ["VOO"] if us_trigger else []
    )
    view = notifier(config)
    html = view._build_daily_signal_section(scan, pd.DataFrame())
    assert "港股：扫描失败" in html
    assert "09999 listing_date_unavailable" in html
    if us_trigger:
        assert "VOO" in html
    assert "本次扫描无触发信号" not in html
    assert "扫描未完成" in view._build_daily_summary_section([], [], scan)
    cards = view._build_daily_strategy_section({}, signal_scan=scan)
    assert "扫描失败" in cards


@pytest.mark.parametrize(
    "mode,status",
    [
        ("no_strategy", "not_run"),
        ("unsupported", "not_run"),
        ("no_params", "blocked"),
        ("no_history", "blocked"),
        ("empty_plan", "blocked"),
        ("failed", "failed"),
    ],
)
def test_noncompletion_never_becomes_no_signal(scan_fixture, monkeypatch, mode, status):
    session, strategy, config = scan_fixture
    if mode == "no_strategy":
        strategy = None
    elif mode == "unsupported":
        strategy.supports_market = lambda group: False
    elif mode == "no_history":
        session._historical = {}

    def build(*args, **kwargs):
        if mode == "failed":
            raise RuntimeError("TradePlan unavailable <detail>")
        return None, None, None, []

    monkeypatch.setattr(main, "build_trade_plan", build)
    outcomes = {}
    alerts = main._scan_group(
        session,
        strategy,
        "hk",
        None if mode == "no_params" else {},
        config=config,
        scan_outcomes=outcomes,
    )
    assert alerts == []
    assert outcomes["hk"]["status"] == status
    scan = SimpleNamespace(alerts=alerts, group_outcomes=outcomes)
    html = notifier(config)._build_daily_signal_section(scan, pd.DataFrame())
    assert "本次扫描无触发信号" not in html
    assert "不能据此判断全部市场无信号" in html
    assert "<detail>" not in html


def test_partial_history_does_not_claim_complete(scan_fixture, monkeypatch):
    session, strategy, config = scan_fixture
    config["stocks"].append("00700")
    monkeypatch.setattr(
        main,
        "build_trade_plan",
        lambda history, codes, *a, **k: (plan_for(codes), None, None, codes),
    )
    outcomes = {}
    main._scan_group(session, strategy, "hk", {}, config=config, scan_outcomes=outcomes)
    assert outcomes["hk"]["status"] == "blocked"
    assert outcomes["hk"]["missing_codes"] == ["00700"]
    assert outcomes["hk"]["scanned_codes"] == ["09999"]


def test_complete_zero_signals_is_explicit(scan_fixture, monkeypatch):
    session, strategy, config = scan_fixture
    monkeypatch.setattr(
        main,
        "build_trade_plan",
        lambda history, codes, *a, **k: (plan_for(codes), None, None, codes),
    )
    scan = main._collect_signal_scan(
        session,
        dict.fromkeys(main.OPTIMIZER_GROUPS, strategy),
        {group: {} for group in main.OPTIMIZER_GROUPS},
        config,
    )
    assert {x["status"] for x in scan.group_outcomes.values()} == {
        "completed_no_signal"
    }
    html = notifier(config)._build_daily_signal_section(scan, pd.DataFrame())
    assert "本次扫描无触发信号" in html
    assert "扫描未全部完成" not in html


def test_legacy_empty_result_is_not_completion_evidence(scan_fixture):
    _, _, config = scan_fixture
    html = notifier(config)._build_daily_signal_section(
        SimpleNamespace(alerts=[]), pd.DataFrame()
    )
    assert "缺少扫描完成状态" in html
    assert "本次扫描无触发信号" not in html


def test_failure_outside_trade_plan_does_not_abort_other_markets(
    scan_fixture, monkeypatch
):
    session, strategy, config = scan_fixture

    def supports_market(group):
        if group == "hk":
            raise ValueError("invalid active strategy contract")
        return True

    strategy.supports_market = supports_market
    monkeypatch.setattr(
        main,
        "build_trade_plan",
        lambda history, codes, *a, **k: (plan_for(codes), None, None, codes),
    )
    scan = main._collect_signal_scan(
        session,
        dict.fromkeys(main.OPTIMIZER_GROUPS, strategy),
        {group: {} for group in main.OPTIMIZER_GROUPS},
        config,
    )
    assert scan.group_outcomes["hk"]["status"] == "failed"
    assert "invalid active strategy contract" in scan.group_outcomes["hk"]["reason"]
    assert scan.group_outcomes["a_share"]["status"] == "completed_no_signal"
    assert scan.group_outcomes["us"]["status"] == "completed_no_signal"


@pytest.mark.parametrize("skip", [False, True])
def test_unrequested_markets_are_not_applicable(scan_fixture, monkeypatch, skip):
    session, strategy, config = scan_fixture
    config = {"stocks": ["600036", "09999"] if skip else ["600036"]}
    monkeypatch.setattr(
        main,
        "build_trade_plan",
        lambda history, codes, *a, **k: (plan_for(codes), None, None, codes),
    )
    scan = main._collect_signal_scan(
        session,
        {"a_share": strategy},
        {"a_share": {}},
        config,
        skip_codes={"09999"} if skip else None,
    )
    assert scan.group_outcomes["hk"]["status"] == "not_applicable"
    assert scan.group_outcomes["us"]["status"] == "not_applicable"
    html = notifier(config)._build_daily_signal_section(scan, pd.DataFrame())
    assert "本次扫描无触发信号" in html
    assert "扫描未全部完成" not in html
    assert "无需扫描" in html


def test_trade_plan_dropped_symbol_is_not_scan_completion(scan_fixture, monkeypatch):
    session, strategy, config = scan_fixture
    config["stocks"].append("00700")
    session._historical["00700"] = pd.DataFrame({"close": [1.0]})
    monkeypatch.setattr(
        main,
        "build_trade_plan",
        lambda *a, **k: (plan_for(["09999"], trigger=True), None, None, ["09999"]),
    )
    outcomes = {}
    alerts = main._scan_group(
        session, strategy, "hk", {}, config=config, scan_outcomes=outcomes
    )
    assert outcomes["hk"]["status"] == "blocked"
    assert outcomes["hk"]["missing_codes"] == ["00700"]
    assert outcomes["hk"]["scanned_codes"] == ["09999"]
    assert outcomes["hk"]["available_codes"] == ["00700", "09999"]
    assert alerts
    html = notifier(config)._build_daily_signal_section(
        SimpleNamespace(alerts=alerts, group_outcomes=outcomes), pd.DataFrame()
    )
    assert "覆盖1/2只" in html
    assert "实际扫描子集" in html


def test_no_requested_symbols_does_not_claim_a_scan_ran(scan_fixture):
    session, _, _ = scan_fixture
    config = {"stocks": []}
    scan = main._collect_signal_scan(session, {}, {}, config)
    html = notifier(config)._build_daily_signal_section(scan, pd.DataFrame())
    assert "本次无待扫描标的" in html
    assert "本次扫描无触发信号" not in html
    assert "扫描未全部完成" not in html


def test_incompatible_active_contract_is_visible_as_blocked(scan_fixture, monkeypatch):
    session, _, config = scan_fixture

    def incompatible(*, groups, compatibility_diagnostics):
        compatibility_diagnostics[groups[0]] = {
            "status": "incompatible_execution_contract",
            "reason": "港股旧产物缺少现金币种转换合同证据，需要按当前合同重新优化",
            "requires_reoptimization": True,
        }

    monkeypatch.setattr(main, "load_latest_strategy_run", incompatible)
    outcomes = {}
    alerts = main._scan_group(
        session, None, "hk", config=config, scan_outcomes=outcomes
    )
    assert alerts == []
    assert outcomes["hk"]["status"] == "blocked"
    assert "重新优化" in outcomes["hk"]["reason"]
    html = notifier(config)._build_daily_signal_section(
        SimpleNamespace(alerts=[], group_outcomes=outcomes), pd.DataFrame()
    )
    assert "港股：扫描受阻" in html
    assert "重新优化" in html
    assert "本次扫描无触发信号" not in html
