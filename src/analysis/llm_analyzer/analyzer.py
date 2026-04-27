"""
LLM分析器主类（兼容性层）
提供与原始LLMAnalyzer相同的接口，内部使用拆分的分析器组件
"""

import logging
from typing import Dict, Any, List, Optional

from .base import BaseLLMClient
from .fundamental_analyzer import FundamentalAnalyzer
from .dividend_extractor import DividendExtractor
from .financial_report_analyzer import FinancialReportAnalyzer

logger = logging.getLogger(__name__)


class LLMAnalyzer(BaseLLMClient):
    """
    LLM股票分析器（兼容性类）
    提供基本面分析和分红公告解析功能，内部委托给专用组件
    """

    def __init__(self, config):
        """
        初始化LLM分析器

        Args:
            config: 配置字典
        """
        super().__init__(config)

        # 读取基本面分析配置开关
        self.enable_fundamental_analysis = self.llm_config.get(
            "enable_fundamental_analysis", True
        )
        logger.info(f"LLM基本面分析开关状态: {self.enable_fundamental_analysis}")

        # 初始化专用组件
        self.fundamental_analyzer = FundamentalAnalyzer(config)
        self.dividend_extractor = DividendExtractor(config)
        self.financial_report_analyzer = FinancialReportAnalyzer(config)

        # 共享LLM调用计数器
        # 注意：两个组件共享同一个计数器（通过继承自BaseLLMClient）
        # 但为了确保一致性，我们使用父类的计数器

        logger.info("LLM分析器初始化完成，使用拆分的组件架构")

    def analyze_stocks(self, stock_codes, stock_data=None):
        """
        分析股票基本面，重点关注分红可持续性和股价稳定性
        委托给FundamentalAnalyzer组件

        Args:
            stock_codes: 股票代码列表
            stock_data: 可选的股票数据字典（映射股票代码到最新数据行），
                        包含真实的分红和财务数据

        Returns:
            dict: 分析结果，键为股票代码，值为分析结果字典
        """
        # 检查基本面分析是否启用
        if not self.enable_fundamental_analysis:
            logger.info("LLM基本面分析已禁用（在analyzer内部检查），返回空结果")
            return {}

        # 确保使用相同的LLM调用计数器
        self.fundamental_analyzer._llm_calls_made = self._llm_calls_made
        self.fundamental_analyzer.max_llm_calls_per_run = self.max_llm_calls_per_run

        result = self.fundamental_analyzer.analyze_stocks(stock_codes, stock_data)

        # 同步计数器状态
        self._llm_calls_made = self.fundamental_analyzer._llm_calls_made

        return result

    def extract_dividend_details_from_announcement(
        self, stock_code, title, announcement_text, content_hash=None, date=""
    ):
        """
        从公告文本中提取结构化分红数据
        委托给DividendExtractor组件

        Args:
            stock_code: 股票代码
            title: 公告标题
            announcement_text: 公告正文文本
            date: 公告日期（可选，用于缓存）
            content_hash: 内容哈希（用于缓存键，可选）

        Returns:
            dict: 结构化分红数据
        """
        # 确保使用相同的LLM调用计数器
        self.dividend_extractor._llm_calls_made = self._llm_calls_made
        self.dividend_extractor.max_llm_calls_per_run = self.max_llm_calls_per_run

        result = self.dividend_extractor.extract_dividend_details_from_announcement(
            stock_code, title, announcement_text, content_hash, date
        )

        # 同步计数器状态
        self._llm_calls_made = self.dividend_extractor._llm_calls_made

        return result

    def analyze_financial_report(
        self, stock_code, report_text, report_type, period_date, report_title=""
    ):
        """
        分析财务报告文本，提取关键财务数据并进行多维度分析
        委托给FinancialReportAnalyzer组件

        Args:
            stock_code: 股票代码
            report_text: 财报正文文本（PDF/HTML提取内容）
            report_type: 报告类型 ('annual', 'semiannual', 'quarterly')
            period_date: 报告期间（如 '2024-12-31'）
            report_title: 报告标题（可选）

        Returns:
            dict: 分析结果，包含提取的财务数据和多维分析
        """
        # 确保使用相同的LLM调用计数器
        self.financial_report_analyzer._llm_calls_made = self._llm_calls_made
        self.financial_report_analyzer.max_llm_calls_per_run = (
            self.max_llm_calls_per_run
        )

        result = self.financial_report_analyzer.analyze_financial_report(
            stock_code, report_text, report_type, period_date, report_title
        )

        # 同步计数器状态
        self._llm_calls_made = self.financial_report_analyzer._llm_calls_made

        return result

    # 保留原始LLMAnalyzer的其他方法（如果有）
    # 这些方法现在委托给fundamental_analyzer

    def _get_stock_info(self, stock_code, stock_data=None):
        """获取股票信息（委托给fundamental_analyzer）"""
        return self.fundamental_analyzer._get_stock_info(stock_code, stock_data)

    def _call_llm_analysis(self, stock_code, stock_info):
        """调用LLM分析（委托给fundamental_analyzer）"""
        return self.fundamental_analyzer._call_llm_analysis(stock_code, stock_info)

    def _build_analysis_prompt(self, stock_code, stock_info):
        """构建分析提示（委托给fundamental_analyzer）"""
        return self.fundamental_analyzer._build_analysis_prompt(stock_code, stock_info)

    def _parse_structured_analysis_response(self, analysis_text):
        """解析结构化分析响应（委托给fundamental_analyzer）"""
        return self.fundamental_analyzer._parse_structured_analysis_response(
            analysis_text
        )

    def _extract_summary_from_structured(self, structured_result, analysis_text):
        """从结构化结果提取摘要（委托给fundamental_analyzer）"""
        return self.fundamental_analyzer._extract_summary_from_structured(
            structured_result, analysis_text
        )

    def _extract_summary(self, analysis_text):
        """提取摘要（委托给fundamental_analyzer）"""
        return self.fundamental_analyzer._extract_summary(analysis_text)

    # DividendExtractor的方法（如果需要直接访问）
    def _build_dividend_extraction_prompt(self, stock_code, title, announcement_text):
        """构建分红提取提示（委托给dividend_extractor）"""
        return self.dividend_extractor._build_dividend_extraction_prompt(
            stock_code, title, announcement_text
        )

    def _parse_dividend_extraction_response(self, llm_response):
        """解析分红提取响应（委托给dividend_extractor）"""
        return self.dividend_extractor._parse_dividend_extraction_response(llm_response)
