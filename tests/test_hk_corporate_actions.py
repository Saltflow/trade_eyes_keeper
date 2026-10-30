"""Historical HK distributions keep currencies, announcement dates and gaps."""

from datetime import date
from html import escape
from types import SimpleNamespace

import pytest

from src.data.corporate_action_sources import reconcile_corporate_actions
from src.data.hk_corporate_actions import (
    EtnetCorporateActionProvider,
    HkexDividendEvidenceProvider,
    match_hkex_cash_notice,
    parse_etnet_dividend_html,
)
from src.data.market_history import CorporateAction, corporate_action_issues

START, END = date(2018, 3, 1), date(2026, 9, 24)
HEADERS = [
    "公布日期",
    "財政年度",
    "事項",
    "除淨日",
    "截止過戶日期由",
    "截止過戶日期至",
    "派送日",
]


def _row(**changes):
    values = {
        "announcement": "2026/08/26",
        "year": "2026/12",
        "particulars": "中期息港元 0.94",
        "ex": "2026/09/10",
        "book_start": "2026/09/14",
        "book_end": "2026/09/18",
        "payable": "2026/10/16",
    }
    values.update(changes)
    return list(values.values())


def _html(rows, *, history=True):
    if history:
        rows = rows + [
            _row(
                announcement="2017/03/23",
                year="2016/12",
                particulars="末期息港元 0.23",
                ex="2017/06/08",
                book_start="2017/06/12",
                book_end="2017/06/16",
                payable="2017/07/18",
            )
        ]
    return (
        "<table>"
        + "".join(
            "<tr>" + "".join(f"<td>{escape(value)}</td>" for value in row) + "</tr>"
            for row in [HEADERS, *rows]
        )
        + "</table>"
    )


def _parse(rows, code="00883", **kwargs):
    return parse_etnet_dividend_html(_html(rows, **kwargs), code, START, END)


def test_actual_cnooc_hkd_dividend_preserves_announced_and_payable_dates():
    (action,) = _parse([_row()])
    assert action.cash_per_share == 0.94
    assert action.share_multiplier == 1.0
    assert action.currency == "HKD"
    assert action.published_at == date(2026, 8, 26)
    assert action.ex_date == date(2026, 9, 10)
    assert action.payable_date == date(2026, 10, 16)
    assert action.record_date is None
    assert "book_closed_until=2026-09-18" in action.diagnostics
    assert corporate_action_issues([action]) == ()


def test_same_amount_yahoo_event_gains_real_announcement_without_double_cash():
    (source,) = _parse([_row()])
    yahoo = CorporateAction(
        code="00883",
        action_type="cash_dividend",
        ex_date=source.ex_date,
        cash_per_share=0.94,
        source="yahoo_chart_dividends",
        currency="HKD",
    )
    (action,) = reconcile_corporate_actions([yahoo], [source])
    assert action.cash_per_share == 0.94
    assert action.published_at == date(2026, 8, 26)
    assert action.payable_date == date(2026, 10, 16)
    assert yahoo.published_at is None


@pytest.mark.parametrize(
    "particulars,announcement,ex,payable",
    [
        (
            "3900 股派一股騰訊音樂ADS，可選現金",
            "2018/12/03",
            "2018/12/28",
            "2019/02/20",
        ),
        ("21 股派一股京東集團-SW A類普通股", "2021/12/23", "2022/01/20", "2022/03/25"),
        ("10 股派一股美團-W B類普通股", "2022/11/16", "2023/01/05", "2023/03/24"),
    ],
)
def test_tencent_actual_in_kind_distributions_remain_unresolved(
    particulars, announcement, ex, payable
):
    (action,) = _parse(
        [
            _row(
                particulars=particulars,
                announcement=announcement,
                ex=ex,
                payable=payable,
                book_start="--",
                book_end="--",
            )
        ],
        code="00700",
    )
    assert action.action_type == "in_kind_distribution"
    assert action.cash_per_share is None
    assert action.share_multiplier is None
    assert action.published_at.isoformat() == announcement.replace("/", "-")
    assert corporate_action_issues([action])


def test_later_hkd_conversion_is_not_backdated_to_rmb_earnings_announcement():
    (action,) = _parse(
        [
            _row(
                particulars="末期息人民幣 0.095 或港元 0.10322",
                announcement="2025/03/26",
                ex="2025/05/23",
                payable="2025/07/03",
                book_start="2025/05/27",
                book_end="2025/06/02",
            )
        ],
        code="01816",
    )
    assert action.cash_per_share == 0.10322
    assert action.currency == "HKD"
    assert action.published_at is None
    assert "etnet_announcement_date=2025-03-26" in action.diagnostics
    assert "hkd_cash_publication_date_unverified" in action.diagnostics
    assert corporate_action_issues([action])


def test_rmb_only_event_does_not_become_hkd_cash():
    (action,) = _parse([_row(particulars="末期息人民幣 0.147")], code="01339")
    assert action.action_type == "unresolved_cash_dividend"
    assert action.cash_per_share is None
    assert action.currency is None
    assert action.published_at is None
    assert "hkd_cash_amount_missing" in action.diagnostics
    assert corporate_action_issues([action])


