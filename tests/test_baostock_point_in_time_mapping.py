import socket
from datetime import date

import pytest

from src.data.market_history import BaostockMarketHistoryProvider
from src.instruments.point_in_time import (
    BaostockStatementProvider,
    PointInTimeFundamentalStore,
)


def test_baostock_total_share_is_already_reported_in_shares():
    snapshot = BaostockStatementProvider._snapshot(
        {
            "pubDate": "2026-04-25",
            "statDate": "2026-03-31",
            "roeAvg": "0.023971",
            "netProfit": "13312000000.000000",
            "MBRevenue": "",
            "totalShare": "21231768401.00",
            "epsTTM": "2.397",
        },
        quarter=1,
    )

    assert snapshot is not None
    assert snapshot.published_at == date(2026, 4, 25)
    assert snapshot.common_shares_outstanding == pytest.approx(21_231_768_401)
    assert snapshot.reported_roe == pytest.approx(2.3971)
    assert snapshot.diluted_eps == pytest.approx(2.397)
    assert snapshot.period_type == "quarter"
    assert "diluted_eps_contains_baostock_ttm_eps" in snapshot.diagnostics


def test_baostock_fourth_quarter_is_an_annual_statement():
    snapshot = BaostockStatementProvider._snapshot(
        {
            "pubDate": "2026-03-31",
            "statDate": "2025-12-31",
            "roeAvg": "0.12643",
            "netProfit": "62783000000",
            "MBRevenue": "294916000000",
            "totalShare": "19868519955",
            "epsTTM": "3.16",
        },
        quarter=4,
    )

    assert snapshot is not None
    assert snapshot.period_type == "year"
    assert snapshot.is_cumulative
    assert snapshot.diluted_eps == pytest.approx(3.16)


def test_legacy_baostock_q4_is_normalized_and_compacted(tmp_path):
    corrected = BaostockStatementProvider._snapshot(
        {
            "pubDate": "2026-03-31",
            "statDate": "2025-12-31",
            "roeAvg": "0.12643",
            "netProfit": "62783000000",
            "MBRevenue": "294916000000",
            "totalShare": "19868519955",
            "epsTTM": "3.16",
        },
        quarter=4,
    )
    assert corrected is not None
    legacy = corrected.copy(deep=True)
    legacy.period_type = "quarter"
    legacy.diluted_eps = None
    store = PointInTimeFundamentalStore(tmp_path)
    store.upsert("601088", [legacy, corrected])

    stored = store.read_all("601088")
    assert len(stored) == 1
    assert stored[0].period_type == "year"
    assert stored[0].diluted_eps == pytest.approx(3.16)


class _Result:
    error_code = "0"

    def __init__(self, rows):
        self._rows = rows
        self._index = -1
        self.fields = list(rows[0]) if rows else []

    def next(self):
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self):
        row = self._rows[self._index]
        return [row[name] for name in self.fields]


class _DividendModule:
    @staticmethod
    def query_adjust_factor(**_kwargs):
        return _Result([])

    @staticmethod
    def query_dividend_data(*_args, **kwargs):
        if int(kwargs.get("year", 0)) != 2025:
            return _Result([])
        return _Result(
            [
                {
                    "dividPlanAnnounceDate": "2025-03-22",
                    "dividRegistDate": "2025-07-04",
                    "dividOperateDate": "2025-07-07",
                    "dividPayDate": "2025-07-07",
                    "dividCashPsBeforeTax": "2.26",
                    "dividStocksPs": "0.000000",
                    "dividReserveToStockPs": "",
                }
            ]
        )


def test_zero_stock_dividend_remains_cash_only_and_keeps_dates():
    actions = BaostockMarketHistoryProvider()._actions(
        _DividendModule(),
        "601088",
        "sh.601088",
        date(2025, 1, 1),
        date(2025, 12, 31),
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.action_type == "cash_dividend"
    assert action.share_multiplier is None
    assert action.published_at == date(2025, 3, 22)
    assert action.record_date == date(2025, 7, 4)
    assert action.payable_date == date(2025, 7, 7)


class _FailedLogin:
    error_code = "10002007"
    error_msg = "timed out"


class _TimeoutProbeModule:
    def __init__(self):
        self.observed_timeouts = []
        self.logout_called = False

    def login(self):
        self.observed_timeouts.append(socket.getdefaulttimeout())
        return _FailedLogin()

    def logout(self):
        self.observed_timeouts.append(socket.getdefaulttimeout())
        self.logout_called = True


def test_baostock_socket_timeout_is_bounded_and_restored():
    module = _TimeoutProbeModule()
    provider = BaostockMarketHistoryProvider(
        module=module,
        config={
            "point_in_time_data": {
                "market_history": {
                    "baostock_socket_timeout_seconds": 7,
                }
            }
        },
    )
    previous = socket.getdefaulttimeout()

    with pytest.raises(RuntimeError, match="timed out"):
        provider.fetch("601398", date(2020, 1, 1), date(2026, 1, 1))

    assert module.observed_timeouts == [7.0, 7.0]
    assert module.logout_called
    assert socket.getdefaulttimeout() == previous
