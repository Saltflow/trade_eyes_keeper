"""
LLM分析模块
提供股票基本面分析和分红公告解析功能

模块结构：
- base: BaseLLMClient基类
- fundamental_analyzer: 基本面分析器
- dividend_extractor: 分红公告解析器
- analyzer: 兼容性主类LLMAnalyzer
"""

from .analyzer import LLMAnalyzer
from .fundamental_analyzer import FundamentalAnalyzer
from .dividend_extractor import DividendExtractor
from .base import BaseLLMClient

__all__ = [
    "LLMAnalyzer",
    "FundamentalAnalyzer",
    "DividendExtractor",
    "BaseLLMClient",
]

__version__ = "2.0.0"
