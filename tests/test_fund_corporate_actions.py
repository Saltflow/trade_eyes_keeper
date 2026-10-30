from datetime import date

import pandas as pd
import pytest
import requests

from src.data.fund_corporate_actions import (
    FundActionEvidenceError,
    SseFundCorporateActionProvider,
    parse_distribution_announcement,
    parse_split_announcement,
    verify_split_market_basis,
)

NOTICE = """
华泰柏瑞沪深300交易型开放式指数证券投资基金分红公告
公告送出日期：2025 年 6 月 11 日
基金主代码 510300
本次分红方案（单位：元/10 份基金份额） 0.880
权益登记日 2025年6月17日
除息日 2025年6月18日
现金红利发放日 2025年6月27日
"""
PDF_URL = "/disclosure/fund/announcement/c/new/2025-06-11/510300_notice.pdf"
SPLIT_NOTICE = """
基金份额拆分结果与实施安排
基金代码：510500
份额拆分日：2022年8月26日。
本基金本次基金份额拆分比例为1.14539。
份额拆分日日终，进行变更登记。
按照“上进位”的原则进行取整，小数点后基金份额均进位为1份。
份额拆分日的下一工作日（2022年8月29日）恢复相关业务。
"""


def split_prices():
    return pd.DataFrame(
        {
            "date": ["2022-08-26", "2022-08-29"],
            "raw_close": [7.220, 6.300],
            "qfq_close": [5.915, 5.911],
        }
    )


class Response:
    def __init__(self, data=None, content=b"%PDF-fake", url=""):
        self.data = data
        self.content = content
        self.url = url

    def json(self):
        return self.data

    def raise_for_status(self):
        return None


def index(rows, *, page=1, total=None):
    return {
        "pageHelp": {"pageNo": page, "total": len(rows) if total is None else total},
        "result": rows,
    }


def row(**kwargs):
    return {
        "SECURITY_CODE": "510300",
        "SSEDATE": "2025-06-11",
        "TITLE": "华泰柏瑞沪深300交易型开放式指数证券投资基金分红公告",
        "URL": PDF_URL,
        **kwargs,
    }


def parse(text=NOTICE, disclosed_at=date(2025, 6, 11)):
    return parse_distribution_announcement(
        text,
        code="510300",
        disclosed_at=disclosed_at,
        source_url="https://www.sse.com.cn" + PDF_URL,
    )


def test_actual_announcement_units_and_dates():
    action = parse()
    assert action.cash_per_share == pytest.approx(0.088)
    assert action.published_at == date(2025, 6, 11)
    assert action.ex_date == date(2025, 6, 18)
    assert action.record_date == date(2025, 6, 17)
    assert action.payable_date == date(2025, 6, 27)
    assert action.source == "sse_fund_distribution"


def test_later_of_exchange_and_printed_date_is_causal():
    assert parse(disclosed_at=date(2025, 6, 12)).published_at == date(2025, 6, 12)
    assert parse(disclosed_at=date(2025, 6, 10)).published_at == date(2025, 6, 11)


def test_exchange_date_can_supply_missing_printed_date():
    text = NOTICE.replace("公告送出日期：2025 年 6 月 11 日", "")
    assert parse(text).published_at == date(2025, 6, 11)


@pytest.mark.parametrize(
    "before,after",
    [
        ("基金主代码 510300", "基金主代码 510880"),
        ("元/10 份基金份额", "元"),
        ("0.880", "-0.880"),
        ("0.880", "0.000"),
        ("除息日 2025年6月18日", "除息日 待定"),
        ("权益登记日 2025年6月17日", "权益登记日 2025年6月19日"),
        ("现金红利发放日 2025年6月27日", "现金红利发放日 2025年6月16日"),
        ("公告送出日期：2025 年 6 月 11 日", "公告送出日期：2025 年 6 月 19 日"),
    ],
)
def test_unusable_notice_is_rejected(before, after):
    with pytest.raises(FundActionEvidenceError):
        parse(NOTICE.replace(before, after))


def test_multi_class_cash_table_is_not_silently_assigned():
    with pytest.raises(FundActionEvidenceError, match="amount/unit"):
        parse(NOTICE + "\n本次分红方案（单位：元/10份基金份额） 1.00")


def test_non_ten_unit_uses_explicit_denominator():
    assert parse(NOTICE.replace("元/10 份", "元/100份")).cash_per_share == 0.0088


