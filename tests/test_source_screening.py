"""Screening is a cheap prerequisite, not a seven-year PDF recovery mechanism."""

import json
import threading
from types import SimpleNamespace

import pytest

from src.data import source_screening as screen


def passed(**changes):
    return dict(
        code="000001",
        name="bank",
        market_cap_cny_100m=100,
        pe_ttm=10,
        adjusted_pe_ttm=11,
        debt_assets=0.85,
        earnings_yield_proxy=0.1,
        leverage_exempt=True,
        **changes,
    )


def test_financial_exemption_changes_only_leverage():
    row = passed()
    assert screen.evaluate(row)["status"] == "passed"
    row["leverage_exempt"] = False
    assert screen.evaluate(row)["status"] == "rejected"
    row["leverage_exempt"], row["adjusted_pe_ttm"] = True, 25
    assert screen.evaluate(row)["status"] == "rejected"


@pytest.mark.parametrize(
    "field,value",
    [
        ("market_cap_cny_100m", 10),
        ("pe_ttm", 0),
        ("adjusted_pe_ttm", 25),
        ("earnings_yield_proxy", 0.07),
    ],
)
def test_strict_screen_boundaries(field, value):
    row = passed()
    row[field] = value
    assert screen.evaluate(row)["status"] == "rejected"


def test_missing_manual_and_nan_stay_pending():
    row = passed()
    for patch in [
        {"earnings_yield_proxy": None},
        {"pe_ttm": float("nan")},
        {"manual_review_required": True},
    ]:
        assert screen.evaluate(dict(row, **patch))["status"] == "pending"


def test_fixed_normalization_periods_and_no_unadjusted_roe_substitute():
    records = [
        {"REPORT_DATE": f"{y}-12-31", "ROEKCJQ": 10, "KCFJCXSYJLR": 100}
        for y in range(2021, 2026)
    ]
    records += [
        {
            "REPORT_DATE": "2026-06-30",
            "BPS": 5,
            "TOTAL_SHARE": 100,
            "ZCFZL": 80,
            "PARENTNETPROFIT": 70,
            "KCFJCXSYJLR": 50,
        },
        {"REPORT_DATE": "2025-06-30", "PARENTNETPROFIT": 60, "KCFJCXSYJLR": 40},
    ]
    records[4]["PARENTNETPROFIT"] = 120
    quote = {"price": 10, "date": "20261023150000", "total_shares": 100}
    pb = screen.derive_fields(records, quote, "PB")
    assert pb["pe_ttm"] == pytest.approx(1000 / 130)
    assert pb["adjusted_pe_ttm"] == pytest.approx(1000 / 110)
    assert pb["earnings_yield_proxy"] == pytest.approx(0.05)
    assert screen.derive_fields(records, quote, "PE")["earnings_yield_proxy"] == 0.1
    assert screen.derive_fields(records, quote, None)["earnings_yield_proxy"] is None
    for row in records:
        row["ROEJQ"], row["ROEKCJQ"] = 20, None
    assert screen.derive_fields(records, quote, "PB")["earnings_yield_proxy"] is None


def test_pending_fetches_once_and_preserves_original_rows(tmp_path, monkeypatch):
    snapshot = {
        "rows": [
            {"code": "1", "status": "passed"},
            {"code": "2", "status": "rejected"},
            {"code": "3", "status": "pending"},
        ]
    }
    calls = []

    def fetch(row, *args):
        calls.append(row["code"])
        return dict(row, field_error="unavailable")

    monkeypatch.setattr(screen, "fetch_fields", fetch)
    for _ in range(2):
        result = screen.complete_pending(
            snapshot, tmp_path / "fields", threading.Event(), lambda *a: None
        )
    assert calls == ["3"]
    assert result["rows"][:2] == snapshot["rows"][:2]
    assert result["rows"][2]["status"] == "pending"
    assert json.loads((tmp_path / "fields/3/result.json").read_text("utf8"))[
        "field_error"
    ]


def test_raw_saved_before_parse_and_reorganization_does_not_auto_pass(
    tmp_path, monkeypatch
):
    records = [
        {"SECURITY_CODE": "002379", "REPORT_DATE": f"{y}-12-31", "ROEKCJQ": 25}
        for y in range(2021, 2026)
    ]
    records.append(
        {
            "SECURITY_CODE": "002379",
            "REPORT_DATE": "2026-06-30",
            "BPS": 10,
            "ZCFZL": 40,
            "PARENTNETPROFIT": 10,
            "KCFJCXSYJLR": 10,
        }
    )
    for day in ["2025-06-30", "2025-12-31"]:
        records.append(
            {
                "SECURITY_CODE": "002379",
                "REPORT_DATE": day,
                "PARENTNETPROFIT": 10,
                "KCFJCXSYJLR": 10,
            }
        )
    parts = [""] * 88
    parts[2], parts[3], parts[30], parts[73] = (
        "002379",
        "2",
        "20261023150000",
        "1000000000",
    )
    body = ('v_sz002379="' + "~".join(parts) + '";').encode("gbk")
    response = iter(
        [
            SimpleNamespace(
                text=json.dumps({"result": {"data": records}}),
                raise_for_status=lambda: None,
                json=lambda: {"result": {"data": records}},
            ),
            SimpleNamespace(content=body, raise_for_status=lambda: None),
        ]
    )
    monkeypatch.setattr(screen.requests, "get", lambda *a, **k: next(response))
    row = {
        "code": "002379",
        "status": "pending",
        "path": "PB",
        "manual_review_required": True,
    }
    result = screen.fetch_fields(row, tmp_path, lambda: None)
    assert result["status"] == "pending"
    assert result["checks"]["5"] == "待核"
    assert result["market_cap_cny_100m"] == 20
    assert result["share_date"] == "20261023150000"
    assert (tmp_path / "financial.response.txt").exists()
    assert (tmp_path / "quote.response.bin").read_bytes() == body