def test_explicitly_hkd_primary_with_secondary_rmb_keeps_original_currency():
    (action,) = _parse([_row(particulars="末期息港元 0.125 或人民幣 0.104269")])
    assert action.cash_per_share == 0.125
    assert action.published_at == date(2026, 8, 26)
    assert corporate_action_issues([action]) == ()


def test_no_distribution_is_ignored_but_unknown_event_is_preserved():
    (action,) = _parse(
        [
            _row(particulars="不派第二次中期息", ex="--", payable="--"),
            _row(particulars="每十股派息港元 0.94"),
        ]
    )
    assert action.action_type == "unsupported_distribution"
    assert action.cash_per_share is None
    assert corporate_action_issues([action])


def test_rights_issue_is_preserved_and_reconciliation_rejects_it():
    (action,) = _parse([_row(particulars="每十股可認購一股，供股價港元 3")])
    assert action.action_type == "rights_issue"
    assert action.cash_per_share is None
    with pytest.raises(ValueError, match="unsupported rights"):
        reconcile_corporate_actions([action])


def test_identical_repeated_cash_notice_does_not_double_count():
    (action,) = _parse([_row(), _row(announcement="2026/09/01"), _row()])
    assert action.cash_per_share == 0.94
    assert action.published_at == date(2026, 9, 1)


@pytest.mark.parametrize("particulars", ["中期息港元 0.95", "特別股息港元 0.94"])
def test_conflicting_same_day_cash_requires_explicit_resolution(particulars):
    with pytest.raises(ValueError, match="ambiguous same-day"):
        _parse([_row(), _row(particulars=particulars)])


@pytest.mark.parametrize(
    "changes",
    [
        {"announcement": "2026/09/11"},
        {"announcement": "--"},
        {"ex": "--"},
        {"ex": "2026/09/31"},
        {"payable": "2026/09/01"},
        {"book_start": "2026/09/20"},
        {"particulars": "中期息港元 0"},
        {"particulars": "中期息港元 0.94 或港元 1"},
    ],
)
def test_invalid_or_inconsistent_cash_rows_are_rejected(changes):
    with pytest.raises(ValueError):
        _parse([_row(**changes)])


def test_empty_or_recent_only_history_cannot_claim_old_window_coverage():
    with pytest.raises(ValueError, match="does not reach requested start"):
        _parse([], history=False)
    with pytest.raises(ValueError, match="does not reach requested start"):
        _parse([_row()], history=False)
    assert _parse([]) == []


def test_absent_changed_or_truncated_table_is_rejected():
    with pytest.raises(ValueError, match="absent or ambiguous"):
        parse_etnet_dividend_html("<html>blocked</html>", "00883", START, END)
    with pytest.raises(ValueError, match="absent or ambiguous"):
        parse_etnet_dividend_html(
            _html([]).replace("公布日期", "日期"), "00883", START, END
        )
    with pytest.raises(ValueError, match="incomplete"):
        _parse([_row()[:-1]])


def test_unsupported_action_outside_window_is_not_imported():
    action = _row(
        particulars="每十股派一股", ex="2017/12/28", announcement="2017/12/01"
    )
    assert _parse([action]) == []


def test_one_bounded_http_request_and_utf8_history_decode():
    calls = []
    response = SimpleNamespace(
        content=_html([_row()]).encode("utf-8"),
        raise_for_status=lambda: None,
    )

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    provider = EtnetCorporateActionProvider(http=SimpleNamespace(get=get))
    (action,) = provider.fetch("00883", START, END)
    assert action.cash_per_share == 0.94
    assert len(calls) == 1
    assert calls[0][0].endswith("code=883")
    assert calls[0][1] == {"timeout": 20.0}


OFFICIAL_URL = (
    "https://www.hkexnews.hk/listedco/listconews/sehk/2025/0521/2025052101139.pdf"
)
OFFICIAL_FORM = """
Cash Dividend Announcement for Equity Issuer
Issuer name CGN Power Co., Ltd.
Stock code 01816
Announcement date 21 May 2025
Status Update to previous announcement
Reason for the update / change Update on exchange rate, dividend amount in HKD
Dividend declared RMB 0.095 per share
Default currency and amount in which
HKD 0.10322 per share
the dividend will be paid
Exchange rate RMB 1 : HKD 1.08656
Ex-dividend date 23 May 2025
Record date 02 June 2025
Payment date 03 July 2025
Information relating to withholding tax
"""


def _cgn_undated_cash():
    return _parse(
        [
            _row(
                particulars="末期息人民幣 0.095 或港元 0.10322",
                announcement="2025/03/26",
                ex="2025/05/23",
                payable="2025/07/03",
                book_start="2025/05/27",
                book_end="2025/06/02",
            ),
        ],
        code="01816",
    )[0]


def _match_form(text=OFFICIAL_FORM, action=None, published_at=date(2025, 5, 21)):
    return match_hkex_cash_notice(
        text,
        action or _cgn_undated_cash(),
        published_at=published_at,
        source_url=OFFICIAL_URL,
    )


