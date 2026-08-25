from datetime import date

from src.fundamental_embedding.csrc_industry import parse_csrc_classification_text


def test_parser_carries_industry_across_pdf_continuation_rows():
    text = """
2020年4季度上市公司行业分类结果 门类名称及代码 行业大类代码 行业大类名称 上市公司代码 上市公司简称
农、林、牧、渔业 01 农业 000998 隆平高科
(A) 002041 登海种业
02 林业 000592 平潭发展
采矿业(B) 06 煤炭开采和洗选业 000552 靖远煤电
000780 *ST平能
制造业(C) 38 电气机械和器材制造业 000333 美的集团
"""
    rows = parse_csrc_classification_text(
        text,
        period_end=date(2020, 12, 31),
        published_at=date(2021, 1, 25),
        source_url="https://www.csrc.gov.cn/example.pdf",
        source_content=b"official-document",
    )
    by_symbol = {item.symbol: item for item in rows}

    assert by_symbol["000998"].industry_code == "A01"
    assert by_symbol["002041"].industry_code == "A01"
    assert by_symbol["000592"].industry_code == "A02"
    assert by_symbol["000552"].industry_code == "B06"
    assert by_symbol["000780"].industry_code == "B06"
    assert by_symbol["000333"].industry_code == "C38"
    assert by_symbol["000333"].published_at == date(2021, 1, 25)
    assert by_symbol["000333"].source_sha256 is not None
