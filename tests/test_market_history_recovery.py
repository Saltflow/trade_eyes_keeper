from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.data.backtest_data import validate_market_bundle
from src.data.market_history import BaostockMarketHistoryProvider, CorporateAction
from src.data.price_adjustments import apply_disclosed_adjustments
from src.data.tencent_market_history import TencentMarketHistoryProvider


class _Result:
    fields = ("date", "open", "high", "low", "close", "volume", "amount", "tradestatus")
    error_code = "0"

    def __init__(self, status):
        self.rows = iter([["2026-09-21", "10", "10", "10", "10", "", "", status]])

    def next(self):
        self.row = next(self.rows, None)
        return self.row is not None

    def get_row_data(self):
        return self.row


@pytest.mark.parametrize("status,expected", [("0", 0.0), ("1", None), ("", None)])
def test_only_explicit_baostock_suspensions_have_zero_turnover(status, expected):
    class Module:
        def query_history_k_data_plus(self, *args, **kwargs):
            return _Result(status)

    frame = BaostockMarketHistoryProvider()._prices(
        Module(), "sh.601088", date(2026, 9, 21), date(2026, 9, 21), adjustflag="3"
    )
    for field in ("volume", "amount"):
        if expected is None:
            assert pd.isna(frame.loc[0, field])
        else:
            assert frame.loc[0, field] == expected
    assert bool(frame.loc[0, "tradable"]) == (status == "1")


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class _TencentHttp:
    def __init__(self, *, omit_qfq_date=False, mismatch_volume=False):
        self.calls = []
        self.omit_qfq_date = omit_qfq_date
        self.mismatch_volume = mismatch_volume

    def get(self, _url, *, params, **kwargs):
        self.calls.append(params["param"])
        symbol, _, start, end, _, adjustment = params["param"].split(",")
        rows = [
            ["2026-09-21", "10", "10", "11", "9", "100"],
            ["2026-09-22", "4.5", "4.5", "5", "4", "200"],
            ["2026-09-23", "4.7", "4.7", "5", "4.5", "300"],
        ]
        rows = [row for row in rows if start <= row[0] <= end]
        if adjustment:
            if self.omit_qfq_date:
                rows = rows[:-1]
            if self.mismatch_volume:
                rows[0][-1] = "999"
        return _Response(
            {"code": 0, "data": {symbol: {"qfqday" if adjustment else "day": rows}}}
        )


def _tencent(http=None):
    provider = TencentMarketHistoryProvider({}, http=http or _TencentHttp())
    provider.interval = 0
    return provider


def _price_bundle():
    return _tencent().fetch("510300", date(2026, 9, 21), date(2026, 9, 23))


def test_tencent_price_only_bundle_cannot_pass_preflight():
    bundle = _price_bundle()
    assert bundle.prices.volume.tolist() == [10000, 20000, 30000]
    with pytest.raises(ValueError, match="unresolved corporate action"):
        validate_market_bundle(bundle, "510300", date(2026, 9, 21), date(2026, 9, 23))


@pytest.mark.parametrize(
    "option,reason",
    [
        ("omit_qfq_date", "dates do not align"),
        ("mismatch_volume", "volume differs"),
    ],
)
def test_tencent_rejects_incompatible_raw_and_adjusted_windows(option, reason):
    with pytest.raises(ValueError, match=reason):
        _tencent(_TencentHttp(**{option: True})).fetch(
            "510300", date(2026, 9, 21), date(2026, 9, 23)
        )


def _distribution(**changes):
    values = {
        "code": "510300",
        "action_type": "cash_and_stock_dividend",
        "ex_date": date(2026, 9, 22),
        "published_at": date(2026, 9, 18),
        "cash_per_share": 1.0,
        "share_multiplier": 2.0,
        "source": "disclosure",
    }
    values.update(changes)
    return CorporateAction(**values)


def test_disclosed_cash_and_split_rebuild_qfq_without_double_counting():
    raw_bundle = _price_bundle()
    result = apply_disclosed_adjustments(
        raw_bundle, [_distribution()], evidence="source-url"
    )
    assert np.allclose(result.prices.qfq_factor, [0.45, 1.0, 1.0])
    assert np.allclose(result.prices.qfq_close, [4.5, 4.5, 4.7])
    assert raw_bundle.actions[0].action_type == "action_coverage_unverified"
    validate_market_bundle(result, "510300", date(2026, 9, 21), date(2026, 9, 23))


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"published_at": None}, "causal publication"),
        ({"rights_price": 5}, "rights issue"),
        ({"cash_per_share": 11}, "exceeds prior close"),
        ({"code": "different"}, "different instrument"),
    ],
)
def test_bad_disclosures_cannot_make_prices_eligible(changes, reason):
    with pytest.raises(ValueError, match=reason):
        apply_disclosed_adjustments(
            _price_bundle(), [_distribution(**changes)], evidence="url"
        )


def test_multiple_same_day_disclosures_must_be_reconciled_first():
    with pytest.raises(ValueError, match="same-day"):
        apply_disclosed_adjustments(
            _price_bundle(), [_distribution(), _distribution()], evidence="url"
        )


@pytest.mark.parametrize(
    "changed_field", [None, "foreAdjustFactor", "backAdjustFactor", "adjustFactor"]
)
def test_unchanged_cumulative_factor_metadata_is_not_a_second_action(changed_field):
    rows = [
        {
            "dividOperateDate": "2019-05-30",
            "foreAdjustFactor": "0.762186",
            "backAdjustFactor": "4.513163",
            "adjustFactor": "4.513163",
        },
        {
            "dividOperateDate": "2019-07-19",
            "foreAdjustFactor": "0.762186",
            "backAdjustFactor": "4.513163",
            "adjustFactor": "4.513163",
        },
    ]
    if changed_field:
        rows[1][changed_field] = "1.1"

    class Result:
        fields = tuple(rows[0])
        error_code = "0"

        def __init__(self):
            self.rows = iter(reversed(rows))

        def next(self):
            self.row = next(self.rows, None)
            return self.row is not None

        def get_row_data(self):
            return [self.row[key] for key in self.fields]

    class Module:
        def query_adjust_factor(self, **kwargs):
            return Result()

    actions = BaostockMarketHistoryProvider()._actions(
        Module(), "000333", "sz.000333", date(2019, 1, 1), date(2019, 12, 31)
    )
    assert len(actions) == (2 if changed_field else 1)
    if not changed_field:
        assert "unchanged_factor_record:2019-07-19" in actions[0].diagnostics
