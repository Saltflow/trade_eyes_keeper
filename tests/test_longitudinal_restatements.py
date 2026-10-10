"""Version-aware retrospective selection based on explicit original disclosures."""

from copy import deepcopy

from src.fundamental_quant.longitudinal_data import (
    _restatement_disclosure,
    build_comparison_panel,
    historical_month_end_valuations,
)


def source(selected="是 □否", reason="同一控制下企业合并"):
    return {
        "metadata": {"period_end": "2025-12-31", "published_at": "2026-04-18"},
        "pages": {
            "7": "公司是否需追溯调整或重述以前年度会计数据\n"
            + selected
            + "\n追溯调整或重述原因\n"
            + reason
            + "\n2025 年 2024 年\n调整前 调整后\n营业收入 68,567,696,803.55 71,348,284,153.46 71,353,764,354.48"
        },
    }


def observation(value, published, column="current", basis=None):
    return {
        "issuer_id": "a_share:000498",
        "company_name": "山东路桥",
        "metric": "revenue",
        "period": "2024-12-31",
        "original_column_period": "2024-12-31",
        "column": column,
        "kind": "FY",
        "value": value,
        "unit": "元",
        "currency": None,
        "scope": "consolidated",
        "published_at": published,
        "refs": ["old:p1" if column == "current" else "new:p157"],
        "restatement_basis": basis,
    }


def spec(as_of="2026-10-10"):
    return {
        "as_of": as_of,
        "history_years": 7,
        "companies": [{"issuer_id": "a_share:000498", "name": "山东路桥"}],
    }


def test_checked_restatement_is_not_policy_boilerplate_or_checked_no():
    basis = _restatement_disclosure(source(), "new")
    assert basis["reason"] == "同一控制下企业合并"
    assert basis["refs"] == ["new:p7"]
    assert basis["quote"] in source()["pages"]["7"]
    assert _restatement_disclosure(source("□是 否"), "new") is None
    assert (
        _restatement_disclosure(
            {"pages": {"1": "同一控制下企业合并的会计政策：编制比较报表应调整"}}, "new"
        )
        is None
    )


def test_restatement_declaration_across_page_header_keeps_both_source_pages():
    data = source()
    data["pages"] = {
        "7": "公司是否需追溯调整或重述以前年度会计数据\n是 □否",
        "8": "山东高速路桥集团股份有限公司 2025年年度报告\n8\n追溯调整或重述原因\n同一控制下企业合并\n2024 年 调整前 调整后",
    }
    basis = _restatement_disclosure(data, "new")
    assert basis["refs"] == ["new:p7", "new:p8"]


def test_documented_restatement_selects_adjusted_value_but_preserves_both_facts():
    basis = _restatement_disclosure(source(), "new")
    rows = [
        observation(71348284153.46, "2025-04-15"),
        observation(71353764354.48, "2026-04-18", "comparative", basis),
    ]
    panel = build_comparison_panel(rows, spec())
    assert panel["rows"][0]["values"]["revenue"] == 71353764354.48
    assert len(panel["facts"]) == 2
    assert sorted(f["value"] for f in panel["facts"].values()) == [
        71348284153.46,
        71353764354.48,
    ]
    history = panel["rows"][0]["version_history"]["revenue"]
    assert len(history["selected_refs"]) == len(history["superseded_refs"]) == 1
    assert panel["issues"][0]["code"] == "disclosed_restatement_applied"
    assert panel["issues"][0]["status"] == "resolved"
    assert "scope" in panel["issues"][0]["limitation"]
    older = build_comparison_panel(rows, spec("2025-12-31"))
    assert older["rows"][0]["values"]["revenue"] == 71348284153.46
    assert len(older["facts"]) == 1


def test_newer_value_without_restatement_and_same_date_disagreement_remain_conflicts():
    original = observation(100, "2025-04-15")
    later = observation(101, "2026-04-18", "comparative")
    panel = build_comparison_panel([original, later], spec())
    assert panel["rows"][0]["values"]["revenue"] is None
    assert panel["issues"][0]["code"] == "conflicting_financial_versions"
    later["restatement_basis"] = _restatement_disclosure(source(), "new")
    another = deepcopy(later)
    another["value"] = 102
    panel = build_comparison_panel([original, later, another], spec())
    assert panel["rows"][0]["values"]["revenue"] is None
    original["currency"] = "CNY"
    later["currency"] = "USD"
    assert (
        build_comparison_panel([original, later], spec())["rows"][0]["values"][
            "revenue"
        ]
        is None
    )


