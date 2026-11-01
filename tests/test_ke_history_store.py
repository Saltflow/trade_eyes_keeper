"""Synthetic integration fixtures for disclosed EPS and fund-reference joins."""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.data.historical_fund_ke import (
    FundKeHistory,
    FundKeObservation,
    unavailable_fund_history,
)
from src.data.historical_ke_filings import EpsFact
from src.data.ke_history_store import KeHistoryStore
from src.data.market_history import PriceHistoryBundle
from src.experiments.ke_local_trial import MemoryStatementStore, make_ke_context
from src.search.config import ExecutionConfig
from src.strategy.api import StrategyMarketData


def fixture_data(code):
    dates = ["2025-01-02", "2025-01-03", "2025-01-06"]
    prices = np.full((3, 1), 100.0)
    data = StrategyMarketData(
        indicator_matrix=np.ones((3, 1, 1)), dates=dates, symbols=[code], prices=prices
    )
    bundle = PriceHistoryBundle(
        code=code,
        currency="CNY",
        prices=pd.DataFrame(
            {
                "date": dates,
                "raw_close": prices[:, 0],
                "tradable": [True] * 3,
            }
        ),
    )
    return data, {code: bundle}


def test_disclosed_eps_needs_no_balance_sheet_and_latest_loss_blocks_old_profit(
    tmp_path,
):
    directory = tmp_path / "ke_history"
    directory.mkdir()
    rows = [
        EpsFact(
            "00001",
            date(2024, 1, 1),
            date(2024, 12, 31),
            "annual",
            date(2025, 1, 2),
            7.0,
            "CNY",
            "ordinary",
            "https://official.example/results",
            "a" * 64,
        ),
        EpsFact(
            "00001",
            date(2024, 1, 1),
            date(2024, 12, 31),
            "annual",
            date(2025, 1, 3),
            -1.0,
            "CNY",
            "ordinary",
            "https://official.example/revision",
            "b" * 64,
        ),
    ]
    (directory / "eps_facts.json").write_text(
        json.dumps({"facts": [r.to_dict() for r in rows]}), encoding="utf-8"
    )
    store = KeHistoryStore(tmp_path)
    data, bundles = fixture_data("00001")
    audit = {}
    panel = make_ke_context(
        MemoryStatementStore([]),
        bundles,
        ExecutionConfig(),
        set(),
        audit,
        supplements=store,
    )(data)
    assert panel.fundamental_availability_mask[:, 0, 0].tolist() == [False, True, False]
    assert panel.fundamental_features[1, 0, 0] == pytest.approx(0.09)
    assert audit["00001"]["known_nonpositive_days"] == 1
    assert audit["00001"]["source_documents"] == {
        "https://official.example/results": "a" * 64
    }


def test_reference_pe_null_is_barrier_and_reit_is_not_applicable(tmp_path):
    directory = tmp_path / "ke_history"
    directory.mkdir()
    history = FundKeHistory(
        "510300",
        "000300",
        (
            FundKeObservation(
                date(2025, 1, 1),
                date(2025, 1, 2),
                10.0,
                "https://www.csindex.com.cn/reference",
                "a" * 64,
                "2025-02-01T00:00:00+00:00",
            ),
            FundKeObservation(
                date(2025, 1, 2),
                date(2025, 1, 3),
                None,
                "https://www.csindex.com.cn/reference",
                "b" * 64,
                "2025-02-01T00:00:00+00:00",
            ),
        ),
    )
    (directory / "fund_histories.json").write_text(
        json.dumps(
            {
                "histories": {
                    "510300": history.as_dict(),
                    "508091": unavailable_fund_history("508091").as_dict(),
                },
            }
        ),
        encoding="utf-8",
    )
    store = KeHistoryStore(tmp_path)
    data, bundles = fixture_data("510300")
    audit = {}
    panel = make_ke_context(
        MemoryStatementStore([]),
        bundles,
        ExecutionConfig(),
        set(),
        audit,
        supplements=store,
    )(data)
    assert panel.fundamental_availability_mask[:, 0, 0].tolist() == [True, False, False]
    assert panel.fundamental_features[0, 0, 0] == pytest.approx(0.12)
    assert store.fund("508091", date(2025, 1, 3))["status"] == "not_applicable"


def test_fund_history_identity_mismatch_is_rejected(tmp_path):
    directory = tmp_path / "ke_history"
    directory.mkdir()
    (directory / "fund_histories.json").write_text(
        json.dumps(
            {
                "histories": {"VOO": unavailable_fund_history("508091").as_dict()},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="identity"):
        KeHistoryStore(tmp_path)


def test_daily_reference_reprices_known_eps_without_using_today_pe(tmp_path):
    directory = tmp_path / "ke_history"
    directory.mkdir()
    history = FundKeHistory(
        "510300",
        "000300",
        (
            FundKeObservation(
                date(2025, 1, 1),
                date(2025, 1, 2),
                10.0,
                "https://www.csindex.com.cn/reference",
                "a" * 64,
                "2025-02-01T00:00:00+00:00",
                reference_price=100.0,
            ),
            FundKeObservation(
                date(2025, 1, 2),
                date(2025, 1, 3),
                5.0,
                "https://www.csindex.com.cn/reference",
                "a" * 64,
                "2025-02-01T00:00:00+00:00",
                reference_price=120.0,
            ),
        ),
    )
    (directory / "fund_histories.json").write_text(
        json.dumps(
            {
                "histories": {"510300": history.as_dict()},
            }
        ),
        encoding="utf-8",
    )
    result = KeHistoryStore(tmp_path).fund("510300", date(2025, 1, 2))
    assert result["ke"] == pytest.approx(0.02 + 1 / 12)
    assert result["source_date"] == "2025-01-01"
    assert result["valuation_mode"] == "price_updated_reference"
