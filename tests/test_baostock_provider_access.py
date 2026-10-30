from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
import requests

from scripts.check_baostock_access import refresh_official
from scripts.fetch_industry_classification import fetch_classifications
from src.data.baostock_access import BaostockAccessBlocked, BaostockAccessController
from src.data.market_history import MarketHistoryProvider
from src.instruments.point_in_time import BaostockStatementProvider


def _config(tmp_path):
    return {
        "provider_access": {
            "baostock": {
                "state_dir": str(tmp_path),
                "min_request_interval_seconds": 0.001,
            }
        }
    }


def test_statements_do_not_query_quarters_after_evaluation_end(tmp_path):
    empty = SimpleNamespace(error_code="0", fields=[], next=lambda: False)
    module = SimpleNamespace(
        login=Mock(return_value=empty),
        logout=Mock(return_value=empty),
        query_profit_data=Mock(return_value=empty),
    )
    config = _config(tmp_path)

    result = BaostockStatementProvider(module, config).fetch(
        "688001", date(2025, 9, 21), date(2026, 9, 21)
    )

    assert [call.args[1:] for call in module.query_profit_data.call_args_list] == [
        (year, quarter)
        for year in (2024, 2025, 2026)
        for quarter in range(1, 5)
        if year < 2026 or quarter <= 2
    ]
    assert len(result.attempts) == 10
    assert BaostockAccessController(config).status()["request_count"] == 12


def test_blacklisted_market_source_does_not_trigger_fallback():
    baostock = Mock()
    baostock.fetch.side_effect = BaostockAccessBlocked("blacklist", "blacklisted")
    yahoo = Mock()
    provider = MarketHistoryProvider(
        {}, baostock_provider=baostock, yahoo_provider=yahoo
    )

    with pytest.raises(BaostockAccessBlocked):
        provider.fetch("688001", date(2021, 9, 21), date(2026, 9, 21))

    yahoo.fetch.assert_not_called()


def test_industry_snapshot_uses_the_shared_breaker(tmp_path, monkeypatch):
    baostock = pytest.importorskip("baostock")
    login = Mock(side_effect=AssertionError("must not connect while blacklisted"))
    monkeypatch.setattr(baostock, "login", login)
    config = _config(tmp_path)
    BaostockAccessController(config).record_blacklist("already denied")

    with pytest.raises(BaostockAccessBlocked):
        fetch_classifications({"688001"}, config=config)

    login.assert_not_called()


def test_query_error_is_not_an_empty_successful_financial_response(tmp_path):
    success = SimpleNamespace(error_code="0")
    failure = SimpleNamespace(error_code="10002007", error_msg="receive failed")
    module = SimpleNamespace(
        login=Mock(return_value=success),
        logout=Mock(return_value=success),
        query_profit_data=Mock(return_value=failure),
    )
    config = _config(tmp_path)

    with pytest.raises(RuntimeError, match="receive failed"):
        BaostockStatementProvider(module, config).fetch(
            "688001", date(2025, 9, 21), date(2026, 9, 21)
        )

    assert module.query_profit_data.call_count == 1
    module.logout.assert_not_called()
    assert BaostockAccessController(config).status()["reason"] == "cooldown"


def test_official_check_uses_direct_ipv4_egress_and_does_not_store_ip(
    tmp_path, monkeypatch
):
    controller = BaostockAccessController(_config(tmp_path))
    controller.record_blacklist("blocked")
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value.json.return_value = {"ip": "203.0.113.5"}
    session.post.return_value.json.return_value = {"stats": {"total": 0, "data": []}}
    monkeypatch.setattr(
        "scripts.check_baostock_access.requests.Session", lambda: session
    )

    status = refresh_official(controller)

    assert not status["blocked"]
    assert session.trust_env is False
    assert session.post.call_args.kwargs["json"] == {"ip": "203.0.113.5"}
    assert "203.0.113.5" not in controller.state_path.read_text(encoding="utf-8")


def test_official_check_failure_keeps_existing_ban(tmp_path, monkeypatch):
    controller = BaostockAccessController(_config(tmp_path))
    controller.record_blacklist("blocked")
    before = controller.state_path.read_bytes()
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.side_effect = requests.Timeout("unavailable")
    monkeypatch.setattr(
        "scripts.check_baostock_access.requests.Session", lambda: session
    )

    with pytest.raises(requests.Timeout):
        refresh_official(controller)

    assert controller.state_path.read_bytes() == before
    session.post.assert_not_called()
