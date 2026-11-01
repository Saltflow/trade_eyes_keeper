"""Synthetic contracts; real downloaded evidence remains under .tmp only."""

import hashlib
import json
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

import pytest

from src.data.historical_fund_ke import (
    CSI_FUND_REFERENCES,
    CSI_HISTORY_URL,
    FundKeHistory,
    FundKeObservation,
    fetch_csi_index_history,
    parse_csi_index_history,
    parse_hsi_monthly_catalog,
    parse_hsi_roundup,
    unavailable_fund_history,
)

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
URL = CSI_HISTORY_URL + "?indexCode=000300&startDate=20241001&endDate=20261030"


def _row(day="20241031", pe=10.0, *, index="000300", close=100.0):
    return {"tradeDate": day, "indexCode": index, "peg": pe, "close": close}


def _parse(rows, **kwargs):
    return parse_csi_index_history(
        json.dumps({"code": "200", "data": rows}).encode(),
        "510300",
        source_url=kwargs.pop("source_url", URL),
        observed_at=kwargs.pop("observed_at", NOW),
        start="2024-10-01",
        end="2026-10-30",
        **kwargs,
    )


def test_confirmed_fund_reference_map_matches_existing_daily_report():
    assert {
        code: CSI_FUND_REFERENCES[code]
        for code in ("510300", "515180", "520650", "588510")
    } == {
        "510300": "000300",
        "515180": "000922",
        "520650": "931637",
        "588510": "932456",
    }


def test_historical_pe_is_dated_and_cannot_backfill_earlier_or_same_day():
    history = _parse([_row()])
    assert history.at("2024-10-30").ke is None
    assert history.at("2024-10-31").ke is None
    value = history.at("2024-11-01")
    assert value.ke == pytest.approx(0.12)
    assert value.pe_ttm == 10.0
    assert value.source_date == date(2024, 10, 31)
    assert value.available_from == date(2024, 11, 1)
    assert value.valuation_mode == "lagged_reference"
    assert value.valuation_basis == "official_index_reference"
    assert not history.qualified_for_promotion
    assert "no_archived_revision" in history.vintage_basis
    assert value.observed_at == NOW.isoformat()
    assert len(value.source_sha256) == 64
    json.dumps(history.as_dict())
    json.dumps(value.as_dict())


@pytest.mark.parametrize("pe", [None, 0, -1, float("inf"), float("nan"), "--", True])
def test_latest_unknown_or_nonpositive_value_blocks_old_positive_pe(pe):
    history = _parse([_row(), _row("20241101", pe)])
    assert history.at("2024-11-01").ke == pytest.approx(0.12)
    assert history.at("2024-11-02").ke is None
    assert history.at("2024-11-02").reason == "no_positive_historical_pe"


def test_future_observations_do_not_change_prior_history():
    original = _parse([_row()])
    revised = _parse([_row(), _row("20241102", 100.0)])
    assert original.at("2024-11-01").ke == revised.at("2024-11-01").ke
    assert revised.at("2024-11-03").ke == pytest.approx(0.03)


def test_stale_reference_cannot_forward_fill_forever():
    history = _parse([_row()])
    assert history.at("2024-11-07").ke is not None
    assert history.at("2024-11-08").reason == "stale_reference"
    with pytest.raises(ValueError, match="max_age"):
        history.at("2024-11-01", max_age_days=-1)


def test_same_day_index_price_can_revalue_known_earnings_once():
    history = _parse([_row()])
    value = history.at(
        "2024-11-01", reference_price=50.0, reference_price_as_of="2024-11-01"
    )
    assert value.pe_ttm == 5.0
    assert value.ke == pytest.approx(0.22)
    assert value.valuation_mode == "price_updated_reference"
    with pytest.raises(ValueError, match="dated same-day"):
        history.at(
            "2024-11-01", reference_price=50.0, reference_price_as_of="2024-11-02"
        )


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("https:", "http:"),
        URL.replace("www.csindex.com.cn", "evil.invalid"),
        URL.replace("indexCode=000300", "indexCode=000922"),
        URL.replace("/perf/index-perf", "/current-snapshot"),
    ],
)
def test_source_identity_and_official_endpoint_are_required(url):
    with pytest.raises(ValueError):
        _parse([_row()], source_url=url)


