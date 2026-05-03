"""
LLM分析模块基础类
提供共享的API调用和配置功能
"""

import logging
import json
import hashlib
import time
from datetime import datetime
import sys
import io
import re
import requests
from typing import Optional, Dict, Any, List
from ...data.cache_manager import CacheManager

# 猴子补丁：确保JSON序列化使用UTF-8编码，不转义非ASCII字符
_original_json_dumps = json.dumps


def _patched_json_dumps(obj, *args, **kwargs):
    """确保所有JSON序列化使用UTF-8编码"""
    kwargs["ensure_ascii"] = False
    return _original_json_dumps(obj, *args, **kwargs)


json.dumps = _patched_json_dumps

_original_json_dump = json.dump


def _patched_json_dump(obj, fp, *args, **kwargs):
    """确保所有JSON序列化使用UTF-8编码"""
    kwargs["ensure_ascii"] = False
    return _original_json_dump(obj, fp, *args, **kwargs)


json.dump = _patched_json_dump

# 设置UTF-8编码，避免中文字符编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

logger = logging.getLogger(__name__)


class BaseLLMClient:
    """LLM基础客户端，提供共享的API调用和配置功能"""

    def __init__(self, config):
        """
        初始化LLM基础客户端

        Args:
            config: 配置字典
        """
        self.config = config
        self.llm_config = config.get("llm", {})
        # 初始化缓存管理器
        self.cache_manager = CacheManager(config)
        # LLM调用计数器（用于限制每轮调用次数）
        self._llm_calls_made = 0
        # LLM调用上限：公告/基础分析与财报分析可能不同，取较大值
        self.max_llm_calls_per_run = max(
            config.get("announcements", {}).get("max_llm_calls_per_run", 5),
            config.get("financial_reports", {}).get("max_llm_calls_per_run", 60),
        )

        # 初始化DeepSeek API配置（使用requests直接调用，避免httpx/OpenAI库问题）
        api_key = self.llm_config.get("api_key", "")
        base_url = self.llm_config.get("base_url", "")
        model = self.llm_config.get("model", "")

        if not api_key:
            logger.warning("LLM API密钥未配置，LLM分析功能不可用")
        else:
            # 使用requests库直接调用API，避免httpx/OpenAI库的SSL超时问题
            self.api_key = api_key
            self.base_url = base_url
            self.model = model
            logger.info("LLM API配置完成，将使用requests直接调用")

    def _call_chat_completion(
        self, messages, temperature=0.7, max_tokens=2000, stream=False
    ):
        """
        使用requests直接调用DeepSeek API

        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            stream: 是否使用流式响应

        Returns:
            dict: API响应
        """
        if not hasattr(self, "api_key") or not self.api_key:
            raise ValueError("API key not configured")

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

        try:
            # 设置更长的超时时间和合理的读取设置
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=300.0,  # 5分钟超时
                verify=True,  # SSL验证
                stream=stream,  # 如果stream=True，需要流式处理
            )
            response.raise_for_status()

            if stream:
                # 流式响应处理
                content_chunks = []
                for chunk in response.iter_lines():
                    if chunk:
                        chunk_str = chunk.decode("utf-8")
                        if chunk_str.startswith("data: "):
                            chunk_data = chunk_str[6:]
                            if chunk_data == "[DONE]":
                                break
                            try:
                                chunk_json = json.loads(chunk_data)
                                if (
                                    "choices" in chunk_json
                                    and len(chunk_json["choices"]) > 0
                                ):
                                    if (
                                        "delta" in chunk_json["choices"][0]
                                        and "content"
                                        in chunk_json["choices"][0]["delta"]
                                    ):
                                        content_chunks.append(
                                            chunk_json["choices"][0]["delta"]["content"]
                                        )
                            except json.JSONDecodeError:
                                logger.warning(f"无法解析流式响应块: {chunk_data}")
                content = "".join(content_chunks)
                return {"choices": [{"message": {"content": content}}]}
            else:
                # 非流式响应
                return response.json()

        except requests.exceptions.ChunkedEncodingError as e:
            logger.error(f"流式响应分块编码错误: {e}")
            # 检查是否为InvalidChunkLength错误
            if "InvalidChunkLength" in str(e):
                logger.error("检测到InvalidChunkLength错误，可能是响应过大")
                # 尝试减少max_tokens并重试
                reduced_max_tokens = max(500, max_tokens // 2)
                logger.info(f"尝试减少max_tokens到{reduced_max_tokens}并重试...")
                return self._call_chat_completion(
                    messages, temperature, reduced_max_tokens, stream=False
                )
            # 尝试非流式模式重试
            if stream:
                logger.info("尝试使用非流式模式重试...")
                return self._call_chat_completion(
                    messages, temperature, max_tokens, stream=False
                )
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"API请求失败: {e}")
            # 检查是否为InvalidChunkLength错误（可能隐藏在内部异常中）
            if "InvalidChunkLength" in str(e):
                logger.error("检测到InvalidChunkLength错误，尝试减少max_tokens重试")
                reduced_max_tokens = max(500, max_tokens // 2)
                logger.info(f"尝试减少max_tokens到{reduced_max_tokens}并重试...")
                return self._call_chat_completion(
                    messages, temperature, reduced_max_tokens, stream=False
                )

            if hasattr(e, "response") and e.response is not None:
                logger.error(
                    f"响应状态: {e.response.status_code}, 内容: {e.response.text[:500]}"
                )
            raise

    def _increment_llm_calls(self):
        """增加LLM调用计数"""
        self._llm_calls_made += 1

    def _can_make_llm_call(self):
        """检查是否可以进行LLM调用"""
        if not hasattr(self, "api_key") or not self.api_key:
            return False
        if self._llm_calls_made >= self.max_llm_calls_per_run:
            logger.warning(f"已达到LLM调用限制 ({self.max_llm_calls_per_run})")
            return False
        return True

    @staticmethod
    def _parse_float_or_null(value):
        """解析浮点数或返回null"""
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_date_or_null(value):
        """解析日期字符串或返回null"""
        if value is None:
            return None
        try:
            # 尝试常见日期格式
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
                try:
                    dt = datetime.strptime(str(value), fmt)
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue
            return None
        except Exception:
            return None

    @staticmethod
    def _calculate_data_hash(data_row):
        """
        计算股票数据行的哈希值，用于验证缓存数据新鲜度

        Args:
            data_row: 股票数据字典

        Returns:
            str: 数据哈希值
        """
        import hashlib
        import json

        if not data_row:
            return ""

        # 选择关键字段进行计算（这些字段变化会影响分析结果）
        key_fields = [
            "close",  # 收盘价
            "dividend_per_share",  # 每股分红
            "dividend_yield",  # 股息率
            "pe_ratio",  # 市盈率
            "pb_ratio",  # 市净率
            "roe",  # 净资产收益率
            "debt_ratio",  # 负债率
            "earnings_growth",  # 业绩增长
            "ma60",  # 60日移动平均线
        ]

        # 提取关键字段的值
        hash_data = {}
        for field in key_fields:
            if field in data_row:
                value = data_row[field]
                # 处理None值
                if value is None:
                    hash_data[field] = "null"
                elif isinstance(value, float):
                    # 浮点数保留3位小数以确保稳定性
                    hash_data[field] = f"{value:.3f}"
                else:
                    hash_data[field] = str(value)

        # 计算哈希
        if not hash_data:
            return ""

        json_str = json.dumps(hash_data, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(json_str.encode("utf-8")).hexdigest()