def test_full_paginated_index_and_pdf_evidence(tmp_path, monkeypatch):
    calls = []

    def http_get(url, **kwargs):
        calls.append((url, kwargs))
        if "params" in kwargs:
            page = kwargs["params"]["pageHelp.pageNo"]
            if page == 1:
                return Response(index([row()], page=1, total=2))
            return Response(
                index(
                    [row(URL="/other.pdf", TITLE="部分基金管理人公告")], page=2, total=2
                )
            )
        return Response(url=url)

    provider = SseFundCorporateActionProvider(
        http_get=http_get, evidence_dir=tmp_path, page_size=1
    )
    monkeypatch.setattr(provider, "_pdf_text", lambda _: NOTICE)
    actions = provider.fetch_actions("510300", date(2018, 3, 28), date(2026, 9, 23))
    assert len(actions) == 1
    assert len(calls) == 3
    assert calls[0][1]["params"]["TITLE"] == ""
    assert calls[0][1]["params"]["START_DATE"] == ""
    assert len(list(tmp_path.glob("*.index.json"))) == 2
    assert len(list(tmp_path.glob("*.pdf"))) == 1
    assert len(list(tmp_path.glob("*.manifest.json"))) == 1


@pytest.mark.parametrize("failure", ["missing", "repeat", "changed", "wrong_fund"])
def test_partial_index_is_never_empty_success(failure):
    def http_get(url, **kwargs):
        page = kwargs["params"]["pageHelp.pageNo"]
        if failure == "wrong_fund":
            return Response(index([row(SECURITY_CODE="510880")]))
        if page == 1:
            return Response(index([row()], total=2))
        if failure == "missing":
            return Response(index([], page=2, total=2))
        if failure == "repeat":
            return Response(index([row()], page=2, total=2))
        return Response(index([row(URL="/next.pdf")], page=2, total=3))

    provider = SseFundCorporateActionProvider(http_get=http_get, page_size=1)
    with pytest.raises(FundActionEvidenceError):
        provider.fetch_actions("510300", date(2018, 3, 28), date(2026, 9, 23))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/notice.pdf",
        "//127.0.0.1/notice.pdf",
        "http://www.sse.com.cn/notice.pdf",
        "https://www.sse.com.cn/notice.html",
        "https://secret@www.sse.com.cn/notice.pdf",
    ],
)
def test_non_official_document_urls_are_rejected(url):
    with pytest.raises(FundActionEvidenceError):
        SseFundCorporateActionProvider._document_url(url)


def test_pdf_failure_does_not_return_empty_actions():
    def http_get(url, **kwargs):
        if "params" in kwargs:
            return Response(index([row()]))
        raise requests.Timeout("timed out")

    provider = SseFundCorporateActionProvider(http_get=http_get)
    with pytest.raises(requests.Timeout):
        provider.fetch_actions("510300", date(2018, 3, 28), date(2026, 9, 23))


def test_events_are_filtered_by_ex_date_not_announcement_date(monkeypatch):
    def http_get(url, **kwargs):
        return Response(index([row()])) if "params" in kwargs else Response(url=url)

    provider = SseFundCorporateActionProvider(http_get=http_get)
    monkeypatch.setattr(provider, "_pdf_text", lambda _: NOTICE)
    assert (
        len(provider.fetch_actions("510300", date(2025, 6, 18), date(2025, 6, 18))) == 1
    )
    assert provider.fetch_actions("510300", date(2025, 6, 12), date(2025, 6, 17)) == []


def test_old_out_of_window_format_is_skipped_only_after_explicit_ex_date(monkeypatch):
    def http_get(url, **kwargs):
        return (
            Response(index([row(SSEDATE="2010-10-18")]))
            if "params" in kwargs
            else Response(url=url)
        )

    provider = SseFundCorporateActionProvider(http_get=http_get)
    monkeypatch.setattr(
        provider, "_pdf_text", lambda _: "旧式收益分配公告\n除息日：2010年10月22日"
    )
    assert provider.fetch_actions("510300", date(2018, 3, 28), date(2026, 9, 23)) == []
    with pytest.raises(FundActionEvidenceError, match="Fund code"):
        provider.fetch_actions("510300", date(2009, 1, 1), date(2026, 9, 23))