@pytest.mark.parametrize(
    "rows",
    [
        [_row(index="000922")],
        [_row(day="not-a-date")],
        [_row(), _row(pe=20.0)],
        [{"tradeDate": "20241031", "indexCode": "000300", "currentPE": 10}],
    ],
)
def test_schema_identity_dates_and_conflicts_fail_closed(rows):
    with pytest.raises(ValueError):
        _parse(rows)


def test_duplicate_equal_rows_are_idempotent_and_interval_is_not_extended():
    history = _parse([_row(), _row(), _row("20270101")])
    assert len(history.observations) == 1
    with pytest.raises(ValueError, match="timezone-aware"):
        _parse([_row()], observed_at=NOW.replace(tzinfo=None))


def test_fetch_archives_exact_original_bytes_without_current_holdings(tmp_path):
    body = json.dumps({"code": "200", "data": [_row()]}).encode()
    response = Mock(content=body, url=URL)
    get = Mock(return_value=response)
    history = fetch_csi_index_history(
        "510300",
        "2024-10-01",
        "2026-10-30",
        http_get=get,
        archive_dir=tmp_path,
        observed_at=NOW,
    )
    response.raise_for_status.assert_called_once()
    assert get.call_count == 1
    assert "indexCode=000300" in get.call_args.args[0]
    digest = hashlib.sha256(body).hexdigest()
    assert (tmp_path / f"{digest}.raw").read_bytes() == body
    assert history.at("2024-11-01").source_sha256 == digest


def test_reit_and_unverified_voo_remain_explicitly_missing():
    reit = unavailable_fund_history("508091").at("2026-10-30")
    assert reit.status == "not_applicable" and reit.ke is None
    assert "no_dividend_or_ffo" in reit.reason
    get = Mock(side_effect=AssertionError("must not fetch a current snapshot"))
    voo = fetch_csi_index_history("VOO", "2024-11-01", "2026-10-30", http_get=get)
    assert voo.at("2026-10-30").status == "unavailable"
    get.assert_not_called()


def _catalog(
    *, publish="2025-03-03T04:00:00.000Z", title="Monthly Roundup (February 2025)"
):
    return json.dumps(
        {
            "items": [
                {
                    "elements": {
                        "title": {"value": title},
                        "publishDate": {"value": publish},
                        "url": {
                            "value": "/content/dam/wpb/hsil/en_hk/monthly_roundup/content/20250303T000000.pdf"
                        },
                    }
                }
            ]
        }
    ).encode()


def _monthly_text(pe="5.98"):
    return (
        "Feb 2025\nPE Ratio (Times)\nHang Seng SCHK High Dividend Low Volatility Index 2.26% 5.99% 18.33% -0.06% "
        + pe
        + " 6.45%"
    )


def test_monthly_import_uses_real_catalog_publication_not_month_end():
    report = parse_hsi_monthly_catalog(_catalog())[0]
    assert report.period_end == date(2025, 2, 28)
    with patch("src.data.historical_fund_ke._pdf_text", return_value=_monthly_text()):
        row = parse_hsi_roundup(b"%PDF-synthetic-unit-only", report, observed_at=NOW)
    history = FundKeHistory("520550", "HSHYLV", (row,))
    assert history.at("2025-03-03", max_age_days=45).ke is None
    value = history.at("2025-03-04", max_age_days=45)
    assert value.pe_ttm == 5.98  # Never use adjacent 6.45% dividend yield.
    assert value.ke == pytest.approx(0.02 + 1 / 5.98)
    assert value.catalog_sha256 == hashlib.sha256(_catalog()).hexdigest()
    assert value.valuation_mode == "lagged_reference"
    no_anchor = history.at(
        "2025-03-04",
        max_age_days=45,
        reference_price=100.0,
        reference_price_as_of="2025-03-04",
    )
    assert no_anchor.reason == "missing_reference_price_anchor"
    assert history.at("2025-04-15", max_age_days=45).reason == "stale_reference"


@pytest.mark.parametrize(
    "text",
    [
        _monthly_text() * 2,
        _monthly_text().replace("Feb 2025", "Jan 2025"),
        "Feb 2025\nOther index 5.98 6.45%",
    ],
)
def test_ambiguous_or_mismatched_pdf_is_not_imported(text):
    report = parse_hsi_monthly_catalog(_catalog())[0]
    target = "src.data.historical_fund_ke._pdf_text"
    with patch(target, return_value=text), pytest.raises(ValueError):
        parse_hsi_roundup(b"%PDF-synthetic-unit-only", report, observed_at=NOW)


