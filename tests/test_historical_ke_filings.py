import subprocess
from datetime import date

import pytest

from src.data.historical_ke_filings import (
    EpsFact,
    dollar_eps_cells,
    earnings_as_of,
    eps_numbers,
    extract_pdf_pages,
    facts_from_eps_row,
)


def fact(start, end, kind, published, eps, **kwargs):
    return EpsFact(
        "EXAMPLE",
        start,
        end,
        kind,
        published,
        eps,
        "CNY",
        "ordinary",
        "https://official.example/filing.pdf",
        "a" * 64,
        **kwargs,
    )


def test_annual_plus_ytd_bridge_and_publication_cutoff():
    annual = fact("2023-01-01", "2023-12-31", "annual", "2024-03-01", 2.0)
    prior = fact("2023-01-01", "2023-06-30", "ytd", "2023-08-01", 0.7)
    current = fact("2024-01-01", "2024-06-30", "ytd", "2024-08-01", 1.1)
    assert earnings_as_of([annual, prior, current], "2024-08-01")["ttm_eps"] == 2
    resolved = earnings_as_of([annual, prior, current], "2024-08-02")
    assert resolved["ttm_eps"] == pytest.approx(2.4)
    assert [c["weight"] for c in resolved["components"]] == [1, 1, -1]
    assert resolved["currency"] == "CNY"


def test_missing_bridge_and_latest_loss_never_use_older_profit():
    old = fact("2023-01-01", "2023-12-31", "annual", "2024-03-01", 2)
    recent = fact("2024-01-01", "2024-06-30", "ytd", "2024-08-01", -1)
    assert earnings_as_of([old, recent], "2024-08-02")["ttm_eps"] is None
    loss = fact("2024-01-01", "2024-12-31", "annual", "2025-03-01", -3)
    assert earnings_as_of([old, loss], "2025-03-02")["ttm_eps"] == -3


def test_four_contiguous_quarters_and_missing_quarter():
    rows = [
        fact(a, b, "quarter", p, value)
        for a, b, p, value in [
            ("2023-07-01", "2023-09-30", "2023-11-01", 1),
            ("2023-10-01", "2023-12-31", "2024-03-01", 2),
            ("2024-01-01", "2024-03-31", "2024-05-01", 3),
            ("2024-04-01", "2024-06-30", "2024-08-01", 4),
        ]
    ]
    assert earnings_as_of(rows, "2024-08-02")["ttm_eps"] == 10
    assert earnings_as_of(rows[1:], "2024-08-02")["ttm_eps"] is None


def test_class_currency_and_restated_comparison_do_not_leak():
    original = fact("2023-01-01", "2023-12-31", "annual", "2024-03-01", 2)
    revised = fact("2023-01-01", "2023-12-31", "annual", "2024-09-01", 3)
    assert earnings_as_of([original, revised], "2024-08-01")["ttm_eps"] == 2
    assert earnings_as_of([original, revised], "2024-09-02")["ttm_eps"] == 3
    wrong = EpsFact(**{**original.to_dict(), "share_class": "Class A"})
    assert earnings_as_of([original, wrong], "2024-09-02")["ttm_eps"] is None
    assert (
        earnings_as_of([wrong], "2024-09-02", share_class="Class B")["ttm_eps"] is None
    )


def test_explicit_ytd_column_and_cents_not_quarter_or_millions():
    rows = facts_from_eps_row(
        "Basic 51.12 49.44 112.30 93.20",
        [
            {
                "period_start": "2024-01-01",
                "period_end": "2024-06-30",
                "period_kind": "ytd",
                "value_index": 2,
            }
        ],
        symbol="EXAMPLE",
        published_at=date(2024, 8, 1),
        currency="CNY",
        share_class="ordinary",
        source_url="https://official.example/results.pdf",
        document_sha256="a" * 64,
        unit="cents_per_share",
    )
    assert rows[0].eps == 1.123
    assert eps_numbers("B share (5.88) 12.18") == [-5.88, 12.18]
    with pytest.raises(ValueError, match="unit"):
        facts_from_eps_row(
            "1.20",
            [],
            symbol="X",
            published_at=date(2024, 8, 1),
            currency="CNY",
            share_class="ordinary",
            source_url="x",
            document_sha256="a" * 64,
            unit="millions",
        )


def test_pdf_worker_deadline_and_page_budget(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 0.01
        raise subprocess.TimeoutExpired(args[0], 0.01)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(TimeoutError, match="hard deadline"):
        extract_pdf_pages(b"%PDF", pages=[1], timeout_seconds=0.01)
    with pytest.raises(ValueError, match="page selection"):
        extract_pdf_pages(b"%PDF", range(1, 26))


def test_fact_json_roundtrip_and_invalid_provenance():
    value = fact("2023-01-01", "2023-12-31", "annual", "2024-03-01", 2)
    assert EpsFact.from_dict(value.to_dict()) == value
    with pytest.raises(ValueError, match="SHA256"):
        EpsFact(**{**value.to_dict(), "document_sha256": "not-a-hash"})
    with pytest.raises(ValueError, match="next-day"):
        EpsFact(**{**value.to_dict(), "available_from": "2024-03-01"})


def test_currency_cells_keep_class_b_and_dotted_leaders_separate():
    assert dollar_eps_cells("Class B ... $ ...1..2...18 $ (5.88) $ 32.14 $ 26.95") == [
        12.18,
        -5.88,
        32.14,
        26.95,
    ]
    with pytest.raises(ValueError, match="decimal evidence"):
        dollar_eps_cells("Class B $ 123")


def test_same_date_conflicts_and_currency_changes_fail_closed():
    annual = fact("2023-01-01", "2023-12-31", "annual", "2024-03-01", 2)
    conflict = EpsFact(**{**annual.to_dict(), "eps": 3})
    assert earnings_as_of([annual, conflict], "2024-08-02")["ttm_eps"] is None
    prior = fact("2023-01-01", "2023-06-30", "ytd", "2023-08-01", 0.7)
    current = fact("2024-01-01", "2024-06-30", "ytd", "2024-08-01", 1.1)
    changed_currency = EpsFact(**{**current.to_dict(), "currency": "HKD"})
    assert (
        earnings_as_of([annual, prior, changed_currency], "2024-08-02")["ttm_eps"]
        is None
    )


def test_real_tencent_ytd_excerpt_uses_explicit_ytd_columns():
    # Official 2024-11-13 announcement p2, not p1's Q3 basic EPS 5.762.
    rows = facts_from_eps_row(
        "– basic 15.346 9.312 65%",
        [
            {
                "period_start": "2024-01-01",
                "period_end": "2024-09-30",
                "period_kind": "ytd",
                "value_index": 0,
            }
        ],
        symbol="00700",
        published_at=date(2024, 11, 13),
        currency="CNY",
        share_class="ordinary",
        source_url="https://www1.hkexnews.hk/listedco/listconews/sehk/2024/1113/2024111300327.pdf",
        document_sha256="feb53a86353377b146a7a671e2e762368e399ae2ec0d8949f4f3709b438e6003",
        selected_pdf_pages=(2,),
    )
    assert rows[0].eps == 15.346
    assert rows[0].available_from == date(2024, 11, 14)