def test_actual_hkex_update_supplies_later_hkd_publication_and_real_record_date():
    source = _cgn_undated_cash()
    result = _match_form(action=source)
    assert result.published_at == date(2025, 5, 21)
    assert result.published_at != date(2025, 3, 26)
    assert result.cash_per_share == 0.10322
    assert result.record_date == date(2025, 6, 2)
    assert result.source_url == OFFICIAL_URL
    assert corporate_action_issues([result]) == ()
    assert source.published_at is None


@pytest.mark.parametrize(
    "changed",
    [
        OFFICIAL_FORM.replace("Stock code 01816", "Stock code 01339"),
        OFFICIAL_FORM.replace(
            "Ex-dividend date 23 May 2025", "Ex-dividend date 22 May 2025"
        ),
        OFFICIAL_FORM.replace("RMB 0.095 per share", "RMB 0.096 per share"),
        OFFICIAL_FORM.replace("HKD 0.10322 per share", "HKD amount to be announced"),
    ],
)
def test_official_notice_must_identify_same_issuer_entitlement_and_declared_cash(
    changed,
):
    assert _match_form(changed) is None


@pytest.mark.parametrize("published_at", [date(2025, 3, 25), date(2025, 5, 24)])
def test_document_outside_announcement_to_ex_window_cannot_backfill_date(published_at):
    assert _match_form(published_at=published_at) is None


@pytest.mark.parametrize(
    "changed,error",
    [
        (
            OFFICIAL_FORM.replace("HKD 0.10322 per share", "HKD 0.103221 per share"),
            "conflicting official HKD cash",
        ),
        (
            OFFICIAL_FORM.replace(
                "Payment date 03 July 2025", "Payment date 04 July 2025"
            ),
            "conflicting official payment date",
        ),
        (
            OFFICIAL_FORM.replace(
                "Announcement date 21 May 2025", "Announcement date 22 May 2025"
            ),
            "predates",
        ),
    ],
)
def test_conflicting_later_official_terms_are_not_ignored(changed, error):
    with pytest.raises(ValueError, match=error):
        _match_form(changed)


def test_exchange_rate_and_net_cash_tax_table_do_not_replace_gross_dividend():
    result = _match_form(OFFICIAL_FORM + "Net amount HKD 0.092898 per share")
    assert result.cash_per_share == 0.10322


def test_legacy_cgn_notice_matches_real_payment_date_and_both_currency_amounts():
    (action,) = _parse(
        [
            _row(
                particulars="末期息人民幣 0.068 或港元 0.0834",
                announcement="2018/03/08",
                ex="2018/06/07",
                payable="2018/07/18",
                book_start="2018/06/09",
                book_end="2018/06/14",
            ),
        ],
        code="01816",
    )
    text = """(Stock Code : 1816)
    The Company will pay an annual final dividend around Wednesday, July 18, 2018
    in cash. The dividend shall be denominated in RMB at RMB0.068 per Share
    (inclusive of tax), i.e. a cash dividend of HK$0.08340 per Share will be paid.
    """
    result = match_hkex_cash_notice(
        text, action, published_at=date(2018, 5, 30), source_url=OFFICIAL_URL
    )
    assert result.published_at == date(2018, 5, 30)
    assert result.ex_date == date(2018, 6, 7)
    assert result.record_date is None
    assert "hkex_event_match=payable_date_and_cash" in result.diagnostics
    assert (
        match_hkex_cash_notice(
            text.replace("July 18, 2018", "July 19, 2018"),
            action,
            published_at=date(2018, 5, 30),
            source_url=OFFICIAL_URL,
        )
        is None
    )


def test_official_filing_can_supply_missing_hkd_amount_for_rmb_only_event():
    source = _cgn_undated_cash()
    source.cash_per_share = None
    source.action_type = "unresolved_cash_dividend"
    source.diagnostics[0] = "etnet_particulars=末期息人民幣 0.095"
    result = _match_form(action=source)
    assert result.cash_per_share == 0.10322
    assert result.currency == "HKD"
    assert result.published_at == date(2025, 5, 21)


def test_in_kind_event_cannot_be_rewritten_by_unrelated_cash_notice():
    source = _cgn_undated_cash()
    source.action_type = "in_kind_distribution"
    assert _match_form(action=source) is None


def test_refine_stops_after_latest_conflicting_evidence_and_keeps_gap():
    calls = []
    response = _notice_response(b"latest")
    provider = HkexDividendEvidenceProvider(
        http=SimpleNamespace(get=lambda *a, **kw: calls.append(a[0]) or response)
    )
    provider._announcements = lambda *args: [
        {"published_at": date(2025, 5, 22), "url": OFFICIAL_URL},
        {"published_at": date(2025, 5, 21), "url": OFFICIAL_URL + "?old=1"},
    ]
    provider._notice_text = lambda content: OFFICIAL_FORM.replace(
        "HKD 0.10322 per share", "HKD 0.104 per share"
    )
    (result,) = provider.refine([_cgn_undated_cash()])
    assert result.published_at is None
    assert len(calls) == 1
    assert any(x.startswith("hkex_cash_evidence_conflict=") for x in result.diagnostics)


