"""Real report entrypoints with offline sources, fake portfolios and no sends."""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

import main
from src.models.schemas import SessionContext
from src.strategy.return_curves import CURVE_CONTRACT, load_return_curves

TODAY = date(2026, 11, 1)
UNIVERSE = ["601088", "00700", "AAPL"]


def payload(today=TODAY):
    return {
        "contract": CURVE_CONTRACT,
        "strategy_id": "ke_threshold_allocation",
        "universe": UNIVERSE,
        "generated_at": f"{today.isoformat()}T00:00:00+00:00",
        "markets": {
            group: {
                "dates": [(today - timedelta(days=1)).isoformat(), today.isoformat()],
                "values": [1000, 1010],
                "initial_capital": 1000,
                "coverage": {"status": "complete"},
                "data_as_of": today.isoformat(),
            }
            for group in ("a_share", "hk", "us")
        },
    }


def config_for(path):
    return {
        "stocks": UNIVERSE,
        "report": {
            "return_curve_artifact": str(path),
        },
    }


def write_payload(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def assert_unavailable(value):
    assert value["strategy_id"] == "ke_threshold_allocation"
    assert all(
        item["coverage"]["status"] == "unavailable"
        for item in value["markets"].values()
    )
    assert all("values" not in item for item in value["markets"].values())


def test_loader_and_session_field_accept_exact_explicit_ke_artifact(tmp_path):
    path = tmp_path / "ke.json"
    original = payload()
    write_payload(path, original)
    loaded = load_return_curves(config_for(path), today=TODAY)
    assert loaded == original
    context = SessionContext(session_id="synthetic", strategy_return_curves=loaded)
    assert context.strategy_return_curves == original


@pytest.mark.parametrize("path", [True, {"unexpected": "path"}])
def test_invalid_curve_path_config_is_reported_without_raising(path):
    config = config_for("unused")
    config["report"]["return_curve_artifact"] = path
    loaded = load_return_curves(config, today=TODAY)
    assert_unavailable(loaded)
    assert loaded["markets"]["a_share"]["coverage"]["message"] == "Ke曲线路径配置无效"


@pytest.mark.parametrize(
    "corrupt, failed_markets",
    [
        (
            lambda v: v.update(strategy_id="old_technical_strategy"),
            {"a_share", "hk", "us"},
        ),
        (lambda v: v.update(universe=UNIVERSE[:-1]), {"us"}),
        (lambda v: v.update(universe=["601088", None]), {"a_share", "hk", "us"}),
        (
            lambda v: v.update(generated_at="2026-10-01T00:00:00+00:00"),
            {"a_share", "hk", "us"},
        ),
        (
            lambda v: v.update(generated_at="2026-11-02T00:00:00+00:00"),
            {"a_share", "hk", "us"},
        ),
        (
            lambda v: v["markets"]["a_share"].update(values=[1000, float("nan")]),
            {"a_share"},
        ),
        (lambda v: v["markets"]["hk"].update(initial_capital=0), {"hk"}),
        (lambda v: v["markets"]["us"].update(data_as_of="2026-10-01"), {"us"}),
        (lambda v: v["markets"].pop("hk"), {"hk"}),
    ],
)
def test_invalid_curves_fail_only_at_their_own_scope(tmp_path, corrupt, failed_markets):
    path = tmp_path / "ke.json"
    original = payload()
    corrupt(original)
    write_payload(path, original)
    loaded = load_return_curves(config_for(path), today=TODAY)
    for group, item in loaded["markets"].items():
        if group in failed_markets:
            assert item["coverage"]["status"] == "unavailable"
            assert item["coverage"]["message"]
            assert "values" not in item
        else:
            assert item == original["markets"][group]


@pytest.mark.parametrize("value", [None, [], 1, "synthetic corrupted JSON"])
def test_malformed_json_root_is_unavailable_not_a_report_exception(tmp_path, value):
    path = tmp_path / "ke.json"
    write_payload(path, value)
    assert_unavailable(load_return_curves(config_for(path), today=TODAY))


@pytest.mark.parametrize("location", ["markets", "item", "coverage"])
def test_malformed_nested_curve_shape_fails_closed(tmp_path, location):
    path = tmp_path / "ke.json"
    original = payload()
    if location == "markets":
        original["markets"] = []
    elif location == "item":
        original["markets"]["hk"] = []
    else:
        original["markets"]["hk"]["coverage"] = []
    write_payload(path, original)
    loaded = load_return_curves(config_for(path), today=TODAY)
    if location == "markets":
        assert_unavailable(loaded)
    else:
        assert loaded["markets"]["hk"]["coverage"]["status"] == "unavailable"
        assert loaded["markets"]["a_share"] == original["markets"]["a_share"]
        assert loaded["markets"]["us"] == original["markets"]["us"]


@pytest.mark.parametrize("recorded_sgx,current_sgx", [(False, True), (True, False)])
def test_sgx_universe_changes_keep_original_three_market_curves(
    tmp_path, recorded_sgx, current_sgx
):
    path = tmp_path / "ke.json"
    original = payload()
    sgx = ["C38U.SI", "G3B-SI"]
    original["universe"] = UNIVERSE + (sgx if recorded_sgx else [])
    write_payload(path, original)
    config = config_for(path)
    config["stocks"] = UNIVERSE + (sgx if current_sgx else [])
    loaded = load_return_curves(config, today=TODAY)
    assert loaded == original  # Real simulated universe and values remain unchanged.


def test_same_market_addition_invalidates_only_that_market_with_clear_reason(tmp_path):
    path = tmp_path / "ke.json"
    original = payload()
    write_payload(path, original)
    config = config_for(path)
    config["stocks"] = UNIVERSE + ["01088"]
    loaded = load_return_curves(config, today=TODAY)
    assert "监控标的已变更" in loaded["markets"]["hk"]["coverage"]["message"]
    assert loaded["markets"]["a_share"] == original["markets"]["a_share"]
    assert loaded["markets"]["us"] == original["markets"]["us"]


def test_expired_market_observations_do_not_hide_other_markets(tmp_path):
    path = tmp_path / "ke.json"
    original = payload()
    old = "2026-10-01"
    original["markets"]["hk"].update(dates=[old, "2026-10-02"], data_as_of="2026-10-02")
    write_payload(path, original)
    loaded = load_return_curves(config_for(path), today=TODAY)
    assert "行情截至2026-10-02" in loaded["markets"]["hk"]["coverage"]["message"]
    assert "7天有效期" in loaded["markets"]["hk"]["coverage"]["message"]
    assert loaded["markets"]["a_share"] == original["markets"]["a_share"]
    assert loaded["markets"]["us"] == original["markets"]["us"]


@pytest.fixture
def offline_report(monkeypatch, tmp_path):
    path = tmp_path / "ke.json"
    now = datetime.now(timezone.utc).date()
    write_payload(path, payload(now))
    config = config_for(path)
    trigger = {
        "mode": "any",
        "conditions": [{"path": "announcements", "operator": "non_empty"}],
    }
    config.update(
        announcements={"enable": True},
        instrument_audit={"show_in_daily_report": False},
        scheduler={
            "timezone": "Asia/Shanghai",
            "brief_reports": [{"id": "fixture", "triggers": trigger}],
        },
    )
    frame = pd.DataFrame(
        [
            {"stock_code": code, "stock_name": "Synthetic fixture", "close": 10}
            for code in UNIVERSE
        ]
    )
    session = SimpleNamespace(
        session_id="offline-ke-report",
        config=config,
        stocks_data={code: object() for code in UNIVERSE},
        errors=[],
        announcements={},
        placements={},
        dividend_events=[],
        _historical={},
        get_all_dataframe=lambda: frame,
    )
    manager = Mock()
    manager.create_session.return_value = session
    monkeypatch.setattr(main, "SessionManager", Mock(return_value=manager))
    fetcher = Mock()
    monkeypatch.setattr(main, "StockDataFetcher", Mock(return_value=fetcher))
    announcement = {
        "title": "Synthetic official report",
        "date": now.isoformat(),
        "url": "https://disclosure.example.test/fixture.pdf",
    }
    announcements = Mock()
    announcements.get_recent_important_announcements.return_value = {
        "601088": [announcement]
    }
    monkeypatch.setattr(main, "AnnouncementFetcher", Mock(return_value=announcements))
    monkeypatch.setattr(
        "src.data.announcement_fetcher.AnnouncementFetcher",
        Mock(return_value=announcements),
    )
    monkeypatch.setattr("src.data.web_crawler.StockWebCrawler", Mock())
    resolver = Mock()
    resolver.resolve.return_value = [{"code": "601088", "event_type": "cash_dividend"}]
    monkeypatch.setattr(
        "src.data.dividend_events.DailyDividendEventResolver",
        Mock(return_value=resolver),
    )

    forbidden = {}
    for name in (
        "evaluate_all_groups",
        "load_latest_strategy_run",
    ):
        forbidden[name] = Mock(
            side_effect=AssertionError(f"retired technical path: {name}")
        )
        module = (
            "src.backtest" if name == "evaluate_all_groups" else "src.search.artifacts"
        )
        monkeypatch.setattr(module + "." + name, forbidden[name])
    execution = SimpleNamespace(
        lot_sizes={"a_share": 100, "hk": 100, "us": 1},
        fx_rates={"a_share": 1, "hk": 0.9, "us": 7},
        withholding_rates={"a_share": 0, "hk": 0.2, "us": 0},
    )
    monkeypatch.setattr(
        main,
        "get_market_optimizer_configs",
        lambda *args, **kwargs: {
            group: SimpleNamespace(execution=execution)
            for group in ("a_share", "hk", "us")
        },
    )
    portfolio = SimpleNamespace()
    accounts = Mock(spec=main.RefPortfolioManager)
    accounts.load.return_value = portfolio
    accounts.is_initialized.return_value = True
    accounts.apply_corporate_actions.return_value = (portfolio, [])
    accounts.get_status.side_effect = lambda *args: {
        "nav": 1000,
        "nav_return_pct": 0,
        "valuation_reason": "",
    }
    monkeypatch.setattr(main, "RefPortfolioManager", Mock(return_value=accounts))
    monkeypatch.setattr(main, "_reference_portfolio_actions", lambda *args: [])
    monkeypatch.setattr(main, "_reference_portfolio_external_prices", lambda *args: {})
    monkeypatch.setattr(main, "_reference_portfolio_prices", lambda *args, **kwargs: {})
    notifier = Mock()
    for method in (
        "send_from_session",
        "send_daily_report_from_session",
        "send_brief_report",
    ):
        getattr(notifier, method).return_value = {
            "status": "succeeded",
            "deliveries": {"fixture": {"status": "sent"}},
        }
    monkeypatch.setattr(main, "NotifierManager", Mock(return_value=notifier))
    snapshots = Mock()
    monkeypatch.setattr("src.triggers.snapshots.capture_report_snapshot", snapshots)
    return SimpleNamespace(
        config=config,
        session=session,
        forbidden=forbidden,
        accounts=accounts,
        notifier=notifier,
        snapshots=snapshots,
        announcements=announcements,
    )


@pytest.mark.parametrize("kind", ["daily", "brief"])
def test_real_report_entrypoints_skip_technical_calls_but_keep_events_snapshots_and_ledger(
    offline_report, kind
):
    fixture = offline_report
    if kind == "daily":
        fixture.config["_trigger_context"] = {
            "kind": "daily",
            "events": [
                {
                    "symbol": "00700",
                    "title": "Synthetic matched Q3 disclosure",
                    "url": "https://disclosure.example.test/trigger-q3.pdf",
                    "published_at": "2026-11-01T00:00:00+00:00",
                    "publication_date": "2026-11-01",
                }
            ],
        }
        result = main.run_daily_task(force=True, config=fixture.config)
        fixture.notifier.send_daily_report_from_session.assert_called_once_with(
            fixture.session
        )
        assert (
            fixture.session.announcements["00700"][0]["title"]
            == "Synthetic matched Q3 disclosure"
        )
    else:
        result = main.run_brief_report("fixture", force=True, config=fixture.config)
        fixture.notifier.send_brief_report.assert_called_once()
        assert all(
            not item["_trading_blocked_reason"]
            for item in fixture.session.ref_portfolio_status.values()
        )
    assert result.status == "succeeded"
    assert (
        fixture.session.strategy_return_curves["strategy_id"]
        == "ke_threshold_allocation"
    )
    assert fixture.session.announcements["601088"][0]["url"].endswith("fixture.pdf")
    fixture.snapshots.assert_called_once_with(fixture.config, fixture.session, kind)
    assert fixture.accounts.apply_corporate_actions.call_count == 3
    assert fixture.accounts.save.call_count == 3
    for forbidden in fixture.forbidden.values():
        forbidden.assert_not_called()
