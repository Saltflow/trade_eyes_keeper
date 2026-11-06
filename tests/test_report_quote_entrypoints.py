"""Whole-pool report acceptance with real orchestration and offline providers."""

from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

import main
from src.core.data_fetcher import StockDataFetcher
from src.core.ref_portfolio import RefPortfolio, RefPortfolioManager
from src.core.report_quotes import prepare_report_quotes
from src.data.cache_manager import CacheManager
from src.data.data_source import DataSource
from src.data.dividend_events import DailyDividendEventResolver
from src.data.web_crawler import StockWebCrawler
from src.models.schemas import SessionContext
from src.notification.email_notifier import EmailNotifier
from src.notification.feishu_notifier import FeishuNotifier

# Full configured pool recorded in the 2026-11-06 production incident receipt.
# Keep it in source: acceptance must not depend on a developer's .tmp artifacts.
POOL = (
    "000958",
    "002594",
    "00700",
    "00728",
    "00883",
    "00956",
    "01088",
    "01339",
    "01816",
    "09999",
    "159655",
    "508091",
    "510300",
    "510500",
    "512810",
    "518660",
    "520550",
    "588510",
    "600036",
    "601088",
    "601985",
    "BRK.B",
    "C38U.SI",
    "G3B.SI",
    "VOO",
)
CURRENT_POOL = (*POOL, "600750")
NOW = datetime(2026, 11, 6, 9, 55, tzinfo=ZoneInfo("Asia/Shanghai"))
BAD_CODE = "00883"
LOW_PRICE_CODE = "159655"


def history_frame():
    dates = pd.bdate_range(end="2026-11-05", periods=80)
    return pd.DataFrame(
        {
            "date": dates,
            "open": 10.0,
            "close": 10.5,
            "high": 11.0,
            "low": 9.0,
            "volume": 1000.0,
            "amount": 10500.0,
        }
    )


def quote_for(code):
    stamp = "2026-11-06T09:54:30+08:00"
    quote = {
        "stock_code": code,
        "name": "Fixture " + code,
        "open": 20.0,
        "close": 21.0,
        "high": 22.0,
        "low": 19.0,
        "prev_close": 20.5,
        "volume": 2000.0,
        "amount": 42000.0,
        "source": "offline_provider_fixture",
    }
    if code in {"BRK.B", "VOO"}:
        stamp = "2026-11-05T16:00:00-05:00"
    elif code.endswith(".SI"):
        quote.update(
            source="sgx_public_delayed",
            delay_seconds=120,
            timestamp_kind="delayed_market_snapshot",
        )
    if code == LOW_PRICE_CODE:
        quote.update(open=0.049, close=0.05, low=0.04, high=0.06, prev_close=0.051)
    quote.update(date=stamp[:10], quote_time=stamp)
    return quote