def test_refine_respects_document_budget_and_preserves_unresolved_input():
    calls = []
    response = _notice_response(b"unrelated")
    provider = HkexDividendEvidenceProvider(
        {"point_in_time_data": {"hkex_dividend_max_documents": 1}},
        http=SimpleNamespace(get=lambda *a, **kw: calls.append(a[0]) or response),
    )
    provider._announcements = lambda *args: [
        {"published_at": date(2025, 5, 21), "url": OFFICIAL_URL},
        {"published_at": date(2025, 5, 20), "url": OFFICIAL_URL + "?previous=1"},
    ]
    provider._notice_text = lambda content: "unrelated issuer"
    (result,) = provider.refine([_cgn_undated_cash()])
    assert result.published_at is None
    assert len(calls) == 1


def test_official_search_truncation_cannot_be_treated_as_full_coverage():
    response = SimpleNamespace(
        text="<div>Total records found: 2</div><table><tr><td class='release-time'>21/05/2025</td><td class='doc-link'><a href='/a.pdf'>Dividend</a></td></tr></table>",
        raise_for_status=lambda: None,
    )
    provider = HkexDividendEvidenceProvider(
        http=SimpleNamespace(post=lambda *a, **kw: response)
    )
    provider.provider._stock_ids = {"01816": "115406"}
    with pytest.raises(ValueError, match="incomplete HKEX"):
        provider._announcements(
            "01816", date(2018, 3, 1), date(2026, 9, 24), "dividend"
        )


def _picc_rmb_only():
    return _parse(
        [
            _row(
                particulars="中期息人民幣 0.036",
                announcement="2020/08/21",
                ex="2020/11/02",
                payable="2020/12/18",
                book_start="2020/11/04",
                book_end="2020/11/09",
            ),
        ],
        code="01339",
    )[0]


PICC_FX_NOTICE = """
(Stock Code: 1339)
The Company will distribute the interim dividend for the half year ended
30 June 2020 on or around 18 December 2020 (Friday).
The interim dividend is denominated in RMB, which is RMB0.36 per 10 shares
(inclusive of tax). The applicable exchange rate for calculating the amount
of interim dividend on H Shares is HK$1 = RMB0.862364.
"""


def test_actual_declared_cash_and_implementation_fx_are_explicitly_converted():
    result = match_hkex_cash_notice(
        PICC_FX_NOTICE,
        _picc_rmb_only(),
        published_at=date(2020, 10, 28),
        source_url=OFFICIAL_URL,
    )
    assert result.cash_per_share == pytest.approx(0.036 / 0.862364)
    assert result.published_at == date(2020, 10, 28)
    assert "hkd_cash_from_disclosed_rmb_and_fx=0.036/0.862364" in result.diagnostics
    assert result.payable_date == date(2020, 12, 18)
    assert corporate_action_issues([result]) == ()


@pytest.mark.parametrize(
    "changed",
    [
        PICC_FX_NOTICE.replace("RMB0.36 per 10 shares", "RMB0.37 per 10 shares"),
        PICC_FX_NOTICE.replace("HK$1 = RMB0.862364", "HK$1 = RMB0"),
        PICC_FX_NOTICE.replace("HK$1 = RMB0.862364", "RMB1 = HK$0.862364"),
        PICC_FX_NOTICE.replace("18 December 2020", "19 December 2020"),
        PICC_FX_NOTICE.replace(
            "applicable exchange rate for calculating",
            "historical price assumption for calculating",
        ),
    ],
)
def test_cash_cannot_be_inferred_from_wrong_terms_date_or_unspecified_fx(changed):
    assert (
        match_hkex_cash_notice(
            changed,
            _picc_rmb_only(),
            published_at=date(2020, 10, 28),
            source_url=OFFICIAL_URL,
        )
        is None
    )


@pytest.mark.parametrize("page_count", [9, 32])
def test_late_implementation_pages_are_kept_in_official_notice_evidence(page_count):
    provider = HkexDividendEvidenceProvider()
    content = _pdf_pages(["opening"] * (page_count - 1) + [PICC_FX_NOTICE])
    text = provider._notice_text(content)
    assert "HK$1 = RMB0.862364" in text
    result = match_hkex_cash_notice(
        text,
        _picc_rmb_only(),
        published_at=date(2020, 10, 28),
        source_url=OFFICIAL_URL,
    )
    assert result.cash_per_share == pytest.approx(0.036 / 0.862364)
    assert corporate_action_issues([result]) == ()


def test_image_only_stock_code_needs_matching_official_listing_identity():
    text = PICC_FX_NOTICE.replace("(Stock Code: 1339)", "PICC GROUP")
    kwargs = {"published_at": date(2020, 10, 28), "source_url": OFFICIAL_URL}
    assert match_hkex_cash_notice(text, _picc_rmb_only(), **kwargs) is None
    assert (
        match_hkex_cash_notice(text, _picc_rmb_only(), listed_code="01816", **kwargs)
        is None
    )
    result = match_hkex_cash_notice(
        text, _picc_rmb_only(), listed_code="01339", **kwargs
    )
    assert result.cash_per_share == pytest.approx(0.036 / 0.862364)
    assert "issuer_identity=hkex_title_search" in result.diagnostics