def test_interim_restatement_selects_adjusted_same_half_year_not_full_year():
    data = source()
    data["metadata"] = {"period_end": "2026-06-30", "published_at": "2026-08-26"}
    basis = _restatement_disclosure(data, "interim")
    original = observation(-1723188225.83, "2025-08-30")
    adjusted = observation(-1720922052.20, "2026-08-26", "comparative", basis)
    for item in [original, adjusted]:
        item.update(
            period="2025-06-30",
            original_column_period="2025-06-30",
            kind="H1",
            metric="cfo",
        )
    panel = build_comparison_panel([original, adjusted], spec())
    row = panel["rows"][0]
    assert row["period"] == "2025-06-30"
    assert row["kind"] == "H1"
    assert row["values"]["cfo"] == -1720922052.20
    assert panel["metrics"]["a_share:000498"]["growth"] == {}


def test_pit_valuation_keeps_original_until_restatement_disclosure():
    prices = [
        {
            "issuer_id": "a_share:000498",
            "date": date,
            "close": 10,
            "shares_outstanding": 100,
            "currency": "CNY",
            "adjustment": "none",
            "refs": ["price"],
        }
        for date in ["2025-12-31", "2026-05-29"]
    ]
    statements = [
        {
            "issuer_id": "a_share:000498",
            "period_end": "2024-12-31",
            "published_at": published,
            "currency": "CNY",
            "parent_equity": value,
            "refs": [ref],
        }
        for published, value, ref in [
            ("2025-04-15", 200, "old"),
            ("2026-04-18", 250, "restated"),
        ]
    ]
    rows = historical_month_end_valuations(prices, statements)["rows"]
    assert rows[0]["pb"] == 5
    assert rows[1]["pb"] == 4
    assert "old" in rows[0]["refs"]
    assert "restated" not in rows[0]["refs"]


def test_shares_are_not_monetary_currency_and_common_bvps_is_separate():
    equity = observation(1000, "2025-04-15")
    equity.update(metric="parent_equity", currency="CNY")
    shares = observation(100, "2025-04-15")
    shares.update(metric="shares_outstanding", currency=None, unit="股")
    other = observation(200, "2025-04-15")
    other.update(metric="other_equity", currency="CNY")
    panel = build_comparison_panel([equity, shares, other], spec())
    row = panel["rows"][0]
    assert row["currency"] == "CNY"
    assert row["calculated"]["bvps"] == 10
    assert row["calculated"]["common_equity_bvps"] == 8
    lacking_other = build_comparison_panel([equity, shares], spec())["rows"][0]
    assert lacking_other["calculated"]["bvps"] == 10
    assert lacking_other["calculated"]["common_equity_bvps"] is None


def test_reviewed_image_restatement_column_can_resolve_with_explicit_basis():
    basis = _restatement_disclosure(source(), "new")
    basis["method"] = "reviewed_image_restatement_header/v1"
    basis["quote"] = "2024年（经重述）"
    basis["refs"] = ["EIMG:verified-hash"]
    old = observation(100, "2025-04-15")
    later = observation(101, "2026-04-18", "comparative", basis)
    assert (
        build_comparison_panel([old, later], spec())["rows"][0]["values"]["revenue"]
        == 101
    )
    later["restatement_basis"] = None
    later["restated"] = True
    assert (
        build_comparison_panel([old, later], spec())["rows"][0]["values"]["revenue"]
        is None
    )


