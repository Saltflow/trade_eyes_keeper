"""Real cache/PDF reconciliation: consolidated earnings are not parent earnings."""

import hashlib
import json
from pathlib import Path

import pytest

from src.instruments.point_in_time import (
    BaostockStatementProvider,
    PointInTimeFundamentalStore,
)

CASES = json.loads(
    (Path(__file__).parent / "fixtures/baostock_profit_scope.json").read_text(
        encoding="utf8"
    )
)


@pytest.mark.parametrize(
    "case", CASES, ids=lambda c: c["code"] + c["cache_record"]["period_end"]
)
def test_real_legacy_cache_consolidated_scope(case, tmp_path):
    row = case["cache_record"]
    total = next(f for f in case["pdf_facts"] if f["metric"] == "net_profit_total")
    parent = next(f for f in case["pdf_facts"] if f["metric"] == "net_profit_parent")
    assert row["net_income_parent"] == total["value"]
    assert total["value"] != parent["value"]
    path = tmp_path / "fundamentals" / (case["code"] + ".statements.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"statements": [row]}), encoding="utf8")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    value = PointInTimeFundamentalStore(tmp_path).read_all(case["code"])[0]
    assert value.net_income_parent is None
    assert value.net_income_total == total["value"]
    assert value.average_parent_equity is None
    assert (
        value.source_scope_correction["raw_fields"]["net_income_parent"]
        == total["value"]
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_new_provider_preserves_total_and_does_not_invent_parent():
    row = CASES[0]["cache_record"]
    result = BaostockStatementProvider._snapshot(
        {
            "statDate": row["period_end"],
            "pubDate": row["published_at"],
            "netProfit": str(row["net_income_parent"]),
            "roeAvg": "0.1",
        },
        4,
    )
    assert result.net_income_total == row["net_income_parent"]
    assert result.net_income_parent is None
    assert result.average_parent_equity is None


def test_unknown_or_composite_source_is_not_silently_relabelled():
    from src.instruments.statement_scope import normalize_statement_scope

    for source in ("unknown_vendor", "baostock_profit+cninfo_annual_report"):
        record = {
            "source": source,
            "net_income_parent": 100,
            "average_parent_equity": 200,
        }
        assert normalize_statement_scope(record) == record


def test_already_split_conflicting_totals_remain_visible():
    from src.instruments.statement_scope import normalize_statement_scope

    record = {
        "source": "baostock_profit",
        "net_income_parent": 100,
        "net_income_total": 120,
    }
    result = normalize_statement_scope(record)
    assert result["source_scope_correction"]["status"] == "conflict"
    assert result["source_scope_correction"]["raw_fields"]["net_income_parent"] == 100
    assert result["net_income_total"] == 120
    assert result["net_income_parent"] is None
    assert record["net_income_parent"] == 100


def test_normalization_idempotent_and_preserves_new_total():
    from src.instruments.statement_scope import normalize_statement_scope

    record = CASES[0]["cache_record"]
    fixed = normalize_statement_scope(record)
    assert normalize_statement_scope(fixed) == fixed
    assert (
        normalize_statement_scope(
            {
                "source": "baostock_profit",
                "net_income_total": 100,
                "net_income_parent": None,
            }
        )["net_income_total"]
        == 100
    )