def test_conflicting_revision_is_rejected(monkeypatch):
    def http_get(url, **kwargs):
        if "params" in kwargs:
            return Response(index([row(), row(URL="/corrected.pdf")]))
        return Response(content=url.encode(), url=url)

    provider = SseFundCorporateActionProvider(http_get=http_get)
    monkeypatch.setattr(
        provider,
        "_pdf_text",
        lambda content: (
            NOTICE.replace("0.880", "0.990") if b"corrected" in content else NOTICE
        ),
    )
    with pytest.raises(FundActionEvidenceError, match="Conflicting"):
        provider.fetch_actions("510300", date(2018, 3, 28), date(2026, 9, 23))


def test_pdf_magic_is_checked_before_parsing():
    with pytest.raises(FundActionEvidenceError, match="PDF"):
        SseFundCorporateActionProvider._pdf_text(b"<html>blocked</html>")


@pytest.mark.parametrize(
    "title", ["基金份额拆分公告", "基金份额折算公告", "基金合并公告"]
)
def test_non_cash_action_is_not_silently_omitted(title, monkeypatch):
    def http_get(url, **kwargs):
        return (
            Response(index([row(TITLE=title)]))
            if "params" in kwargs
            else Response(url=url)
        )

    provider = SseFundCorporateActionProvider(http_get=http_get)
    monkeypatch.setattr(provider, "_pdf_text", lambda _: "折算基准日：2025年6月12日")
    with pytest.raises(FundActionEvidenceError, match="Unsupported non-cash"):
        provider.fetch_actions("510300", date(2018, 3, 28), date(2026, 9, 23))


def test_project_company_merger_is_not_a_fund_unit_action():
    title = (
        "华夏凯德商业资产封闭式基础设施证券投资基金"
        "关于不动产项目公司完成吸收合并的公告"
    )

    def http_get(url, **kwargs):
        if "params" in kwargs:
            return Response(index([row(TITLE=title, SSEDATE="2026-02-11")]))
        raise AssertionError("Asset-level merger PDF must not be classified as a fund action")

    provider = SseFundCorporateActionProvider(http_get=http_get)
    assert provider.fetch_actions("510300", date(2018, 3, 28), date(2026, 9, 23)) == []


@pytest.mark.parametrize(
    "effective_text",
    [
        "份额折算基准日：2012年5月11日",
        "自基金份额折算日（2012年5月11日）起，份额调整。",
        "确定2012年5月11日为华泰柏瑞沪深300交易型开放式指数证券投资基金的基金份额折算日。",
    ],
)
def test_older_non_cash_action_requires_explicit_out_of_window_date(
    monkeypatch, effective_text
):
    def http_get(url, **kwargs):
        return (
            Response(index([row(TITLE="基金份额折算公告", SSEDATE="2012-05-10")]))
            if "params" in kwargs
            else Response(url=url)
        )

    provider = SseFundCorporateActionProvider(http_get=http_get)
    monkeypatch.setattr(provider, "_pdf_text", lambda _: effective_text)
    assert provider.fetch_actions("510300", date(2018, 3, 28), date(2026, 9, 23)) == []
    monkeypatch.setattr(
        provider, "_pdf_text", lambda _: "份额折算公告，仅写收益测算日期"
    )
    with pytest.raises(FundActionEvidenceError, match="Unsupported non-cash"):
        provider.fetch_actions("510300", date(2018, 3, 28), date(2026, 9, 23))


def test_official_pdf_cache_is_reused_and_hash_checked(tmp_path, monkeypatch):
    calls = []

    def http_get(url, **kwargs):
        calls.append(url)
        return Response(url=url)

    provider = SseFundCorporateActionProvider(http_get=http_get, evidence_dir=tmp_path)
    monkeypatch.setattr(provider, "_pdf_text", lambda _: NOTICE)
    url = "https://www.sse.com.cn" + PDF_URL
    first = provider._document(url)
    assert provider._document(url) == first
    assert calls == [url]
    next(tmp_path.glob("*.pdf")).write_bytes(b"corrupted")
    with pytest.raises(FundActionEvidenceError, match="Corrupt"):
        provider._document(url)
    assert calls == [url]


def split_evidence(text=SPLIT_NOTICE):
    return parse_split_announcement(
        text,
        code="510500",
        disclosed_at=date(2022, 8, 23),
        source_url="https://www.sse.com.cn/split.pdf",
    )


def test_split_uses_explicit_day_end_and_post_split_session():
    evidence = split_evidence()
    assert evidence.record_date == date(2022, 8, 26)
    assert evidence.first_post_split_session == date(2022, 8, 29)
    assert evidence.published_at == date(2022, 8, 23)
    assert evidence.share_multiplier == 1.14539
    assert evidence.share_rounding == "ceil"
    verify_split_market_basis(evidence, split_prices())


