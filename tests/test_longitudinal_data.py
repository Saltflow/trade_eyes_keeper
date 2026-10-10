"""Calculation controls, data contracts and frozen-source offline replay."""

from pathlib import Path

import pytest

from src.fundamental_quant.longitudinal_data import (
    build_comparison_panel,
    historical_month_end_valuations,
    prepare_comparison,
)

SPEC = {
    "group_id": "test",
    "companies": [{"issuer_id": "X", "name": "Test"}],
    "as_of": "2026-10-10",
    "history_years": 7,
}


def obs(metric, value, year, *, currency="CNY", published=None):
    return {
        "issuer_id": "X",
        "company_name": "Test",
        "metric": metric,
        "period": f"{year}-12-31",
        "kind": "FY",
        "value": value,
        "currency": currency,
        "unit": "元",
        "scope": "consolidated",
        "published_at": published or f"{year + 1}-03-31",
        "refs": [f"d{year}:p1"],
    }


def test_actual_intervals_rolling_and_cumulative_use_matched_consolidated_profit():
    observations = []
    for y, revenue, profit, cash in [
        (2021, 100, 10, 20),
        (2022, 110, 30, 15),
        (2023, 121, 30, 15),
        (2024, 133.1, 30, 20),
    ]:
        observations += [
            obs("revenue", revenue, y),
            obs("cfo", cash, y),
            obs("net_profit_total", profit, y),
            obs("net_profit_parent", profit / 2, y),
        ]
    panel = build_comparison_panel(observations, SPEC)
    m = panel["metrics"]["X"]
    assert m["growth"]["revenue"]["intervals"] == 3
    assert m["growth"]["revenue"]["value"] == pytest.approx(0.1)
    assert (
        m["cumulative_cash_conversion"]["value"] == 0.7
    )  # 70/100, not mean of annual ratios or parent NP
    assert m["rolling_three_year"][0]["intervals"] == 3


def test_missing_year_negative_endpoint_no_fabrication_and_no_h1_annualization():
    data = [
        obs("revenue", 100, 2021),
        obs("revenue", 146.41, 2025),
        obs("cfo", -10, 2021),
        obs("cfo", 10, 2025),
    ]
    half = obs("revenue", 999, 2026, published="2026-08-01")
    half.update(period="2026-06-30", kind="H1")
    p = build_comparison_panel(data + [half], SPEC)
    assert p["metrics"]["X"]["growth"]["revenue"]["intervals"] == 4
    assert p["metrics"]["X"]["growth"]["revenue"]["missing_years"] == [2022, 2023, 2024]
    assert p["metrics"]["X"]["growth"]["cfo"]["value"] is None
    assert p["rows"][-1]["calculated"]["simple_roe"] is None


def test_conflicts_stay_null_unknown_currency_and_bvps_not_share_capital():
    data = [
        obs("cfo", 100, 2025),
        obs("cfo", 101, 2025),
        obs("capex", 10, 2025),
        obs("parent_equity", 80, 2025),
        obs("share_capital", 10, 2025),
    ]
    p = build_comparison_panel(data, SPEC)
    assert p["rows"][0]["values"]["cfo"] is None
    assert p["rows"][0]["calculated"]["fcf_proxy"] is None
    assert p["rows"][0]["calculated"]["bvps"] is None
    assert p["issues"][0]["code"] == "conflicting_financial_versions"
    p = build_comparison_panel(
        [obs("cfo", 100, 2025), obs("capex", 10, 2025, currency="USD")], SPEC
    )
    assert p["rows"][0]["calculated"]["fcf_proxy"] is None
    p = build_comparison_panel(
        [obs("parent_equity", 80, 2025), obs("shares_outstanding", 20, 2025)], SPEC
    )
    assert p["rows"][0]["calculated"]["bvps"] == 4


