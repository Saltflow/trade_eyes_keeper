from datetime import date
from unittest.mock import patch

from src.fundamental_embedding.capco_industry import (
    parse_capco_classification_pdf,
)


class _Page:
    def __init__(self, table):
        self._table = table

    def extract_tables(self):
        return [self._table]


class _Document:
    def __init__(self, table):
        self.pages = [_Page(table)]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_capco_parser_uses_dated_fine_code_and_skips_headers():
    table = [
        ["股票代码", "股票简称", "门类", "门类名称", "大类", "大类名称", "行业代码", "行业名称"],
        ["000001", "示例银行", "J", "金融业", "JC", "资本市场服务", "66", "货币金融服务"],
        ["not-code", "", "", "", "", "", "", ""],
    ]
    content = b"capco-test"
    with patch(
        "src.fundamental_embedding.capco_industry.pdfplumber.open",
        return_value=_Document(table),
    ):
        rows = parse_capco_classification_pdf(
            content,
            period_end=date(2025, 12, 31),
            published_at=date(2026, 4, 3),
            source_url="https://example.invalid/classification.pdf",
        )

    assert len(rows) == 1
    assert rows[0].symbol == "000001"
    assert rows[0].industry_code == "J66"
    assert rows[0].industry_name == "货币金融服务"
    assert rows[0].taxonomy == "capco-listed-company-2024"
    assert rows[0].source_sha256 is not None