@pytest.mark.parametrize(
    "before,after",
    [
        ("日终", "当日"),
        ("上进位", "舍去尾数"),
        ("（2022年8月29日）", ""),
        ("比例为1.14539", "比例另行公告"),
        ("基金代码：510500", "基金代码：510300"),
    ],
)
def test_split_cannot_infer_missing_execution_facts(before, after):
    with pytest.raises(FundActionEvidenceError):
        split_evidence(SPLIT_NOTICE.replace(before, after))


def test_split_requires_raw_price_basis_not_already_split_adjusted_prices():
    prices = split_prices()
    prices["raw_close"] = prices["qfq_close"] / 0.94
    with pytest.raises(FundActionEvidenceError, match="does not confirm"):
        verify_split_market_basis(split_evidence(), prices)


def test_split_requires_exact_disclosed_sessions_in_source():
    prices = split_prices()
    prices.loc[0, "date"] = "2022-08-25"
    with pytest.raises(FundActionEvidenceError, match="prior source session"):
        verify_split_market_basis(split_evidence(), prices)
    with pytest.raises(FundActionEvidenceError, match="independent raw/qfq"):
        verify_split_market_basis(split_evidence(), None)


def test_split_provider_requires_prior_notice_and_original_bars(monkeypatch):
    def http_get(url, **kwargs):
        if "params" in kwargs:
            return Response(
                index(
                    [
                        row(
                            SECURITY_CODE="510500",
                            SSEDATE="2022-08-29",
                            TITLE="份额拆分结果的公告",
                            URL="/result.pdf",
                        ),
                        row(
                            SECURITY_CODE="510500",
                            SSEDATE="2022-08-23",
                            TITLE="实施基金份额拆分公告",
                            URL="/prior.pdf",
                        ),
                    ]
                )
            )
        return Response(url=url)

    provider = SseFundCorporateActionProvider(http_get=http_get)
    monkeypatch.setattr(provider, "_pdf_text", lambda _: SPLIT_NOTICE)
    with pytest.raises(FundActionEvidenceError, match="independent raw/qfq"):
        provider.fetch_actions("510500", date(2018, 3, 28), date(2026, 9, 23))
    actions = provider.fetch_actions(
        "510500", date(2018, 3, 28), date(2026, 9, 23), market_prices=split_prices()
    )
    assert len(actions) == 1
    assert actions[0].ex_date == date(2022, 8, 29)
    assert actions[0].record_date == date(2022, 8, 26)
    assert actions[0].published_at == date(2022, 8, 23)
    assert actions[0].share_rounding == "ceil"


def test_split_result_alone_does_not_create_causal_prior_knowledge(monkeypatch):
    def http_get(url, **kwargs):
        return (
            Response(
                index(
                    [
                        row(
                            SECURITY_CODE="510500",
                            SSEDATE="2022-08-29",
                            TITLE="份额拆分结果的公告",
                            URL="/result.pdf",
                        )
                    ]
                )
            )
            if "params" in kwargs
            else Response(url=url)
        )

    provider = SseFundCorporateActionProvider(http_get=http_get)
    monkeypatch.setattr(provider, "_pdf_text", lambda _: SPLIT_NOTICE)
    with pytest.raises(FundActionEvidenceError, match="causal prior"):
        provider.fetch_actions(
            "510500", date(2018, 3, 28), date(2026, 9, 23), market_prices=split_prices()
        )


def test_historical_conversion_suspension_has_explicit_resume_boundary(monkeypatch):
    def http_get(url, **kwargs):
        return (
            Response(
                index(
                    [
                        row(
                            SSEDATE="2015-04-03",
                            TITLE="基金因份额折算暂停申购赎回业务公告",
                        )
                    ]
                )
            )
            if "params" in kwargs
            else Response(url=url)
        )

    provider = SseFundCorporateActionProvider(http_get=http_get)
    monkeypatch.setattr(
        provider,
        "_pdf_text",
        lambda _: "份额折算暂停申购赎回，并于2015年4月15日起恢复办理申购和赎回业务。",
    )
    assert provider.fetch_actions("510300", date(2018, 3, 28), date(2026, 9, 23)) == []
    with pytest.raises(FundActionEvidenceError, match="Unsupported non-cash"):
        provider.fetch_actions("510300", date(2014, 1, 1), date(2026, 9, 23))
