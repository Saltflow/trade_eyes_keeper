"""
LLM分析模块（精简版）
仅保留分红公告解析功能

模块结构：
- base: BaseLLMClient基类
- dividend_extractor: 分红公告解析器
- analyzer: 兼容性主类LLMAnalyzer
"""

from .analyzer import LLMAnalyzer
from .dividend_extractor import DividendExtractor
from .base import BaseLLMClient

__all__ = [
    "LLMAnalyzer",
    "DividendExtractor",
    "BaseLLMClient",
]

__version__ = "3.0.0"