def test_listing_identity_cannot_override_a_different_explicit_pdf_issuer():
    assert (
        match_hkex_cash_notice(
            PICC_FX_NOTICE.replace("Stock Code: 1339", "Stock Code: 1816"),
            _picc_rmb_only(),
            published_at=date(2020, 10, 28),
            source_url=OFFICIAL_URL,
            listed_code="01339",
        )
        is None
    )


def _pdf_pages(texts):
    """Tiny uncompressed PDF fixture, without a test-only PDF writer dependency."""
    objects = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in texts:
        page_id = len(objects) + 1
        kids.append(f"{page_id} 0 R")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {page_id + 1} 0 R >>".encode()
        )
        lines = ["BT /F1 10 Tf 30 750 Td 12 TL"]
        for line in text.splitlines():
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"({escaped}) Tj T*")
        stream = ("\n".join([*lines, "ET"]) + "\n").encode("ascii")
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream"
        )
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (
        f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
    )
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(content)
    content.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(content)


def _notice_response(content):
    return SimpleNamespace(
        content=content,
        headers={},
        raise_for_status=lambda: None,
        iter_content=lambda chunk_size: iter([content]),
        close=lambda: None,
    )


def test_long_unrelated_notice_does_not_abort_later_verified_picc_evidence():
    calls = []
    provider = HkexDividendEvidenceProvider(
        http=SimpleNamespace(
            get=lambda url, **kw: calls.append(url) or _notice_response(url.encode())
        )
    )
    long_url = OFFICIAL_URL + "?long=1"
    provider._announcements = lambda *args: [
        {"published_at": date(2020, 10, 29), "url": long_url},
        {"published_at": date(2020, 10, 28), "url": OFFICIAL_URL},
    ]

    def extract(content):
        if content == long_url.encode():
            raise ValueError("HKEX cash notice exceeds the 32-page evidence limit")
        return PICC_FX_NOTICE

    provider._notice_text = extract
    source = _picc_rmb_only()
    (result,) = provider.refine([source])
    assert result.published_at == date(2020, 10, 28)
    assert result.cash_per_share == pytest.approx(0.036 / 0.862364)
    assert corporate_action_issues([result]) == ()
    assert source.published_at is None
    assert len(calls) == 2
    assert any(long_url in item for item in result.diagnostics)


@pytest.mark.parametrize("page_count", [33, 200])
def test_long_pdf_enumerates_only_33_pages_and_extracts_none(monkeypatch, page_count):
    from pdfminer.pdfpage import PDFPage

    read_pages = []
    original = PDFPage.get_pages

    def counted_pages(*args, **kwargs):
        assert kwargs["caching"] is False
        for page in original(*args, **kwargs):
            read_pages.append(page)
            yield page

    monkeypatch.setattr(PDFPage, "get_pages", counted_pages)
    monkeypatch.setattr(
        "pdfminer.pdfinterp.PDFPageInterpreter.process_page",
        lambda *args: pytest.fail("Long documents must not enter layout extraction"),
    )
    provider = HkexDividendEvidenceProvider()
    with pytest.raises(ValueError, match="32-page"):
        provider._notice_text(_pdf_pages([PICC_FX_NOTICE] * page_count))
    assert len(read_pages) == 33


def test_pdf_byte_limit_is_checked_before_parser(monkeypatch):
    monkeypatch.setattr("src.data.hk_corporate_actions._NOTICE_MAX_BYTES", 10)
    monkeypatch.setattr(
        "pdfminer.pdfpage.PDFPage.get_pages",
        lambda *a, **kw: pytest.fail("Oversized bytes must not enter parser"),
    )
    with pytest.raises(ValueError, match="document size limit"):
        HkexDividendEvidenceProvider()._notice_text(b"x" * 11)


def test_extracted_text_limit_rejects_whole_notice(monkeypatch):
    monkeypatch.setattr("src.data.hk_corporate_actions._NOTICE_MAX_TEXT_CHARS", 20)
    with pytest.raises(ValueError, match="extracted text limit"):
        HkexDividendEvidenceProvider()._notice_text(_pdf_pages([PICC_FX_NOTICE]))


def test_stream_download_stops_at_byte_budget_and_closes_response(monkeypatch):
    monkeypatch.setattr("src.data.hk_corporate_actions._NOTICE_MAX_BYTES", 10)
    consumed, closed = [], []

    def chunks(chunk_size):
        assert chunk_size == 64 * 1024
        for index in range(100):
            consumed.append(index)
            yield b"abcdef"

    response = _notice_response(b"")
    response.iter_content = chunks
    response.close = lambda: closed.append(True)

    def get(url, **kwargs):
        assert kwargs["stream"] is True
        return response

    provider = HkexDividendEvidenceProvider(http=SimpleNamespace(get=get))
    with pytest.raises(ValueError, match="document size limit"):
        provider._download_notice(OFFICIAL_URL)
    assert consumed == [0, 1]
    assert closed == [True]


