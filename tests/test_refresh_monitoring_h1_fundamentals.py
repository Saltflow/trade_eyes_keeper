from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from src.instruments.models import FinancialStatementSnapshot
from src.instruments.point_in_time import (
    PointInTimeFundamentalStore,
    StatementFetchResult,
)

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "refresh_monitoring_h1_fundamentals.py"
)
SPEC = importlib.util.spec_from_file_location(
    "refresh_monitoring_h1_fundamentals", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _StaticInterimProvider:
    def __init__(self, results: dict[str, StatementFetchResult]):
        self.results = results
        self.calls: list[tuple[str, date, date]] = []

    def fetch(self, code: str, start: date, end: date) -> StatementFetchResult:
        self.calls.append((code, start, end))
        return self.results[code]


def _base_h1() -> FinancialStatementSnapshot:
    return FinancialStatementSnapshot(
        period_end=date(2026, 6, 30),
        published_at=date(2026, 8, 28),
        period_type="quarter",
        is_cumulative=True,
        currency="CNY",
        # This fixture explicitly supplies parent profit, unlike consolidated
        # Baostock netProfit, which must never be relabelled as parent income.
        source="synthetic_disclosed_interim_parent_profit",
        total_shares=1_000_000_000.0,
        net_income_parent=100_000_000.0,
        reported_roe=8.0,
    )


def _supplement() -> FinancialStatementSnapshot:
    return FinancialStatementSnapshot(
        period_end=date(2026, 6, 30),
        published_at=date(2026, 8, 28),
        period_type="quarter",
        is_cumulative=True,
        currency="CNY",
        accounting_standard="PRC-GAAP",
        source="cninfo_interim_report",
        source_url=("https://static.cninfo.com.cn/finalpage/2026-08-28/1225522363.PDF"),
        parent_equity=500_000_000.0,
        diagnostics=["parent_equity_from_official_interim_report"],
    )


def test_refresh_creates_fundamental_only_isolated_overlay(tmp_path):
    base_root = tmp_path / "base"
    base_store = PointInTimeFundamentalStore(base_root)
    base_path = base_store.upsert("000958", [_base_h1()])
    base_before = base_path.read_bytes()
    provider = _StaticInterimProvider(
        {
            "000958": StatementFetchResult(
                statements=[_supplement()],
                attempts=[{"source": "cninfo_interim_report", "status": "success"}],
            )
        }
    )
    output_dir = tmp_path / "isolated-output"

    report = MODULE.refresh_monitoring_h1_fundamentals(
        {},
        codes=["000958"],
        base_data_root=base_root,
        output_dir=output_dir,
        as_of=date(2026, 9, 18),
        provider=provider,
    )

    assert base_path.read_bytes() == base_before
    assert provider.calls == [("000958", date(2026, 1, 1), date(2026, 9, 18))]
    assert report["market_history_touched"] is False
    assert report["market_history_downloaded"] is False
    assert report["portfolio_optimization_run"] is False
    assert report["allocation_conclusion"] == "not_defined"
    assert not (output_dir / "market").exists()
    summary = json.loads((output_dir / "run_summary.json").read_text("utf-8"))
    assert summary["rows"][0]["base_statement_sha256"]
    assert summary["rows"][0]["verified_parent_equity"] == 500_000_000.0
    selected = PointInTimeFundamentalStore(output_dir).as_of(
        "000958", date(2026, 9, 18)
    )
    assert len(selected) == 1
    assert selected[0].parent_equity == 500_000_000.0
    assert selected[0].net_income_parent == 100_000_000.0
    assert selected[0].total_shares == 1_000_000_000.0
    assert "cninfo_interim_report" in selected[0].source.split("+")


def test_refresh_fails_before_output_when_any_h1_supplement_is_invalid(tmp_path):
    base_root = tmp_path / "base"
    PointInTimeFundamentalStore(base_root).upsert("000958", [_base_h1()])
    provider = _StaticInterimProvider(
        {
            "000958": StatementFetchResult(
                statements=[],
                attempts=[{"source": "cninfo_interim_report", "status": "empty"}],
            )
        }
    )
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="official H1 parent-equity supplement"):
        MODULE.refresh_monitoring_h1_fundamentals(
            {},
            codes=["000958"],
            base_data_root=base_root,
            output_dir=output_dir,
            as_of=date(2026, 9, 18),
            provider=provider,
        )

    assert not output_dir.exists()


def test_refresh_rejects_consolidated_profit_as_parent_before_output(tmp_path):
    base_root = tmp_path / "base"
    statement = _base_h1().copy(update={"source": "baostock_profit"})
    base_path = PointInTimeFundamentalStore(base_root).upsert("000958", [statement])
    before = base_path.read_bytes()
    provider = _StaticInterimProvider(
        {"000958": StatementFetchResult(statements=[_supplement()], attempts=[])}
    )
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="incomplete for overlay: net_income_parent"):
        MODULE.refresh_monitoring_h1_fundamentals(
            {},
            codes=["000958"],
            base_data_root=base_root,
            output_dir=output_dir,
            as_of=date(2026, 9, 18),
            provider=provider,
        )

    assert base_path.read_bytes() == before
    assert not output_dir.exists()
