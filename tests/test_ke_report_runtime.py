"""Real report entrypoints with offline sources, fake portfolios and no sends."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

import main

UNIVERSE = ["601088", "00700", "AAPL"]


@pytest.fixture
def offline_report(monkeypatch, tmp_path):
    path = tmp_path / "ke.json"
    now = datetime.now(timezone.utc).date()
    config = {"stocks": UNIVERSE, "report": {"return_curve_artifact": str(path)}}
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
    # Exercise the real report quote contract; the Ke fixture supplies only
    # external quote data, with a valid source time for the actual market state.
    def provider_quote(code):
        from src.data.market_calendar import (
            resolve_market_data_cutoff,
            resolve_market_session,
        )

        observed = datetime.now(timezone.utc)
        market_session = resolve_market_session(code, as_of=observed)
        if market_session and market_session.is_open(observed):
            source_time = observed
        elif (
            market_session and market_session.break_start
            and market_session.break_start <= observed < market_session.break_end
        ):
            source_time = market_session.break_start
        else:
            source_time = resolve_market_data_cutoff(
                code, observed.date() + timedelta(days=1), as_of=observed
            ).session_close
        return {
            "quote_time": source_time.isoformat(), "close": 10,
            "open": 10, "prev_close": 10, "high": 10, "low": 10,
        }

    fetcher.web_crawler.fetch_realtime_quote.side_effect = provider_quote
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
@pytest.mark.parametrize("legacy_rules", [False, True])
def test_real_report_entrypoints_skip_technical_calls_but_keep_events_snapshots_and_ledger(
    offline_report, kind, legacy_rules
):
    fixture = offline_report
    if legacy_rules:
        fixture.config["scheduler"]["daily_report_frequency"] = "off"
        fixture.config["scheduler"]["daily_report_triggers"] = {"invalid": "retired"}
        fixture.config["scheduler"]["brief_reports"][0]["triggers"] = {"invalid": "retired"}
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
    assert not hasattr(fixture.session, "strategy_return_curves")
    if kind == "daily":
        assert fixture.session.announcements["601088"][0]["url"].endswith("fixture.pdf")
    else:
        # Retired brief predicates cannot cause hidden event acquisition.
        assert fixture.session.announcements == {}
        fixture.announcements.get_recent_important_announcements.assert_not_called()
    fixture.snapshots.assert_called_once_with(fixture.config, fixture.session, kind)
    assert fixture.accounts.apply_corporate_actions.call_count == 3
    assert fixture.accounts.save.call_count == 3
    for forbidden in fixture.forbidden.values():
        forbidden.assert_not_called()