def test_cash_paid_normalization_keeps_original_reported_sign(monkeypatch):
    from src.fundamental_quant import longitudinal_data

    monkeypatch.setattr(
        longitudinal_data, "extract_field", lambda *args: {"status": "not_located"}
    )
    meta = {
        "issuer_id": "a_share:601800",
        "company_name": "中国交建",
        "period_end": "2023-12-31",
        "published_at": "2024-03-28",
    }
    quote = "购建长期资产支付现金 (10) (8)"
    field = {
        "status": "obtained",
        "source_ref": {"pages": [1]},
        "quote": quote,
        "amount": 10,
        "comparative_amount": 8,
        "reported_amount": -10,
        "comparative_reported_amount": -8,
        "period_end": "2023-12-31",
        "comparative_period_end": "2022-12-31",
        "currency": "CNY",
        "sign_convention": "positive_cash_paid_magnitude",
        "cash_payment_sign_basis": {
            "current": {
                "confirmed": True,
                "evidence": [{"page": 2, "quote": "经营现金流出小计 (30) (20)"}],
            },
            "comparative": {
                "confirmed": True,
                "evidence": [{"page": 2, "quote": "经营现金流出小计 (30) (20)"}],
            },
        },
    }
    pack = {"financial_fields": {"capex": field}, "sections": [], "metadata": meta}
    observations = list(
        longitudinal_data._observations(
            pack,
            {
                "metadata": meta,
                "pages": {"1": quote, "2": "经营现金流出小计 (30) (20)"},
            },
            "D",
        )
    )
    assert [item["value"] for item in observations] == [10, 8]
    assert all(item["cash_payment_sign_basis"]["confirmed"] for item in observations)
    assert all("D:p2" in item["refs"] for item in observations)
    assert [item["raw_reported_amount"] for item in observations] == ["-10", "-8"]
    assert all(
        item["sign_convention"] == "positive_cash_paid_magnitude"
        for item in observations
    )


def test_presentation_declaration_is_not_functional_currency_or_transaction():
    from src.fundamental_quant.longitudinal_data import (
        _presentation_currency_disclosure,
    )

    declared = "本集团编制本财务报表时所采用的货币为人民\n币。"
    data = {"pages": {"175": declared}}
    result = _presentation_currency_disclosure(data, "D")
    assert result["currency"] == "CNY"
    assert result["quote"] == declared
    assert result["refs"] == ["D:p175"]
    for text in [
        "本集团以人民币为记账本位币。",
        "本公司以人民币为记账本位币。",
        "购买设备支付人民币100万元。",
        "子公司财务报表以人民币列报。",
    ]:
        assert _presentation_currency_disclosure({"pages": {"1": text}}, "D") is None
    data["pages"]["176"] = "本财务报表以美元列报。"
    assert _presentation_currency_disclosure(data, "D") is None


def test_currency_inheritance_is_document_local_preserves_explicit_fields(monkeypatch):
    from src.fundamental_quant import longitudinal_data

    monkeypatch.setattr(
        longitudinal_data, "extract_field", lambda *args: {"status": "not_located"}
    )
    metadata = {
        "issuer_id": "a_share:000498",
        "company_name": "山东路桥",
        "period_end": "2025-12-31",
        "published_at": "2026-04-18",
    }
    quote = "经营活动产生的现金流量净额 100.00 80.00"
    field = {
        "status": "obtained",
        "source_ref": {"pages": [100]},
        "quote": quote,
        "amount": 100,
        "period_end": "2025-12-31",
        "currency": None,
    }
    pack = {"metadata": metadata, "financial_fields": {"cfo": field}, "sections": []}
    data = {
        "metadata": metadata,
        "pages": {"100": quote, "175": "本集团编制本财务报表时所采用的货币为人民币。"},
    }
    item = next(longitudinal_data._observations(pack, data, "D"))
    assert item["currency"] == "CNY" and item["original_currency"] is None
    assert item["currency_basis"]["refs"] == ["D:p175"]
    assert item["refs"] == ["D:p100", "D:p175"]
    field["currency"] = "USD"
    explicit = next(longitudinal_data._observations(pack, data, "D"))
    assert explicit["currency"] == "USD" and explicit["currency_basis"] is None
    field["currency"] = None
    del data["pages"]["175"]
    assert next(longitudinal_data._observations(pack, data, "D"))["currency"] is None


def test_identical_amount_can_use_currency_evidenced_version_without_restatement():
    old = observation(100, "2025-04-15")
    later = observation(100, "2026-04-18", "comparative")
    later["currency"] = "CNY"
    panel = build_comparison_panel([old, later], spec())
    assert len(panel["facts"]) == 2
    assert panel["rows"][0]["values"]["revenue"] == 100
    assert panel["rows"][0]["currency"] == "CNY"
    assert (
        panel["rows"][0]["version_history"]["revenue"]["policy"]
        == "identical_amount_with_explicit_currency_evidence"
    )
    assert (
        build_comparison_panel([old, later], spec("2025-12-31"))["rows"][0]["currency"]
        is None
    )
    old["currency"] = "USD"
    assert (
        build_comparison_panel([old, later], spec())["rows"][0]["values"]["revenue"]
        is None
    )


