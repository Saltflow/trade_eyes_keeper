import copy
import json
from datetime import date

import pytest

from src.data.corporate_action_currency import HkmaCashValuation, validate_cash_currency
from src.data.hk_corporate_actions import match_hkex_cash_notice
from src.data.market_history import CorporateAction


class Response:
    def __init__(self, payload):
        self.text = json.dumps(payload)

    def raise_for_status(self):
        pass


class HTTP:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return Response(
            {
                "header": {"success": True},
                "result": {"datasize": len(self.rows), "records": self.rows},
            }
        )


def action(**kwargs):
    return CorporateAction(
        code="09999",
        action_type="cash_dividend",
        ex_date=date(2025, 3, 5),
        published_at=date(2025, 2, 20),
        cash_per_share=0.24405,
        currency="USD",
        source="hkex_dividend_notice",
        **kwargs,
    )


def test_foreign_cash_requires_conversion():
    with pytest.raises(ValueError, match="currency mismatch"):
        validate_cash_currency(action())


def test_causal_hkma_conversion_preserves_original_and_reuses(tmp_path):
    http = HTTP([{"end_of_day": "2025-03-04", "usd": 7.778}])
    p = HkmaCashValuation({"point_in_time_data": {"output_dir": str(tmp_path)}}, http)
    result = p.convert(action())
    assert result.cash_per_share == pytest.approx(0.24405 * 7.778)
    assert (
        result.declared_cash_per_share == 0.24405 and result.declared_currency == "USD"
    )
    assert result.cash_conversion["rate_date"] == "2025-03-04"
    assert "not_actual_HKD_receipt" in result.cash_conversion["basis"]
    assert p.convert(action()).dict() == result.dict() and http.calls == 1
    bad = copy.deepcopy(result)
    bad.cash_per_share += 0.01
    with pytest.raises(ValueError, match="valuation evidence"):
        validate_cash_currency(bad)
    bad = copy.deepcopy(result)
    bad.cash_conversion["rate_date"] = "2025-03-05"
    with pytest.raises(ValueError, match="valuation evidence"):
        validate_cash_currency(bad)


@pytest.mark.parametrize(
    "rows",
    [
        [{"end_of_day": "2026-01-01", "usd": 7.8}],
        [],
        [{"end_of_day": "2025-03-04", "usd": None}],
    ],
)
def test_missing_future_invalid_fx_fails(tmp_path, rows):
    with pytest.raises((ValueError, TypeError)):
        HkmaCashValuation(
            {"point_in_time_data": {"output_dir": str(tmp_path)}}, HTTP(rows)
        ).convert(action())


def test_unknown_currency_not_guessed(tmp_path):
    a = action()
    a.currency = "EUR"
    with pytest.raises(ValueError, match="unsupported"):
        HkmaCashValuation(
            {"point_in_time_data": {"output_dir": str(tmp_path)}}
        ).convert(a)


def test_native_a_us_no_conversion():
    for code, currency in [("600000", "CNY"), ("AAPL", "USD")]:
        a = action()
        a.code = code
        a.currency = currency
        validate_cash_currency(a)
        assert a.cash_per_share == 0.24405


def test_usd_form_ordinary_share_not_ads():
    a = action()
    a.published_at = None
    text = "Stock code 09999 Cash Dividend Announcement for Equity Issuer Dividend declared USD 0.24405 pershare Ex-dividend date 05 March 2025"
    result = match_hkex_cash_notice(
        text,
        a,
        published_at=date(2025, 2, 20),
        source_url="https://www.hkexnews.hk/form.pdf",
    )
    assert result.currency == "USD" and result.cash_per_share == 0.24405
    assert (
        match_hkex_cash_notice(
            text.replace("pershare", "per ADS"),
            a,
            published_at=date(2025, 2, 20),
            source_url="https://www.hkexnews.hk/form.pdf",
        )
        is None
    )


def test_execution_refuses_foreign_currency():
    from src.backtest.execution import build_corporate_action_schedule

    with pytest.raises(ValueError, match="currency mismatch"):
        build_corporate_action_schedule([action()], ["2025-03-05"], ["09999"])


def test_provider_refusal_persists_cooldown_without_repeated_requests(tmp_path):
    class BlockedHTTP:
        calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            response = Response({"error": "blocked"})
            response.status_code = 405
            return response

    http = BlockedHTTP()
    cfg = {"point_in_time_data": {"output_dir": str(tmp_path)}}
    with pytest.raises(ValueError, match="provider blocked"):
        HkmaCashValuation(cfg, http).convert(action())
    with pytest.raises(ValueError, match="provider blocked"):
        HkmaCashValuation(cfg, http).convert(action())
    assert http.calls == 1


def test_usd_form_future_announcement_still_rejected():
    a = action()
    text = "Stock code 09999 Cash Dividend Announcement for Equity Issuer Announcement date 06 March 2025 Dividend declared USD 0.24405 pershare Ex-dividend date 05 March 2025"
    with pytest.raises(ValueError, match="predates"):
        match_hkex_cash_notice(
            text,
            a,
            published_at=date(2025, 2, 20),
            source_url="https://www.hkexnews.hk/form.pdf",
        )


@pytest.mark.parametrize(
    "filename,ex_day,pub_day,amount,payment",
    [
        ("2025022000534.txt", "2025-03-05", "2025-02-20", 0.24405, "2025-03-17"),
        ("2020111900807.txt", "2020-12-03", "2020-11-19", 0.039, "2020-12-15"),
        ("prospectus_page_38.txt", "2020-06-11", "2020-06-02", 0.0464, "2020-06-23"),
    ],
)
def test_official_usd_disclosures_replay(filename, ex_day, pub_day, amount, payment):
    from pathlib import Path

    source = Path(__file__).parent / "fixtures/hk_dividends" / filename
    a = action()
    a.ex_date = date.fromisoformat(ex_day)
    a.cash_per_share = amount
    a.payable_date = date.fromisoformat(payment)
    a.published_at = None
    result = match_hkex_cash_notice(
        source.read_text(encoding="utf-8"),
        a,
        published_at=date.fromisoformat(pub_day),
        source_url=next(
            row["url"]
            for row in json.loads(
                (source.parent / "sources.json").read_text(encoding="utf-8")
            )["sources"]
            if row["fixture"] == filename
        ),
        listed_code="09999",
    )
    assert (
        result is not None
        and result.cash_per_share == amount
        and result.currency == "USD"
    )
    assert result.published_at == date.fromisoformat(pub_day)