def test_future_disclosures_are_excluded_from_research_snapshot():
    p = build_comparison_panel([obs("revenue", 1, 2025, published="2026-12-01")], SPEC)
    assert p["rows"] == []
    assert p["coverage"][0]["actual_annual_years"] == []


def test_pit_strict_date_original_version_unadjusted_and_historical_shares():
    prices = [
        {
            "issuer_id": "X",
            "date": day,
            "close": 10,
            "adjustment": "none",
            "shares_outstanding": shares,
            "currency": "CNY",
            "refs": ["price-source"],
        }
        for day, shares in [("2024-04-30", 10), ("2024-05-31", 10), ("2024-06-28", 20)]
    ]
    statements = [
        {
            "issuer_id": "X",
            "period_end": "2023-12-31",
            "published_at": "2024-04-30",
            "currency": "CNY",
            "parent_equity": 50,
            "net_profit_ttm": 10,
            "refs": ["original-report"],
        },
        {
            "issuer_id": "X",
            "period_end": "2023-12-31",
            "published_at": "2024-06-01",
            "currency": "CNY",
            "parent_equity": 40,
            "net_profit_ttm": 5,
            "refs": ["restatement"],
        },
    ]
    p = historical_month_end_valuations(prices, statements)
    assert (
        p["rows"][0]["pb"] is None
    )  # date-only announcement isn't tradable the same day
    assert p["rows"][1]["pe"] == 10
    assert p["rows"][1]["refs"] == ["price-source", "original-report"]
    assert (
        p["rows"][2]["pe"] == 40
    )  # actual historical shares, later disclosed restatement
    assert p["rows"][2]["pb"] == 5
    prices[1]["adjustment"] = "qfq"
    p = historical_month_end_valuations(prices, statements)
    assert len(p["rows"]) == 2
    assert p["issues"][0]["code"] == "unverified_or_adjusted_price"


def test_real_frozen_pack_replay_offline():
    root = Path("data/fundamental_quant/fr_packs_20261008")
    if not (root / "latest_run.json").exists():
        pytest.skip("local frozen financial reports unavailable")
    spec = dict(SPEC, companies=[{"issuer_id": "a_share:000568", "name": "泸州老窖"}])
    packet = prepare_comparison(spec, [root])
    assert packet["external_requests"] == packet["model_calls"] == 0
    assert len(packet["documents"]) == 4
    assert len(packet["reading_catalog"]["documents"]) == 4
    assert all(d["full_mda_chapters"] for d in packet["reading_catalog"]["documents"])
    assert packet["metrics"]["a_share:000568"]["cumulative_cash_conversion"][
        "value"
    ] == pytest.approx(0.9408839133778616)
    assert any(c["kind"] == "mda" and len(c["text"]) > 1000 for c in packet["chapters"])
    assert all(not c["truncated"] for c in packet["chapters"])
    assert all(f["refs"] and f["quote"] for f in packet["facts"].values())
    assert (
        packet["historical_valuations"]["issues"][0]["code"]
        == "historical_price_inputs_missing"
    )
    # The same frozen source must produce a stable cache key.
    assert prepare_comparison(spec, [root])["packet_hash"] == packet["packet_hash"]