def test_restatement_can_use_new_explicit_currency_but_not_contradict_known_currency():
    basis = _restatement_disclosure(source(), "new")
    old = observation(100, "2025-04-15")
    new = observation(101, "2026-04-18", "comparative", basis)
    new["currency"] = "CNY"
    row = build_comparison_panel([old, new], spec())["rows"][0]
    assert row["values"]["revenue"] == 101 and row["currency"] == "CNY"
    old["currency"] = "USD"
    assert (
        build_comparison_panel([old, new], spec())["rows"][0]["values"]["revenue"]
        is None
    )


def sse_source():
    return {
        "metadata": {"period_end": "2021-12-31", "published_at": "2022-03-30"},
        "pages": {
            "8": "主要会计数据 2021年 2020年 调整后 调整前 营业收入 850.00 645.00 610.00 经营活动产生的现金流量净额 -10.00 -0.76 32.77",
            "10": "季度数据与已披露定期报告数据差异说明 主要系报告期发生了同一控制下企业合并航焱、臻景、铁建公司。",
            "207": "对于同一控制下企业合并取得的子公司，其经营成果和现金流量自合并当期期初纳入合并财务报表。编制比较合并财务报表时，对上年财务报表的相关项目进行调整，视同合并后形成的报告主体自最终控制方开始控制时点起一直存在。",
        },
    }


def test_sse_adjusted_table_requires_actual_merger_and_comparative_policy():
    data = sse_source()
    basis = _restatement_disclosure(data, "D")
    assert basis["method"] == "adjusted_comparatives_with_current_merger_evidence/v1"
    assert basis["refs"] == ["D:p8", "D:p10", "D:p207"]
    assert all(
        proof["quote"] in data["pages"][proof["page"]] for proof in basis["evidence"]
    )
    for missing in ["8", "10", "207"]:
        incomplete = deepcopy(data)
        del incomplete["pages"][missing]
        assert _restatement_disclosure(incomplete, "D") is None
    data["pages"]["10"] = "报告期未发生同一控制下企业合并。"
    assert _restatement_disclosure(data, "D") is None
    old = observation(610, "2021-06-09")
    new = observation(645, "2022-03-30", "comparative", basis)
    for item in [old, new]:
        item["period"] = "2020-12-31"
    panel = build_comparison_panel([old, new], spec())
    assert panel["rows"][0]["values"]["revenue"] == 645
    assert len(panel["facts"]) == 2


def test_cumulative_conversion_splits_currency_and_nonconsecutive_years():
    observations = []
    for year, currency, cfo, profit in [
        (2019, None, 5, 10),
        (2020, None, 7, 10),
        (2021, "CNY", 8, 10),
        (2022, "CNY", 9, 10),
        (2024, "CNY", 6, 10),
    ]:
        for metric, amount in [("cfo", cfo), ("net_profit_total", profit)]:
            item = observation(amount, f"{year + 1}-04-01")
            item.update(period=f"{year}-12-31", currency=currency, metric=metric)
            observations.append(item)
    metrics = build_comparison_panel(observations, spec())["metrics"]["a_share:000498"]
    overall = metrics["cumulative_cash_conversion"]
    assert overall["value"] is None and overall["cfo_sum"] is None
    blocks = metrics["cumulative_cash_conversion_segments"]
    assert [b["periods"] for b in blocks] == [
        ["2019-12-31", "2020-12-31"],
        ["2021-12-31", "2022-12-31"],
        ["2024-12-31"],
    ]
    assert [b["value"] for b in blocks] == [0.6, 0.85, 0.6]
    assert blocks[0]["currency_status"] == "presentation_currency_unconfirmed"
    assert blocks[1]["currency"] == "CNY"
    # Different currencies between numerator/denominator are not a usable period.
    observations[-1]["currency"] = "USD"
    bad = build_comparison_panel(observations, spec())["metrics"]["a_share:000498"]
    assert len(bad["cumulative_cash_conversion_segments"]) == 2
