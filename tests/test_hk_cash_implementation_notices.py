"""Real short HKEX implementation terms; no model, network or optimizer calls."""

import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.data.hk_corporate_actions import (
    HkexDividendEvidenceProvider,
    _per_share_amounts,
    match_hkex_cash_notice,
)
from src.data.market_history import CorporateAction, corporate_action_issues

FIXTURES = Path(__file__).parent / "fixtures" / "hkex" / "cash_implementation"
CASES = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))["cases"]


def _text(case):
    raw = (FIXTURES / case["text_file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == case["excerpt_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", case["pdf_sha256"])
    assert case["pdf_page_count"] <= 32
    assert case["excerpt_pages"]
    return raw.decode("utf-8")


def _match(case, *, text=None, published=None):
    return match_hkex_cash_notice(
        _text(case) if text is None else text,
        CorporateAction(**case["etnet_action"]),
        published_at=published or date.fromisoformat(case["published_at"]),
        source_url=case["source_url"],
        listed_code=case["listed_code"],
    )


@pytest.mark.parametrize(
    "case", CASES, ids=lambda item: item["code"] + "_" + item["ex_date"]
)
def test_real_short_implementation_has_causal_unique_gross_cash(case):
    result = _match(case)
    assert result is not None
    assert result.cash_per_share == pytest.approx(case["expected_hkd_per_share"])
    assert result.currency == "HKD"
    assert result.published_at == date.fromisoformat(case["published_at"])
    assert result.published_at <= result.ex_date
    assert result.payable_date == date.fromisoformat(
        case["etnet_action"]["payable_date"]
    )
    assert result.source_url == case["source_url"]
    assert "hkex_cash_evidence_matched" in result.diagnostics
    assert corporate_action_issues([result]) == ()


@pytest.mark.parametrize(
    "case", CASES, ids=lambda item: item["code"] + "_" + item["ex_date"]
)
def test_refinement_retains_failed_long_report_and_uses_later_short_evidence(
    case, tmp_path
):
    provider = HkexDividendEvidenceProvider(
        {"point_in_time_data": {"output_dir": str(tmp_path)}}
    )
    long_url = case["source_url"] + "?annual-report=1"
    provider._announcements = lambda *args: [
        {
            "published_at": date.fromisoformat(case["published_at"]),
            "url": long_url,
            "listed_code": case["code"],
        },
        {
            "published_at": date.fromisoformat(case["published_at"]),
            "url": case["source_url"],
            "listed_code": case["code"],
        },
    ]
    calls = []

    def read(url):
        calls.append(url)
        if url == long_url:
            raise ValueError("HKEX cash notice exceeds the 32-page evidence limit")
        return _text(case)

    provider._cached_notice = read
    source = CorporateAction(**case["etnet_action"])
    (result,) = provider.refine([source])
    assert result.cash_per_share == pytest.approx(case["expected_hkd_per_share"])
    assert corporate_action_issues([result]) == ()
    assert source.published_at is None
    assert calls == [long_url, case["source_url"]]
    assert any(long_url in item for item in result.diagnostics)


@pytest.mark.parametrize(
    "case", CASES, ids=lambda item: item["code"] + "_" + item["ex_date"]
)
def test_new_wording_cannot_bypass_future_filing_gate(case):
    assert (
        _match(case, published=date.fromisoformat(case["ex_date"]) + timedelta(days=1))
        is None
    )


def test_on_or_about_requires_matching_payment_day():
    case = CASES[0]
    text = _text(case).replace("3  August  2018", "4  August  2018")
    if text == _text(case):
        text = re.sub(r"3\s+August\s+2018", "4 August 2018", text)
    assert text != _text(case)
    assert _match(case, text=text) is None


def test_distributed_is_matched_without_discarding_conflicting_dates():
    case = next(
        item
        for item in CASES
        if item["code"] == "00956" and item["ex_date"] == "2019-06-13"
    )
    assert "distributed on" in re.sub(r"\s+", " ", _text(case))
    assert _match(case) is not None
    changed = re.sub(r"25\s+July\s+2019", "26 July 2019", _text(case))
    assert changed != _text(case)
    assert _match(case, text=changed) is None


@pytest.mark.parametrize(
    "words",
    [
        "RMB1.25 (tax inclusive) for every 10 shares",
        "RMB1.25 (inclusive of tax) per 10 ordinary shares",
        "RMB1.25 for each ten H shares",
    ],
)
def test_per_ten_gross_amount_retains_decimal_divisor(words):
    from decimal import Decimal

    assert _per_share_amounts(words, "RMB") == {Decimal("0.125")}


@pytest.mark.parametrize(
    "words",
    [
        "HK$1.426970 (tax inclusive) for every 10 H shares",
        "HK$1.426970 per ten H shares",
    ],
)
def test_hkd_per_ten_is_not_misread_as_per_one(words):
    from decimal import Decimal

    assert _per_share_amounts(words, "HKD") == {Decimal("0.142697")}


def test_changed_declared_rmb_or_duplicate_hkd_cannot_be_resolved_by_new_grammar():
    case = next(
        item
        for item in CASES
        if item["code"] == "00956" and item["ex_date"] == "2020-10-15"
    )
    text = _text(case)
    assert _match(case, text=text.replace("RMB1.25", "RMB1.26")) is None
    assert (
        _match(
            case,
            text=text
            + " Another cash dividend HK$1.50 (tax inclusive) for every 10 H shares.",
        )
        is None
    )


@pytest.mark.parametrize(
    "words",
    [
        "HK$1.25 (net of tax) for every 10 H shares",
        "HK$1.25 (tax inclusive) for every 100 H shares",
        "HK$1.25 (tax inclusive) for every 10 ADS",
    ],
)
def test_other_tax_scope_or_unsupported_units_are_not_guessed(words):
    assert _per_share_amounts(words, "HKD") == set()


def test_conflicting_explicit_issuer_still_wins_over_listing_identity():
    case = CASES[0]
    text = re.sub(
        r"Stock\s+Code\s*:\s*0?1088",
        "Stock Code: 1339",
        _text(case),
        flags=re.IGNORECASE,
    )
    assert text != _text(case)
    assert _match(case, text=text) is None