@pytest.fixture(params=[POOL, CURRENT_POOL], ids=["incident-25", "current-26"])
def offline_reports(request, monkeypatch, tmp_path):
    pool = request.param
    root = Path(__file__).resolve().parents[1]
    local = yaml.safe_load((root / "config/config.yaml").read_text(encoding="utf-8"))
    # Copy only non-secret execution settings; never inherit live destinations.
    config = {
        "stocks": list(pool),
        "optimizer": deepcopy(local.get("optimizer", {})),
        "alerts": {"config_path": str(root / "config/alerts.yaml")},
        "storage": {
            "data_dir": str(tmp_path / "data"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "scheduler": {
            "timezone": "Asia/Shanghai",
            "brief_reports": [
                {
                    "id": "acceptance",
                    "label": "报价验收简报",
                }
            ],
        },
        "announcements": {
            "enable": False,
            "enable_content_fetching": False,
            "enable_llm_extraction": False,
        },
        "instrument_audit": {"show_in_daily_report": False},
        "report": {"return_curve_artifact": str(tmp_path / "absent-ke.json")},
        "notification": {
            "email": {"enabled": True},
            "feishu": {"enabled": True},
            "telegram": {"enabled": False},
        },
        "email": {"archive_dir": str(tmp_path / "mail")},
    }
    for flag in ("SKIP_NOTIFICATIONS", "SKIP_EMAIL", "SKIP_FEISHU"):
        monkeypatch.delenv(flag, raising=False)
    state = SimpleNamespace(
        pool=pool,
        config=config,
        quotes=Counter(),
        histories=Counter(),
        sessions=[],
        html=[],
        cards=[],
        adapters=[],
        before=[],
        bad_kind="stale",
    )

    def history(_self, code, days=120):
        state.histories[code] += 1
        return history_frame()

    def quote(_self, code):
        state.quotes[code] += 1
        if code == BAD_CODE:
            if state.bad_kind == "failure":
                return None
            return {**quote_for(code), "quote_time": "2026-11-05T16:00:00+08:00"}
        return quote_for(code)

    def freeze_quote_clock(session, provider):
        # Instrument the actual call and inject its public clock port only.
        # Assessment, mutation, Session and valuation adapter remain real.
        assert isinstance(session, SessionContext)
        state.sessions.append(session)
        state.before.append(
            {code: frame.copy(deep=True) for code, frame in session._historical.items()}
        )
        adapter = prepare_report_quotes(session, provider, clock=lambda: NOW)
        state.adapters.append(adapter)
        return adapter

    monkeypatch.setattr(DataSource, "fetch_stock_data", history)
    monkeypatch.setattr(StockWebCrawler, "fetch_realtime_quote", quote)
    monkeypatch.setattr(StockWebCrawler, "fetch_placement_data", lambda *a, **k: None)
    monkeypatch.setattr(
        StockWebCrawler, "fetch_dividend_data", lambda *a: {"dividend_per_share": 0.01}
    )
    monkeypatch.setattr(
        StockWebCrawler,
        "fetch_valuation_data",
        lambda *a: {"pe_ratio": 8.0, "pb_ratio": 1.2},
    )
    monkeypatch.setattr(
        CacheManager, "get_latest_llm_extraction_for_stock", lambda *a, **k: None
    )
    monkeypatch.setattr(
        DailyDividendEventResolver,
        "public_schedule",
        lambda *a, **k: {"events": [], "coverage": []},
    )
    # The real manager sees an uninitialized portfolio, so no user files change.
    monkeypatch.setattr(RefPortfolioManager, "load", lambda self: RefPortfolio())
    monkeypatch.setattr(main, "prepare_report_quotes", freeze_quote_clock)
    monkeypatch.setattr(EmailNotifier, "_get_server_info", lambda self: {})
    monkeypatch.setattr(
        "src.notification.daily_pdf._compile_pdf", lambda path: b"%PDF-offline"
    )

    def send_email(_self, subject, body, **kwargs):
        state.html.append(body)
        return {"status": "sent", "reason": "offline_transport_acknowledged"}

    def send_card(_self, payload):
        state.cards.append(payload)
        return {"status": "sent", "reason": "offline_transport_acknowledged"}

    monkeypatch.setattr(EmailNotifier, "_send_email", send_email)
    monkeypatch.setattr(FeishuNotifier, "_post_card", send_card)
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        lambda *a, **k: pytest.fail("report acceptance attempted live HTTP"),
    )
    return state


def card_rows(cards):
    for card in cards:
        for element in card["card"]["body"]["elements"]:
            if element.get("tag") == "table":
                labels = {
                    column["name"]: column["display_name"]
                    for column in element["columns"]
                }
                for row in element["rows"]:
                    yield {labels[key]: value for key, value in row.items()}


@pytest.mark.parametrize("kind", ["daily", "brief"])
@pytest.mark.parametrize("bad_kind", ["stale", "failure"])
def test_whole_pool_entrypoints_refresh_and_deliver_honest_partial_reports(
    offline_reports,
    kind,
    bad_kind,
):
    state = offline_reports
    pool = state.pool
    state.bad_kind = bad_kind
    if kind == "daily":
        result = main.run_daily_task(force=True, config=state.config)
    else:
        result = main.run_brief_report("acceptance", force=True, config=state.config)

    assert result.status == "partial", result.to_dict()
    assert result.reason.startswith("data_not_ready")
    assert {key: value["status"] for key, value in result.deliveries.items()} == {
        "email": "sent",
        "feishu": "sent",
    }
    assert len(state.sessions) == 1
    session = state.sessions[0]
    assert set(session.stocks_data) == set(pool)
    assert len(session.stocks_data) == len(pool)
    assert state.histories == Counter({code: 1 for code in pool})
    assert state.quotes == Counter({code: 1 for code in pool})
    readiness = result.artifacts["data_readiness"]
    assert readiness["available_codes"] == sorted(pool)
    assert readiness["missing_codes"] == []
    assert set(readiness["quote_issues"]) == {BAD_CODE}
    assert readiness["quote_counts"] == {
        "realtime": len(pool) - 5,
        "delayed": 2,
        "last_close": 2,
        "unavailable": 1,
    }
    assert session.stocks_data["601088"].close == 21.0
    assert session.stocks_data[LOW_PRICE_CODE].close == 0.05
    assert session.stocks_data[BAD_CODE].close is None
    assert session.stocks_data[BAD_CODE].pe_ratio == 8.0
    assert session.stocks_data[BAD_CODE].dividend_per_share == 0.01
    for code in pool:
        pd.testing.assert_frame_equal(session._historical[code], state.before[0][code])
        assert session._historical[code].iloc[-1]["date"] == pd.Timestamp("2026-11-05")
        assert session._historical[code].iloc[-1]["close"] == 10.5

    # Real HTML and native Bot tables preserve exactly the configured universe.
    assert len(state.html) == 1
    html_rows = BeautifulSoup(state.html[0], "html.parser").find_all("tr")
    rendered = {
        code: next(
            row.get_text(" ", strip=True)
            for row in html_rows
            if any(
                cell.get_text(strip=True) == code
                or any(
                    node.get_text(strip=True) == code
                    for node in cell.find_all("strong")
                )
                for cell in row.find_all("td", recursive=False)
            )
        )
        for code in pool
    }
    native = {row["代码"]: row for row in card_rows(state.cards)}
    assert set(native) == set(pool)
    for code, label in (
        ("601088", "实时"),
        ("C38U.SI", "延迟采样"),
        ("VOO", "最近收盘"),
        (BAD_CODE, "缺口"),
    ):
        assert label in rendered[code]
        assert label in native[code]["行情状态 / 截至"]
    assert "2026-11-06 01:54:30+00:00" in rendered["601088"]
    assert "2026-11-05 21:00:00+00:00" in native["VOO"]["行情状态 / 截至"]
    assert "0.05" in rendered[LOW_PRICE_CODE]
    assert native[LOW_PRICE_CODE]["报价"] == "0.05"
    assert native[BAD_CODE]["报价"] == "—"
    assert native[BAD_CODE]["PE"] == "8.00"

    # A reference-portfolio fallback receives the same denied result, even if
    # asked repeatedly; it cannot retry the configured failure into a new price.
    adapter = state.adapters[0]
    assert adapter(BAD_CODE) is None and adapter(BAD_CODE) is None
    assert state.quotes[BAD_CODE] == 1
    assert StockDataFetcher.fetch_to_session.__module__ == "src.core.data_fetcher"
