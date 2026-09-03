import json

import pandas as pd
import pytest

from src.data.data_source import DataSource
from src.data.option_data import (
    SINA_CFFEX_CHAIN_URL,
    SINA_CFFEX_DAILY_URL,
    SINA_CFFEX_PAGE_URL,
    SINA_ETF_DAILY_URL,
    SINA_ETF_METADATA_URL,
    SinaOptionDataSource,
    SinaOptionError,
    resample_option_bars,
)


class FakeResponse:
    def __init__(self, text="", payload=None, encoding="utf-8", status_code=200):
        self.content = text.encode(encoding)
        self.text = text
        self.payload = payload
        self.status_code = status_code

    def json(self):
        if self.payload is None:
            raise ValueError("not json")
        return self.payload


class FakeSession:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return self.callback(url, params)


def _quote_text(option_code, option_type, strike, underlying="510300"):
    fields = [""] * 51
    fields[0] = "3"
    fields[1] = "0.0366"
    fields[2] = "0.0367"
    fields[3] = "0.0368"
    fields[4] = "7"
    fields[5] = "87467"
    fields[6] = "-39.34"
    fields[7] = "{:.4f}".format(strike)
    fields[8] = "0.0605"
    fields[9] = "0.0500"
    fields[32] = "2026-09-02 15:00:00"
    fields[36] = underlying
    fields[37] = "300ETF购9月4700" if option_type == "C" else "300ETF沽9月4700"
    fields[39] = "0.0532"
    fields[40] = "0.0326"
    fields[41] = "85448"
    fields[42] = "32099116.00"
    fields[45] = option_type
    fields[46] = "2026-09-23"
    return 'var hq_str_{}="{}";'.format(option_code, ",".join(fields))


def test_cffex_months_and_chain_parse_json():
    chain = {
        "result": {
            "status": {"code": 0},
            "data": {
                "up": [[10, 12.1, 12.2, 12.3, 11, 1000, 1.2, 4650, "io2609C4650"]],
                "down": [[20, 141.0, 141.2, 141.4, 21, 2000, -2.3, "io2609P4650"]],
            },
        }
    }

    def callback(url, params):
        if url == "{}/io/cffex".format(SINA_CFFEX_PAGE_URL):
            return FakeResponse(
                '<li data-value="io2609"></li><li data-value="io2610"></li>'
                '<li data-value="io2609"></li><li data-value="mo2609"></li>'
            )
        if url == SINA_CFFEX_CHAIN_URL:
            return FakeResponse(payload=chain)
        raise AssertionError((url, params))

    session = FakeSession(callback)
    source = SinaOptionDataSource(http=session)

    assert source.fetch_cffex_option_months("io") == ["2609", "2610"]
    frame = source.fetch_cffex_option_chain("io2609")

    assert list(frame["option_type"]) == ["C", "P"]
    assert list(frame["strike"]) == [4650.0, 4650.0]
    assert frame.loc[frame["option_type"] == "P", "last"].iloc[0] == 141.2
    assert (
        frame.loc[frame["option_type"] == "C", "option_code"].iloc[0]
        == "io2609c4650"
    )
    assert session.calls[-1][1]["pinzhong"] == "io2609"


def test_cffex_daily_jsonp_is_typed_and_sorted():
    payload = [
        {"d": "2026-09-02", "o": "12", "h": "13", "l": "11", "c": "12.5", "v": "20"},
        {"d": "2026-09-01", "o": "10", "h": "12", "l": "9", "c": "11", "v": "10"},
    ]

    def callback(url, params):
        assert url == SINA_CFFEX_DAILY_URL
        assert params == {"symbol": "io2609c4650"}
        return FakeResponse("jsonp({});".format(json.dumps(payload)))

    frame = SinaOptionDataSource(http=FakeSession(callback)).fetch_cffex_option_daily(
        "IO2609C4650"
    )

    assert list(frame["date"]) == list(pd.to_datetime(["2026-09-01", "2026-09-02"]))
    assert frame["close"].tolist() == [11.0, 12.5]
    assert frame["strike"].tolist() == [4650.0, 4650.0]


