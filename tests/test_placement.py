"""定增(定向增发)功能测试。

覆盖:
1. web_crawler._parse_placement_row 解析 + 占比 + 解禁日期计算
2. _parse_lockin_years 锁定期文本解析
3. email_notifier._build_placement_section 展示表格
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestLockinParse:
    """锁定期文本解析为年数。"""

    def test_years(self):
        from data.web_crawler import StockWebCrawler
        assert StockWebCrawler._parse_lockin_years("3年") == 3.0

    def test_months(self):
        from data.web_crawler import StockWebCrawler
        assert StockWebCrawler._parse_lockin_years("18个月") == 1.5
        assert StockWebCrawler._parse_lockin_years("6个月") == 0.5

    def test_none(self):
        from data.web_crawler import StockWebCrawler
        assert StockWebCrawler._parse_lockin_years(None) is None
        assert StockWebCrawler._parse_lockin_years("") is None


class TestParsePlacementRow:
    """解析东方财富定增行。"""

    def _crawler(self):
        from data.web_crawler import StockWebCrawler
        return StockWebCrawler({})

    def test_parse_full_row(self):
        c = self._crawler()
        row = {
            "ISSUE_NUM": 457665903.0,
            "ISSUE_PRICE": 43.7,
            "ISSUE_SHARE_AFTER": 21689434304.0,
            "ISSUE_LISTING_DATE": "2026-04-08 00:00:00",
            "LOCKIN_PERIOD": "3年",
            "ISSUE_OBJECT": "太平资产等",
        }
        r = c._parse_placement_row(row)
        assert r["issue_num"] == 457665903.0
        assert r["issue_price"] == 43.7
        # 占比 = 457665903 / 21689434304 * 100 ≈ 2.11%
        assert abs(r["pct_of_total"] - 2.11) < 0.01
        assert r["listing_date"] == "2026-04-08"
        # 上市 2026-04-08 + 3年 = 2029-04-08
        assert r["unlock_date"] == "2029-04-08"
        assert r["is_locked"] is True  # 2029 未到

    def test_pct_none_when_no_total(self):
        c = self._crawler()
        row = {"ISSUE_NUM": 1000, "ISSUE_PRICE": 10.0, "ISSUE_SHARE_AFTER": None}
        r = c._parse_placement_row(row)
        assert r["pct_of_total"] is None

    def test_half_year_lockin(self):
        c = self._crawler()
        row = {
            "ISSUE_NUM": 1000, "ISSUE_PRICE": 10.0,
            "ISSUE_SHARE_AFTER": 100000,
            "ISSUE_LISTING_DATE": "2020-01-01 00:00:00",
            "LOCKIN_PERIOD": "6个月",
        }
        r = c._parse_placement_row(row)
        # 2020-01-01 + 0.5年(182天) ≈ 2020-07-01, 已解禁
        assert r["is_locked"] is False
        assert r["unlock_date"] is not None


class TestPlacementSection:
    """邮件定增表展示。"""

    def _notifier(self):
        tmpdir = tempfile.mkdtemp()
        config = {"email": {"smtp_server": "localhost", "smtp_port": 465,
                            "sender_email": "t@t.com", "sender_password": "x",
                            "receiver_email": "t@t.com", "archive_dir": tmpdir}}
        from notification.email_notifier import EmailNotifier
        n = EmailNotifier(config)
        n._get_server_info = MagicMock(return_value={
            "hostname": "h", "ip_address": "127.0.0.1"})
        return n

    def test_empty_placements_returns_blank(self):
        n = self._notifier()
        assert n._build_placement_section(None, None) == ""
        assert n._build_placement_section({}, None) == ""

    def test_placement_table_content(self):
        n = self._notifier()
        placements = {
            "601088": {
                "issue_num": 457665903.0,
                "issue_price": 43.7,
                "pct_of_total": 2.11,
                "unlock_date": "2029-04-08",
                "is_locked": True,
            }
        }
        stock_data = pd.DataFrame([
            {"stock_code": "601088", "stock_name": "中国神华"}
        ])
        html = n._build_placement_section(placements, stock_data)
        assert "未解禁定增" in html
        assert "601088" in html
        assert "中国神华" in html
        assert "4.58亿股" in html   # 457665903 / 1e8
        assert "2.11%" in html
        assert "43.70元" in html
        assert "2029-04-08" in html

    def test_placement_section_in_email_body(self):
        n = self._notifier()
        placements = {
            "601088": {
                "issue_num": 457665903.0, "issue_price": 43.7,
                "pct_of_total": 2.11, "unlock_date": "2029-04-08",
                "is_locked": True,
            }
        }
        stock_data = pd.DataFrame([
            {"stock_code": "601088", "stock_name": "中国神华",
             "open": 42.0, "close": 42.29, "high": 42.5, "low": 41.8,
             "ma60": 43.0, "dividend_per_share": 1.03, "dividend_yield": 2.44,
             "pe_ratio": 8.0, "pb_ratio": 1.2, "roe": 15.0},
        ])
        html = n._build_email_body(
            alert_stocks=[], stock_data=stock_data,
            placements=placements, daily_mode=True,
        )
        assert "未解禁定增" in html
        assert "4.58亿股" in html