def test_cached_adapter_ttm_and_missing_daily_shares(tmp_path):
    import json

    from src.fundamental_quant.longitudinal_data import load_cached_valuation_events

    (tmp_path / "fundamentals").mkdir()
    (tmp_path / "market").mkdir()
    rows = []
    for period, published, profit in [
        ("2023-06-30", "2023-08-01", 40),
        ("2023-12-31", "2024-03-01", 100),
        ("2024-06-30", "2024-08-01", 60),
    ]:
        rows.append(
            {
                "period_end": period,
                "published_at": published,
                "net_income_parent": profit,
                "parent_equity": 200,
                "currency": "CNY",
                "source": "unit_test_fixture",
                "is_cumulative": True,
                "period_type": "year" if period.endswith("12-31") else "quarter",
            }
        )
    (tmp_path / "fundamentals" / "X.statements.json").write_text(
        json.dumps({"statements": rows})
    )
    (tmp_path / "market" / "X.meta.json").write_text(
        json.dumps({"contract": "raw-and-qfq-1", "currency": "CNY"})
    )
    (tmp_path / "market" / "X.csv").write_text(
        "date,raw_close,qfq_close,tradable\n2024-08-30,10,8,True\n"
    )
    data = load_cached_valuation_events(SPEC, tmp_path)
    assert data["statement_events"][-1]["net_profit_ttm"] == 120
    assert all(
        ref in data["external_evidence"]
        for row in data["price_events"] + data["statement_events"]
        for ref in row["refs"]
    )
    assert all(
        e["file_sha256"] and e["record"] for e in data["external_evidence"].values()
    )
    assert data["price_events"][0]["close"] == 10
    assert data["price_events"][0]["shares_outstanding"] is None
    assert (
        data["issues"][-1]["code"]
        == "daily_historical_share_count_missing_no_projection_from_latest_report"
    )


def test_cumulative_conversion_keeps_loss_and_zero_profit_years():
    data = [
        obs("cfo", 15, 2023),
        obs("net_profit_total", 20, 2023),
        obs("cfo", -10, 2024),
        obs("net_profit_total", -5, 2024),
        obs("cfo", 1, 2025),
        obs("net_profit_total", 0, 2025),
    ]
    panel = build_comparison_panel(data, SPEC)
    value = panel["metrics"]["X"]["cumulative_cash_conversion"]
    assert value["value"] == pytest.approx(6 / 15)
    assert len(value["periods"]) == 3


def test_frozen_supplements_reuse_true_history_shares_and_wrapped_costs():
    root = Path("data/fundamental_quant/fr_packs_20261008")
    supplement = Path(
        "data/fundamental_quant/longitudinal_comparison_20261010/review_source_facts.json"
    )
    if not supplement.exists() or not (root / "latest_run.json").exists():
        pytest.skip("reviewed real source facts unavailable")
    spec = dict(
        SPEC,
        companies=[
            {"issuer_id": i, "name": i}
            for i in ["a_share:000568", "a_share:000858", "a_share:600519"]
        ],
    )
    packet = prepare_comparison(spec, [root], supplemental_facts_paths=[supplement])
    assert len(packet["rows"]) == 21
    assert len(packet["cost_anchors"]) == 9
    assert all(
        issue["code"] == "closing_share_count_not_located" for issue in packet["issues"]
    )
    lz = next(
        r
        for r in packet["rows"]
        if r["issuer_id"] == "a_share:000568" and r["period"] == "2025-12-31"
    )
    assert lz["calculated"]["bvps"] == pytest.approx(33.81946767403899)
    assert any(f.get("source_numeric_reconstruction") for f in packet["facts"].values())
    old = [f for f in packet["facts"].values() if f["period"] == "2021-12-31"]
    assert old and all(f["published_at"] >= "2024-01-01" for f in old)


def test_tampered_supplement_cannot_fill_missing_shares(tmp_path):
    import json

    root = Path("data/fundamental_quant/fr_packs_20261008")
    original = Path(
        "data/fundamental_quant/longitudinal_comparison_20261010/review_source_facts.json"
    )
    if not original.exists():
        pytest.skip("reviewed real source facts unavailable")
    item = json.loads(original.read_text(encoding="utf8"))["shares_2025"][0]
    item["value"] = item["raw_value"] = "123456789012345"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps({"shares_2025": [item]}), encoding="utf8")
    spec = dict(SPEC, companies=[{"issuer_id": item["issuer_id"], "name": "test"}])
    packet = prepare_comparison(spec, [root], supplemental_facts_paths=[path])
    assert any(i["code"] == "supplement_rejected" for i in packet["issues"])
    assert all(r["calculated"]["bvps"] is None for r in packet["rows"])