@pytest.mark.parametrize("failure", ["corrupt", "long", "http", "timeout"])
def test_unavailable_notice_keeps_gap_once_per_call_and_can_retry(failure, monkeypatch):
    monkeypatch.setattr("src.data.hk_corporate_actions.time.sleep", lambda delay: None)
    import requests

    calls = []
    bad_content = (
        _pdf_pages([PICC_FX_NOTICE] * 33) if failure == "long" else b"not a PDF"
    )

    def get(url, **kwargs):
        calls.append(url)
        if failure == "timeout":
            raise requests.Timeout("timeout")
        response = _notice_response(bad_content)
        if failure == "http":

            def fail():
                raise requests.HTTPError("503 unavailable")

            response.raise_for_status = fail
        return response

    provider = HkexDividendEvidenceProvider(http=SimpleNamespace(get=get))
    provider._announcements = lambda *args: [
        {"published_at": date(2020, 10, 28), "url": OFFICIAL_URL}
    ]
    source = _picc_rmb_only()
    result = provider.refine([source, source])
    attempts = 2 if failure == "timeout" else 1
    assert len(calls) == attempts  # shared by both events and both title searches
    assert OFFICIAL_URL not in provider._text_cache
    for action in result:
        assert action.published_at is None
        assert action.cash_per_share is None
        assert corporate_action_issues([action])
        assert len([d for d in action.diagnostics if "unavailable=" in d]) == 1
    assert not any("unavailable=" in d for d in source.diagnostics)
    provider.refine([source])
    assert len(calls) == attempts * 2  # failure cache does not survive a call


def test_failed_downloads_consume_budget_without_poisoning_other_issuers():
    import requests

    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("?picc"):
            raise requests.Timeout("timeout")
        return _notice_response(_pdf_pages([OFFICIAL_FORM]))

    provider = HkexDividendEvidenceProvider(
        {"point_in_time_data": {"hkex_dividend_max_documents": 1}},
        http=SimpleNamespace(get=get),
    )
    provider._announcements = lambda code, *args: (
        [
            {"published_at": date(2020, 10, 28), "url": OFFICIAL_URL + "?picc"},
            {"published_at": date(2020, 10, 27), "url": OFFICIAL_URL + "?extra"},
        ]
        if code == "01339"
        else [{"published_at": date(2025, 5, 21), "url": OFFICIAL_URL}]
    )
    picc, cgn = provider.refine([_picc_rmb_only(), _cgn_undated_cash()])
    assert picc.published_at is None
    assert "hkex_cash_evidence_document_budget_exhausted" in picc.diagnostics
    assert cgn.published_at == date(2025, 5, 21)
    assert corporate_action_issues([cgn]) == ()
    assert calls == [OFFICIAL_URL + "?picc", OFFICIAL_URL]


@pytest.mark.parametrize(
    "text",
    [
        "Poll results: ordinary business only.",
        PICC_FX_NOTICE.replace("Stock Code: 1339", "Stock Code: 1816"),
        PICC_FX_NOTICE.replace("18 December 2020", "19 December 2020"),
        PICC_FX_NOTICE.replace("RMB0.36", "RMB0.37"),
        PICC_FX_NOTICE.replace("HK$1 = RMB0.862364", "USD1 = RMB0.862364"),
    ],
)
def test_pdf_refinement_never_admits_unrelated_or_unverified_terms(text):
    provider = HkexDividendEvidenceProvider(
        http=SimpleNamespace(get=lambda *a, **kw: _notice_response(_pdf_pages([text])))
    )
    provider._announcements = lambda *args: [
        {
            "published_at": date(2020, 10, 28),
            "url": OFFICIAL_URL,
            "listed_code": "01339",
        }
    ]
    (result,) = provider.refine([_picc_rmb_only()])
    assert result.published_at is None
    assert result.cash_per_share is None
    assert corporate_action_issues([result])


def test_successful_notice_cache_is_bounded_across_calls():
    calls = []
    provider = HkexDividendEvidenceProvider(
        {"point_in_time_data": {"hkex_dividend_max_documents": 1}},
        http=SimpleNamespace(
            get=lambda url, **kw: (
                calls.append(url) or _notice_response(_pdf_pages([PICC_FX_NOTICE]))
            )
        ),
    )
    for suffix in ("first", "second", "first"):
        url = OFFICIAL_URL + "?" + suffix
        provider._announcements = lambda *args, url=url: [
            {"published_at": date(2020, 10, 28), "url": url}
        ]
        (result,) = provider.refine([_picc_rmb_only()])
        assert result.published_at == date(2020, 10, 28)
        assert list(provider._text_cache) == [url]
    # Production also reuses integrity-checked disk entries after LRU eviction.
    assert len(calls) == 2


def test_real_pdf_pipeline_skips_long_and_unrelated_before_valid_notice():
    documents = {
        OFFICIAL_URL + "?long": _pdf_pages([PICC_FX_NOTICE] * 33),
        OFFICIAL_URL + "?unrelated": _pdf_pages(["Ordinary poll results"]),
        OFFICIAL_URL: _pdf_pages(["opening"] * 8 + [PICC_FX_NOTICE]),
    }
    provider = HkexDividendEvidenceProvider(
        http=SimpleNamespace(get=lambda url, **kw: _notice_response(documents[url]))
    )
    provider._announcements = lambda *args: [
        {"published_at": date(2020, 10, 28), "url": url, "listed_code": "01339"}
        for url in documents
    ]
    (result,) = provider.refine([_picc_rmb_only()])
    assert result.published_at == date(2020, 10, 28)
    assert result.cash_per_share == pytest.approx(0.036 / 0.862364)
    assert result.source_url == OFFICIAL_URL
    assert corporate_action_issues([result]) == ()
    assert any("32-page" in item for item in result.diagnostics)
    assert OFFICIAL_URL + "?long" not in provider._text_cache


