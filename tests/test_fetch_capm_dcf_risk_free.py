"""Pure parsing and point-in-time selection tests for official risk-free curves."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest


def _module():
    path = (
        Path(__file__).resolve().parents[1] / "scripts" / "fetch_capm_dcf_risk_free.py"
    )
    spec = importlib.util.spec_from_file_location("capm_dcf_risk_free", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hkma_uses_last_available_two_year_efn_yield_without_future_fill():
    module = _module()
    rate, audit = module._fetch_hkma_efbn_two_year(
        [
            {"end_of_day": "2025-03-28", "efn_2y": "3.21"},
            {"end_of_day": "2025-04-02", "efn_2y": "3.50"},
        ],
        date(2025, 3, 31),
    )

    assert rate == 0.0321
    assert audit["source_date"] == "2025-03-28"
    assert audit["maturity"] == "2Y"


def test_hkma_accepts_the_latest_official_month_end_observation():
    module = _module()

    rate, audit = module._fetch_hkma_efbn_two_year(
        [{"end_of_day": "2025-03-31", "efn_2y": "3.21"}],
        date(2025, 4, 29),
    )

    assert rate == 0.0321
    assert audit["source_date"] == "2025-03-31"


def test_hkma_paginates_past_the_documented_page_size_to_reach_old_anchors():
    module = _module()

    class Response:
        def __init__(self, records):
            self._records = records

        @staticmethod
        def raise_for_status():
            return None

        def json(self):
            return {"result": {"datasize": 100, "records": self._records}}

    class Http:
        def __init__(self):
            self.offsets = []

        def get(self, _url, *, params, **_kwargs):
            self.offsets.append(params["offset"])
            rows = {
                0: [{"end_of_day": "2025-05-30", "efn_2y": "3.0"}],
                1: [{"end_of_day": "2025-03-28", "efn_2y": "3.1"}],
            }.get(params["offset"], [])
            return Response(rows)

    http = Http()
    records = module._hkma_records(http, timeout=5, not_before=date(2025, 3, 31))

    assert len(records) == 2
    assert http.offsets == [0, 1]


def test_hkma_rejects_a_stale_curve_instead_of_using_a_future_quote():
    module = _module()

    try:
        module._fetch_hkma_efbn_two_year(
            [{"end_of_day": "2025-03-01", "efn_2y": "3.21"}],
            date(2025, 5, 1),
        )
    except RuntimeError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale HKMA rate must fail closed")


def test_us_treasury_reads_the_last_available_ten_year_yield():
    module = _module()

    class Response:
        text = "Date,1 Mo,10 yr\n03/27/2025,4.30,4.36\n03/28/2025,4.31,4.40\n"

        @staticmethod
        def raise_for_status():
            return None

    class Http:
        @staticmethod
        def get(*_args, **_kwargs):
            return Response()

    rate, audit = module._fetch_us_treasury_ten_year(
        {}, Http(), date(2025, 3, 31), timeout=5
    )

    assert rate == pytest.approx(0.044)
    assert audit["source_date"] == "2025-03-28"
    assert audit["maturity"] == "10Y"
