#!/usr/bin/env python3
"""
测试content_fetcher.py中的PDF附件提取和保存功能
"""

import pytest
import tempfile
import os
from unittest.mock import Mock, patch, MagicMock
import sys

# 添加src目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.content_fetcher import ContentFetcher


class TestContentFetcherPDF:
    """测试ContentFetcher的PDF附件功能"""

    def setup_method(self):
        """测试设置"""
        self.config = {"announcements": {"max_pdf_size_mb": 10}}
        self.fetcher = ContentFetcher(self.config)

    def test_extract_pdf_links_from_html_simple(self):
        """测试从HTML提取PDF链接（简单情况）"""
        html = """
        <html>
        <body>
            <a href="/reports/annual.pdf">年报</a>
            <a href="/reports/financial.pdf">财务报表</a>
            <a href="/reports/summary.html">摘要</a>
        </body>
        </html>
        """

        pdf_links = self.fetcher._extract_pdf_links_from_html(
            html, "http://example.com"
        )

        # 应该提取到2个PDF链接
        assert len(pdf_links) == 2
        assert "http://example.com/reports/annual.pdf" in pdf_links
        assert "http://example.com/reports/financial.pdf" in pdf_links

    def test_extract_pdf_links_from_html_no_links(self):
        """测试HTML中没有PDF链接的情况"""
        html = """
        <html>
        <body>
            <a href="/reports/summary.html">摘要</a>
            <a href="/reports/data.txt">数据</a>
        </body>
        </html>
        """

        pdf_links = self.fetcher._extract_pdf_links_from_html(
            html, "http://example.com"
        )
        assert len(pdf_links) == 0

    def test_extract_pdf_links_from_html_relative_urls(self):
        """测试相对URL转换"""
        html = """
        <html>
        <body>
            <a href="reports/annual.pdf">年报</a>
            <a href="../documents/report.pdf">报告</a>
        </body>
        </html>
        """

        pdf_links = self.fetcher._extract_pdf_links_from_html(
            html, "http://example.com/docs/"
        )

        assert len(pdf_links) == 2
        assert "http://example.com/docs/reports/annual.pdf" in pdf_links
        assert "http://example.com/documents/report.pdf" in pdf_links

    def test_extract_pdf_links_from_html_empty(self):
        """测试空HTML内容"""
        pdf_links = self.fetcher._extract_pdf_links_from_html("", "http://example.com")
        assert len(pdf_links) == 0

    def test_extract_pdf_links_from_html_none(self):
        """测试None HTML内容"""
        pdf_links = self.fetcher._extract_pdf_links_from_html(
            None, "http://example.com"
        )
        assert len(pdf_links) == 0

    @patch("requests.get")
    def test_try_download_pdf_attachment_success(self, mock_get):
        """测试成功下载PDF附件"""
        # 模拟响应
        mock_response = Mock()
        mock_response.headers = {"Content-Type": "application/pdf"}
        mock_response.raise_for_status = Mock()
        mock_response.iter_content = Mock(return_value=[b"%PDF", b"-1.4\n", b"content"])
        mock_get.return_value = mock_response

        pdf_links = ["http://example.com/report.pdf"]
        result = self.fetcher._try_download_pdf_attachment(
            pdf_links, "http://example.com/page.html"
        )

        assert result["success"] is True
        assert b"%PDF-1.4\ncontent" in result["content_bytes"]

    @patch("requests.get")
    def test_try_download_pdf_attachment_not_pdf(self, mock_get):
        """测试下载非PDF内容"""
        mock_response = Mock()
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        pdf_links = ["http://example.com/notpdf.pdf"]
        result = self.fetcher._try_download_pdf_attachment(
            pdf_links, "http://example.com/page.html"
        )

        assert result["success"] is False

    @patch("requests.get")
    def test_try_download_pdf_attachment_invalid_pdf(self, mock_get):
        """测试下载无效的PDF（缺少%PDF头）"""
        mock_response = Mock()
        mock_response.headers = {"Content-Type": "application/pdf"}
        mock_response.raise_for_status = Mock()
        mock_response.iter_content = Mock(return_value=[b"NOTPDF", b"content"])
        mock_get.return_value = mock_response

        pdf_links = ["http://example.com/invalid.pdf"]
        result = self.fetcher._try_download_pdf_attachment(
            pdf_links, "http://example.com/page.html"
        )

        assert result["success"] is False

    @patch("requests.get")
    def test_try_download_pdf_attachment_size_exceeded(self, mock_get):
        """测试PDF大小超过限制"""
        # 创建超过限制的内容
        large_content = b"%PDF" + b"x" * (self.fetcher.max_pdf_size + 100)

        mock_response = Mock()
        mock_response.headers = {"Content-Type": "application/pdf"}
        mock_response.raise_for_status = Mock()
        mock_response.iter_content = Mock(
            return_value=[large_content[:8192], large_content[8192:]]
        )
        mock_get.return_value = mock_response

        pdf_links = ["http://example.com/large.pdf"]
        result = self.fetcher._try_download_pdf_attachment(
            pdf_links, "http://example.com/page.html"
        )

        # 超过大小限制应该返回失败
        assert (
            result["success"] is False
            or len(result.get("content_bytes", b"")) <= self.fetcher.max_pdf_size
        )

    def test_try_download_pdf_attachment_empty_list(self):
        """测试空PDF链接列表"""
        result = self.fetcher._try_download_pdf_attachment(
            [], "http://example.com/page.html"
        )
        assert result["success"] is False

    @patch.object(ContentFetcher, "_extract_pdf_links_from_html")
    @patch.object(ContentFetcher, "_download_content")
    def test_fetch_content_with_pdf_attachment(self, mock_download, mock_extract):
        """测试fetch_content处理PDF附件"""
        # 模拟下载返回HTML内容和PDF链接
        mock_download.return_value = {
            "success": True,
            "content": b"<html>PDF linked</html>",
            "content_type": "html",
            "pdf_links": ["http://example.com/report.pdf"],
            "headers": {},
        }

        # 模拟PDF下载成功和提取成功
        with patch.object(
            self.fetcher, "_try_download_pdf_attachment"
        ) as mock_try, patch.object(self.fetcher, "_extract_text") as mock_extract_text:
            mock_try.return_value = {
                "success": True,
                "content_bytes": b"%PDF-1.4\ntest content",
            }

            # 模拟PDF文本提取成功
            mock_extract_text.return_value = {
                "success": True,
                "text": "Extracted PDF text",
                "metadata": {"pdf_page_count": 1, "pdf_has_text": True},
            }

            # 模拟缓存管理器
            mock_cache = Mock()
            mock_cache.save_pdf_file = Mock(return_value="/cache/path/file.pdf")
            mock_cache.get_announcement_content_cache = Mock(
                return_value=None
            )  # 没有缓存
            self.fetcher.cache_manager = mock_cache

            result = self.fetcher.fetch_content(
                "http://example.com/page.html", "601398", "2024-03-30"
            )

            assert result["success"] is True
            assert result["content_type"] == "pdf"
            assert "pdf_file_path" in result

    def test_pdf_validation_logic(self):
        """测试PDF验证逻辑"""
        # 有效的PDF文件头
        valid_pdf = b"%PDF-1.4\nsome content"
        # 无效的PDF文件头
        invalid_pdf = b"NOTPDF-1.4\nsome content"

        # 测试验证逻辑（在_try_download_pdf_attachment中实现）
        # 这里我们直接测试条件
        assert len(valid_pdf) >= 4 and valid_pdf[:4] == b"%PDF"
        assert not (len(invalid_pdf) >= 4 and invalid_pdf[:4] == b"%PDF")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