def test_captured_picc_form_preserves_positioned_label_value_rows():
    from pathlib import Path

    content = (Path(__file__).parent / "fixtures/hkex/2026092901567.pdf").read_bytes()
    (action,) = parse_etnet_dividend_html(
        _html(
            [
                _row(
                    particulars="中期息人民幣 0.11 或港元 0.12791962",
                    announcement="2026/08/28",
                    ex="2026/10/07",
                    payable="2026/11/06",
                    book_start="2026/10/09",
                    book_end="2026/10/14",
                )
            ]
        ),
        "01339",
        START,
        date(2026, 10, 15),
    )
    provider = HkexDividendEvidenceProvider(
        http=SimpleNamespace(get=lambda *a, **kw: _notice_response(content))
    )
    url = (
        "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0929/2026092901567.pdf"
    )
    provider._announcements = lambda *args: [
        {"published_at": date(2026, 9, 29), "url": url, "listed_code": "01339"}
    ]
    (result,) = provider.refine([action])
    assert result.published_at == date(2026, 9, 29)
    assert result.cash_per_share == 0.12791962
    assert result.currency == "HKD"
    assert result.ex_date == date(2026, 10, 7)
    assert result.record_date == date(2026, 10, 14)
    assert result.payable_date == date(2026, 11, 6)
    assert corporate_action_issues([result]) == ()


@pytest.fixture(autouse=True)
def _isolated_notice_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


PICC_2018_VOTE_NOTICE = """
(Stock Code: 1339)
The H share register of members of the Company on 1 May 2018 determines
entitlement to the final dividend. The Company will distribute on around
15 May 2018. The declared amount is RMB0.394 per 10 shares. The applicable
exchange rate for calculating the H share dividend is HK$1=RMB0.800536.
"""


def _picc_2018_rmb_only():
    return _parse(
        [
            _row(
                particulars="末期息人民幣 0.0394",
                announcement="2018/03/23",
                ex="2018/04/24",
                payable="2018/05/25",
                book_start="2018/04/26",
                book_end="2018/05/01",
            )
        ],
        code="01339",
    )[0]


def test_official_record_and_cash_resolve_approximate_pay_date_conflict():
    source = _picc_2018_rmb_only()
    result = match_hkex_cash_notice(
        PICC_2018_VOTE_NOTICE,
        source,
        published_at=date(2018, 4, 19),
        source_url=OFFICIAL_URL,
    )
    assert result.cash_per_share == pytest.approx(0.0394 / 0.800536)
    assert result.record_date == date(2018, 5, 1)
    assert result.payable_date is None
    assert result.published_at == date(2018, 4, 19)
    assert "hkex_event_match=record_date_and_cash" in result.diagnostics
    assert "payable_date_unverified_approximate_hkex_notice" in result.diagnostics
    assert corporate_action_issues([result]) == ()
    assert source.published_at is None


@pytest.mark.parametrize(
    "changed",
    [
        PICC_2018_VOTE_NOTICE.replace("on 1 May 2018", "on 2 May 2018"),
        PICC_2018_VOTE_NOTICE.replace("RMB0.394", "RMB0.395"),
        PICC_2018_VOTE_NOTICE.replace("Stock Code: 1339", "Stock Code: 1816"),
    ],
)
def test_approximate_notice_requires_record_cash_and_issuer(changed):
    assert (
        match_hkex_cash_notice(
            changed,
            _picc_2018_rmb_only(),
            published_at=date(2018, 4, 19),
            source_url=OFFICIAL_URL,
        )
        is None
    )


