"""
LLM分析器主类（精简版）
仅保留股息提取功能，删除基本面/财报分析片汤话
"""

import logging
from .base import BaseLLMClient
from .dividend_extractor import DividendExtractor

logger = logging.getLogger(__name__)


class LLMAnalyzer(BaseLLMClient):
    """
    LLM股票分析器（精简版）
    仅保留分红公告解析功能
    """

    def __init__(self, config):
        super().__init__(config)
        self.dividend_extractor = DividendExtractor(config)
        logger.info("LLM分析器初始化完成（仅股息提取）")

    def extract_dividend_details_from_announcement(
        self, stock_code, title, announcement_text, content_hash=None, date=""
    ):
        """
        从公告文本中提取结构化分红数据
        委托给DividendExtractor组件
        """
        self.dividend_extractor._llm_calls_made = self._llm_calls_made
        self.dividend_extractor.max_llm_calls_per_run = self.max_llm_calls_per_run

        result = self.dividend_extractor.extract_dividend_details_from_announcement(
            stock_code, title, announcement_text, content_hash, date
        )

        self._llm_calls_made = self.dividend_extractor._llm_calls_made
        return result