def test_publication_clock_and_delay_cannot_be_guessed():
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_hsi_monthly_catalog(_catalog(publish="2025-03-03T12:00:00"))
    with pytest.raises(ValueError, match="publication"):
        parse_hsi_monthly_catalog(_catalog(publish="2025-02-28T12:00:00Z"))
    with pytest.raises(ValueError, match="causal delay"):
        FundKeHistory(
            "510300",
            "000300",
            (
                FundKeObservation(
                    date(2025, 1, 1),
                    date(2025, 1, 1),
                    10.0,
                    URL,
                    "a" * 64,
                    NOW.isoformat(),
                ),
            ),
        )


def _multpl(rows):
    return (
        "<html><h1>S&amp;P 500 PE Ratio by Month</h1><table id='datatable'><tr><th>Date</th><th>Value</th></tr>"
        + "".join(
            "<tr><td>" + day + "</td><td>" + pe + "</td></tr>" for day, pe in rows
        )
        + "</table></html>"
    ).encode()


def test_multpl_month_start_is_not_available_until_completed_month_end():
    from src.data.historical_fund_ke import parse_multpl_reference_history

    body = _multpl(
        [
            ("Sep 1, 2026", "<abbr title='Estimate'>†</abbr>25.96"),
            ("Oct 1, 2026", "26.29"),
            ("Oct 30, 2026", "25.86"),
        ]
    )
    history = parse_multpl_reference_history(
        body, observed_at=NOW, start="2026-09-01", end="2026-10-31"
    )
    assert len(history.observations) == 2  # Intramonth latest quote is excluded.
    assert history.at("2026-09-30", max_age_days=45).ke is None
    value = history.at("2026-10-01", max_age_days=45)
    assert value.pe_ttm == 25.96 and value.estimated
    assert value.source_date == date(2026, 9, 30)
    assert value.source_period_start == date(2026, 9, 1)
    assert value.published_at is None
    assert "not_verified_publication" in value.availability_basis
    assert value.valuation_basis == "third_party_index_reference"
    assert value.vintage_basis == "retrospective_uncertain_publication"
    assert not value.qualified_for_promotion
    assert value.ke == pytest.approx(0.02 + 1 / 25.96)
    assert history.at("2026-10-30", max_age_days=45).pe_ttm == 25.96
    assert history.at("2026-11-01", max_age_days=45).pe_ttm == 26.29
    assert not history.at("2026-11-01", max_age_days=45).estimated


def test_multpl_latest_missing_month_blocks_older_positive_reference():
    from src.data.historical_fund_ke import parse_multpl_reference_history

    history = parse_multpl_reference_history(
        _multpl([("Aug 1, 2026", "20.0"), ("Sep 1, 2026", "--")]),
        observed_at=NOW,
        start="2026-08-01",
        end="2026-09-30",
    )
    assert history.at("2026-10-01", max_age_days=45).ke is None
    assert (
        history.at("2026-10-01", max_age_days=45).reason == "no_positive_historical_pe"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/s-p-500-pe-ratio/table/by-month",
        "https://www.multpl.com/shiller-pe/table/by-month",
        "http://www.multpl.com/s-p-500-pe-ratio/table/by-month",
    ],
)
def test_multpl_is_an_independent_exact_source_not_an_official_allowlist_override(url):
    from src.data.historical_fund_ke import parse_multpl_reference_history

    with pytest.raises(ValueError, match="unexpected third-party"):
        parse_multpl_reference_history(
            _multpl([]),
            source_url=url,
            observed_at=NOW,
            start="2026-08-01",
            end="2026-09-30",
        )
    with pytest.raises(ValueError):
        _parse(
            [_row()],
            source_url="https://www.multpl.com/s-p-500-pe-ratio/table/by-month",
        )


@pytest.mark.parametrize(
    "body",
    [
        _multpl([("Sep 1, 2026", "25.0"), ("Sep 1, 2026", "26.0")]),
        _multpl([("not a date", "25.0")]),
        _multpl([("Sep 1, 2026", "25.0")]).replace(
            b"S&amp;P 500 PE Ratio", b"Shiller CAPE"
        ),
        _multpl([("Sep 1, 2026", "dividend yield 2.5%")]),
    ],
)
def test_multpl_conflicts_schema_or_other_valuation_cannot_enter_reference(body):
    from src.data.historical_fund_ke import parse_multpl_reference_history

    with pytest.raises(ValueError):
        parse_multpl_reference_history(
            body, observed_at=NOW, start="2026-08-01", end="2026-09-30"
        )