@pytest.mark.parametrize("notice_url", [OFFICIAL_URL, OFFICIAL_URL + "?file=20240301"])
def test_long_notice_diagnostic_survives_optimizer_readiness(
    monkeypatch, tmp_path, notice_url
):
    import pandas as pd

    from src.data import backtest_data
    from src.data.market_history import PriceHistoryBundle
    from src.data.price_adjustments import apply_disclosed_adjustments

    provider = HkexDividendEvidenceProvider(
        http=SimpleNamespace(
            get=lambda *a, **kw: _notice_response(_pdf_pages([PICC_FX_NOTICE] * 33))
        )
    )
    provider._announcements = lambda *args: [
        {"published_at": date(2020, 10, 28), "url": notice_url}
    ]
    source = _picc_rmb_only()
    raw = PriceHistoryBundle(code="01339", prices=pd.DataFrame(), actions=[])

    def fetch(code, start, end):
        actions = provider.refine([source])
        assert actions[0].published_at is None
        return apply_disclosed_adjustments(raw, actions, evidence="official HKEX")

    class Store:
        def __init__(self, root):
            pass

        def read(self, code):
            return None

        def write(self, bundle):
            pytest.fail("Unverified actions must never be cached")

    monkeypatch.setattr(backtest_data, "PointInTimeMarketStore", Store)
    monkeypatch.setattr(
        backtest_data,
        "MarketHistoryProvider",
        lambda config: SimpleNamespace(fetch=fetch),
    )
    monkeypatch.setattr(
        backtest_data,
        "resolve_market_data_cutoff",
        lambda code, end, **kw: SimpleNamespace(effective_end=end, as_dict=dict),
    )
    result = backtest_data.prepare_backtest_data(
        {"point_in_time_data": {"output_dir": str(tmp_path)}},
        ["01339"],
        date(2020, 1, 1),
        date(2020, 11, 3),
        purpose="optimizer",
    )
    assert not result.ready
    assert result.bundles == {}
    assert result.fetched_codes == []
    assert "unresolved corporate action factor" in result.issues[0].reason
    assert notice_url in result.issues[0].reason
    assert result.issues[0].category == "corporate_action_evidence_missing"
    assert "hkex_cash_evidence_unavailable=" in result.issues[0].reason
    assert "32-page evidence limit" in result.issues[0].reason
    assert result.issues[0].reason.endswith(result.fetch_attempts[-1].reason)
    assert source.published_at is None


@pytest.mark.parametrize("failure", ["timeout", "503", "partial"])
def test_captured_2018_poll_notice_recovers_one_transient_download(monkeypatch, failure):
    monkeypatch.setattr("src.data.hk_corporate_actions.time.sleep", lambda delay: None)
    from pathlib import Path

    import requests

    content = (Path(__file__).parent / "fixtures/hkex/ltn201804191437.pdf").read_bytes()
    url = "https://www1.hkexnews.hk/listedco/listconews/sehk/2018/0419/ltn201804191437.pdf"
    calls = []

    def get(request_url, **kwargs):
        calls.append(request_url)
        if len(calls) == 1:
            if failure == "timeout":
                raise requests.Timeout("temporary notice timeout")
            if failure == "partial":
                response = _notice_response(content)

                def interrupted(chunk_size):
                    yield content[:200]
                    raise requests.ConnectionError("interrupted response body")

                response.iter_content = interrupted
                return response
            response = requests.Response()
            response.status_code = 503
            raise requests.HTTPError("temporary service failure", response=response)
        return _notice_response(content)

    provider = HkexDividendEvidenceProvider(http=SimpleNamespace(get=get))
    provider._announcements = lambda code, start, end, title: (
        [{"published_at": date(2018, 4, 19), "url": url, "listed_code": "01339"}]
        if title in {"poll results", "results"}
        else []
    )
    source = _picc_2018_rmb_only()
    (result,) = provider.refine([source])
    assert len(calls) == 2
    assert result.published_at == date(2018, 4, 19)
    assert result.ex_date == date(2018, 4, 24)
    assert result.record_date == date(2018, 5, 1)
    assert result.cash_per_share == pytest.approx(0.0394 / 0.800536)
    assert result.currency == "HKD"
    assert result.payable_date is None
    assert corporate_action_issues([result]) == ()
    assert source.published_at is None


@pytest.mark.parametrize("budget, expected", [(1, 1), (2, 2), (3, 3)])
def test_transient_retries_share_total_document_request_budget(monkeypatch, budget, expected):
    import requests

    monkeypatch.setattr("src.data.hk_corporate_actions.time.sleep", lambda delay: None)
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        raise requests.Timeout("temporary failure")

    provider = HkexDividendEvidenceProvider(
        {"point_in_time_data": {"hkex_dividend_max_documents": budget}},
        http=SimpleNamespace(get=get),
    )
    provider._announcements = lambda *args: [
        {"published_at": date(2018, 4, 19), "url": OFFICIAL_URL + suffix}
        for suffix in ("?one", "?two", "?three")
    ]
    (result,) = provider.refine([_picc_2018_rmb_only()])
    assert len(calls) == expected
    assert max(calls.count(url) for url in calls) <= 2
    assert not provider._text_cache
    assert corporate_action_issues([result])
    assert "hkex_cash_evidence_document_budget_exhausted" in result.diagnostics


@pytest.mark.parametrize(
    "status, retry_after, expected",
    [(503, None, 1.0), (429, "2", 2.0), (503, "5", 5.0),
     (503, "6", None), (503, "-1", None),
     (503, "Wed, 01 Jan 2020 00:00:00 GMT", 0.0),
     (503, "Thu, 01 Jan 2099 00:00:00 GMT", None),
     (429, "nonsense", None), (503, "nan", None), (503, "inf", None),
     (403, None, None), (404, None, None), (400, None, None)],
)
def test_notice_retry_only_for_bounded_transient_http(status, retry_after, expected):
    import requests

    from src.data.hk_corporate_actions import _notice_retry_delay

    response = requests.Response()
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    assert _notice_retry_delay(requests.HTTPError(response=response)) == expected
    assert _notice_retry_delay(ValueError("32-page evidence limit")) is None