def test_etf_months_codes_and_chain_parse_gbk_quotes():
    metadata = {
        "result": {
            "status": {"code": 0},
            "data": {"contractMonth": ["2026-09", "2026-09", "2026-10"]},
        }
    }
    up = 'var hq_str_OP_UP_5103002609="CON_OP_1001,CON_OP_1002";'
    down = 'var hq_str_OP_DOWN_5103002609="CON_OP_2001";'
    quotes = "\n".join(
        [
            _quote_text("CON_OP_1001", "C", 4.7),
            _quote_text("CON_OP_1002", "C", 4.8),
            _quote_text("CON_OP_2001", "P", 4.7),
        ]
    )

    def callback(url, params):
        if url == SINA_ETF_METADATA_URL:
            assert params == {"exchange": "null", "cate": "300ETF"}
            return FakeResponse(payload=metadata)
        if url == "https://hq.sinajs.cn/list=OP_UP_5103002609":
            return FakeResponse(up, encoding="gbk")
        if url == "https://hq.sinajs.cn/list=OP_DOWN_5103002609":
            return FakeResponse(down, encoding="gbk")
        if url.startswith("https://hq.sinajs.cn/list=CON_OP_"):
            return FakeResponse(quotes, encoding="gbk")
        raise AssertionError((url, params))

    source = SinaOptionDataSource(http=FakeSession(callback))
    assert source.fetch_etf_option_months("510300") == ["2609", "2610"]
    assert source.fetch_etf_option_codes("510300", "2026-09", "call") == [
        "CON_OP_1001",
        "CON_OP_1002",
    ]
    frame = source.fetch_etf_option_chain("510300", "2609")

    assert len(frame) == 3
    assert set(frame["option_type"]) == {"C", "P"}
    assert set(frame["side"]) == {"call", "put"}
    assert frame["strike"].notna().all()
    assert frame["expiry_date"].unique().tolist() == ["2026-09-23"]
    assert frame["underlying"].unique().tolist() == ["510300"]


def test_etf_daily_jsonp_and_weekly_monthly_resampling():
    payload = [
        {"d": "2026-09-01", "o": "1", "h": "3", "l": "1", "c": "2", "v": "10"},
        {"d": "2026-09-02", "o": "2", "h": "4", "l": "2", "c": "3", "v": "20"},
        {"d": "2026-09-03", "o": "3", "h": "5", "l": "2", "c": "4", "v": "30"},
    ]

    def callback(url, params):
        assert url == SINA_ETF_DAILY_URL
        assert params == {"symbol": "1001"}
        return FakeResponse("callback({});".format(json.dumps(payload)))

    source = SinaOptionDataSource(http=FakeSession(callback))
    daily = source.fetch_etf_option_daily("1001")
    daily["option_code"] = "CON_OP_1001"
    weekly = resample_option_bars(daily, "weekly")
    monthly = resample_option_bars(daily, "monthly")

    assert len(daily) == 3
    assert weekly[["open", "high", "low", "close", "volume"]].iloc[0].tolist() == [
        1.0,
        5.0,
        1.0,
        4.0,
        60,
    ]
    assert monthly["volume"].tolist() == [60]


def test_datasource_exposes_lazy_option_source(tmp_path):
    data_source = DataSource({"storage": {"cache_dir": str(tmp_path)}})
    assert data_source.option_data_source is data_source.option_data_source
    assert isinstance(data_source.option_data_source, SinaOptionDataSource)


def test_invalid_option_inputs_fail_closed():
    source = SinaOptionDataSource(http=FakeSession(lambda url, params: FakeResponse()))

    with pytest.raises(SinaOptionError):
        source.fetch_cffex_option_chain("not-a-product", "2609")
    with pytest.raises(SinaOptionError):
        source.fetch_etf_option_codes("510300", "2609", "straddle")
    with pytest.raises(SinaOptionError):
        source.fetch_etf_option_daily("not-a-code")
