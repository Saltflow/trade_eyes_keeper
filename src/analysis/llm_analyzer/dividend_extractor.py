"""
LLM分红公告解析器
从上市公司公告中提取结构化分红数据
"""

import logging
import json
import re
import hashlib
import time
import requests
from datetime import datetime
from typing import Dict, Any, Optional

from .base import BaseLLMClient

logger = logging.getLogger(__name__)


class DividendExtractor(BaseLLMClient):
    """LLM分红公告解析器，专注于从公告文本中提取结构化分红数据"""

    def extract_dividend_details_from_announcement(
        self, stock_code, title, announcement_text, content_hash=None, date=""
    ):
        """
        从公告文本中提取结构化分红数据

        Args:
            stock_code: 股票代码
            title: 公告标题
            announcement_text: 公告正文文本
            date: 公告日期（可选，用于缓存）
            content_hash: 内容哈希（用于缓存键，可选）

        Returns:
            dict: 结构化分红数据，包含以下字段（如果提取成功）：
                - cash_dividend_per_share: float (每股派息)
                - stock_dividend_ratio: float (送股比例)
                - capitalization_ratio: float (转增比例)
                - record_date: str (股权登记日, YYYY-MM-DD)
                - ex_rights_date: str (除权日, YYYY-MM-DD)
                - payment_date: str (派息日, YYYY-MM-DD)
                - total_dividend_amount: float (分红总额，单位：元)
                - confidence_score: float (置信度 0-1)
                - extraction_timestamp: str (提取时间)
                - raw_llm_response: str (原始LLM响应，用于调试)
        """

        # 如果没有内容哈希，计算文本哈希
        if content_hash is None:
            content_hash = hashlib.md5(announcement_text.encode("utf-8")).hexdigest()

        # 检查缓存
        if self.cache_manager:
            cached_extraction = self.cache_manager.get_announcement_extraction_cache(
                stock_code,
                title,
                "",
                content_hash,  # 日期参数留空，因为我们用内容哈希
            )
            if cached_extraction is not None:
                logger.info(f"使用缓存的分红提取结果: {stock_code}")
                return cached_extraction

        # 检查是否为模板占位符内容
        if "{{" in announcement_text or "}}" in announcement_text:
            logger.info(f"检测到模板占位符内容，跳过LLM调用: {stock_code}")
            return {
                "success": False,
                "cash_dividend_per_share": None,
                "stock_dividend_ratio": None,
                "capitalization_ratio": None,
                "record_date": None,
                "ex_rights_date": None,
                "payment_date": None,
                "total_dividend_amount": None,
                "confidence_score": 0.0,
                "notes": "内容为模板占位符，无法提取分红数据",
                "extraction_timestamp": datetime.now().isoformat(),
                "raw_llm_response": "",
                "attempts": 0,
                "content_hash": content_hash,
            }
        # 检查LLM调用限制
        if self._llm_calls_made >= self.max_llm_calls_per_run:
            logger.warning(
                f"已达到LLM调用限制 ({self.max_llm_calls_per_run})，跳过分红提取"
            )
            return {
                "success": False,
                "error": f"达到LLM调用限制 ({self.max_llm_calls_per_run})",
                "cash_dividend_per_share": None,
                "stock_dividend_ratio": None,
                "capitalization_ratio": None,
                "record_date": None,
                "ex_rights_date": None,
                "payment_date": None,
                "total_dividend_amount": None,
                "confidence_score": 0.0,
                "extraction_timestamp": datetime.now().isoformat(),
                "raw_llm_response": "",
            }

        # 检查LLM客户端是否可用 (使用api_key检查)
        if not hasattr(self, "api_key") or not self.api_key:
            logger.warning("LLM API密钥未配置，跳过分红提取")
            return {
                "success": False,
                "error": "LLM API密钥未配置",
                "cash_dividend_per_share": None,
                "stock_dividend_ratio": None,
                "capitalization_ratio": None,
                "record_date": None,
                "ex_rights_date": None,
                "payment_date": None,
                "total_dividend_amount": None,
                "confidence_score": 0.0,
                "extraction_timestamp": datetime.now().isoformat(),
                "raw_llm_response": "",
            }

        # 重试逻辑（最多5次）
        max_retries = 5
        retry_delay = 2  # 秒

        for attempt in range(max_retries):
            try:
                # 构建提示
                prompt = self._build_dividend_extraction_prompt(
                    stock_code, title, announcement_text
                )
                logger.info(f"公告标题: {title}")
                logger.info(
                    f"公告文本长度: {len(announcement_text)}, 前200字符: {announcement_text[:200]}..."
                )
                system_message = (
                    "你是一个专业的财务分析师，擅长从上市公司公告中提取精确的分红数据。"
                )

                # 调用LLM API
                self._llm_calls_made += 1
                logger.info(f"尝试第{attempt + 1}/{max_retries}次LLM调用提取分红数据")

                response_data = self._call_chat_completion(
                    messages=[
                        {"role": "system", "content": system_message},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,  # 低温度以获得更确定的输出
                    max_tokens=800,
                    stream=False,
                )

                # 解析响应
                llm_response = response_data["choices"][0]["message"]["content"]
                logger.info(
                    f"LLM原始响应 (长度: {len(llm_response)}): {llm_response[:500]}..."
                )
                extraction_result = self._parse_dividend_extraction_response(
                    llm_response
                )

                # 如果成功，返回结果
                if extraction_result.get("success", False):
                    # 添加元数据
                    extraction_result["raw_llm_response"] = llm_response
                    extraction_result["extraction_timestamp"] = (
                        datetime.now().isoformat()
                    )
                    extraction_result["content_hash"] = content_hash
                    extraction_result["attempts"] = attempt + 1

                    # 缓存结果
                    if self.cache_manager:
                        try:
                            self.cache_manager.set_announcement_extraction_cache(
                                stock_code, title, date, content_hash, extraction_result
                            )
                            logger.debug(f"分红提取结果已缓存: {stock_code}")
                        except Exception as cache_error:
                            logger.warning(f"缓存分红提取结果失败: {cache_error}")

                    logger.info(
                        f"分红提取完成 (第{attempt + 1}次尝试): {stock_code}, 置信度: {extraction_result.get('confidence_score', 0.0)}"
                    )
                    return extraction_result
                else:
                    # 检查是否为有效的“无分红数据”响应
                    if (
                        extraction_result.get("success") is False
                        and extraction_result.get("confidence_score", -1) >= 0
                    ):
                        # 有效响应：未找到分红数据
                        extraction_result["raw_llm_response"] = llm_response
                        extraction_result["extraction_timestamp"] = (
                            datetime.now().isoformat()
                        )
                        extraction_result["content_hash"] = content_hash
                        extraction_result["attempts"] = attempt + 1

                        # 缓存结果（包括无数据结果）
                        if self.cache_manager:
                            try:
                                self.cache_manager.set_announcement_extraction_cache(
                                    stock_code,
                                    title,
                                    date,
                                    content_hash,
                                    extraction_result,
                                )
                                logger.debug(f"无分红数据结果已缓存: {stock_code}")
                            except Exception as cache_error:
                                logger.warning(f"缓存无分红数据结果失败: {cache_error}")

                        logger.info(
                            f"无分红数据提取完成 (第{attempt + 1}次尝试): {stock_code}"
                        )
                        return extraction_result
                    else:
                        # 解析失败或无效格式
                        logger.warning(
                            f"第{attempt + 1}次尝试：LLM返回无效格式: {extraction_result.get('error', 'unknown')}"
                        )
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                else:
                    # 最后一次尝试失败
                    extraction_result["raw_llm_response"] = llm_response
                    extraction_result["extraction_timestamp"] = (
                        datetime.now().isoformat()
                    )
                    extraction_result["content_hash"] = content_hash
                    extraction_result["attempts"] = attempt + 1
                    extraction_result["error"] = (
                        f"所有{max_retries}次尝试均失败: {extraction_result.get('error', 'unknown')}"
                    )
                    return extraction_result

            except requests.exceptions.RequestException as e:
                logger.error(f"第{attempt + 1}次尝试：API请求失败: {e}")
                # 检查是否为InvalidChunkLength错误（响应过大）
                if "InvalidChunkLength" in str(e):
                    logger.error(f"检测到InvalidChunkLength错误，响应可能过大: {e}")
                    if attempt < max_retries - 1:
                        logger.info("等待后重试，可能会减少请求大小...")
                        time.sleep(retry_delay * 2)
                        continue
                    else:
                        return {
                            "success": False,
                            "error": f"API响应过大(InvalidChunkLength)，所有{max_retries}次尝试均失败: {e}",
                            "cash_dividend_per_share": None,
                            "stock_dividend_ratio": None,
                            "capitalization_ratio": None,
                            "record_date": None,
                            "ex_rights_date": None,
                            "payment_date": None,
                            "total_dividend_amount": None,
                            "confidence_score": 0.0,
                            "extraction_timestamp": datetime.now().isoformat(),
                            "raw_llm_response": "",
                            "attempts": attempt + 1,
                        }
                # 检查是否为认证错误（401）或速率限制（429）
                if hasattr(e, "response") and e.response is not None:
                    status_code = e.response.status_code
                    if status_code == 401:
                        logger.error("API密钥无效或认证失败")
                        return {
                            "success": False,
                            "error": f"API认证失败: {e}",
                            "cash_dividend_per_share": None,
                            "stock_dividend_ratio": None,
                            "capitalization_ratio": None,
                            "record_date": None,
                            "ex_rights_date": None,
                            "payment_date": None,
                            "total_dividend_amount": None,
                            "confidence_score": 0.0,
                            "extraction_timestamp": datetime.now().isoformat(),
                            "raw_llm_response": "",
                            "attempts": attempt + 1,
                        }
                    elif status_code == 429:
                        logger.error("API速率限制")
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (attempt + 2)
                            logger.info(f"等待{wait_time}秒后重试...")
                            time.sleep(wait_time)
                            continue
                        else:
                            return {
                                "success": False,
                                "error": f"API速率限制，所有{max_retries}次尝试均失败: {e}",
                                "cash_dividend_per_share": None,
                                "stock_dividend_ratio": None,
                                "capitalization_ratio": None,
                                "record_date": None,
                                "ex_rights_date": None,
                                "payment_date": None,
                                "total_dividend_amount": None,
                                "confidence_score": 0.0,
                                "extraction_timestamp": datetime.now().isoformat(),
                                "raw_llm_response": "",
                                "attempts": attempt + 1,
                            }
                # 其他网络错误可以重试
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                else:
                    return {
                        "success": False,
                        "error": f"API请求失败，所有{max_retries}次尝试均失败: {e}",
                        "cash_dividend_per_share": None,
                        "stock_dividend_ratio": None,
                        "capitalization_ratio": None,
                        "record_date": None,
                        "ex_rights_date": None,
                        "payment_date": None,
                        "total_dividend_amount": None,
                        "confidence_score": 0.0,
                        "extraction_timestamp": datetime.now().isoformat(),
                        "raw_llm_response": "",
                        "attempts": attempt + 1,
                    }
            except Exception as e:
                logger.error(f"第{attempt + 1}次尝试分红提取失败: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                else:
                    # 最后一次尝试也失败
                    return {
                        "success": False,
                        "error": f"所有{max_retries}次尝试均失败: {e}",
                        "cash_dividend_per_share": None,
                        "stock_dividend_ratio": None,
                        "capitalization_ratio": None,
                        "record_date": None,
                        "ex_rights_date": None,
                        "payment_date": None,
                        "total_dividend_amount": None,
                        "confidence_score": 0.0,
                        "extraction_timestamp": datetime.now().isoformat(),
                        "raw_llm_response": "",
                        "attempts": attempt + 1,
                    }

    def _build_dividend_extraction_prompt(self, stock_code, title, announcement_text):
        """构建分红提取提示"""
        # 限制文本长度以避免令牌超限
        max_text_length = 5000
        if len(announcement_text) > max_text_length:
            truncated_text = (
                announcement_text[:max_text_length] + "... (文本过长，已截断)"
            )
        else:
            truncated_text = announcement_text

        prompt = f"""
请从以下上市公司公告中提取精确的分红数据：

股票代码：{stock_code}
公告标题：{title}

公告正文：
{truncated_text}

请提取以下信息（如果存在）：
1. 每股派息金额（现金分红，单位：元）
2. 送股比例（每10股送X股）
3. 转增比例（每10股转增X股）
4. 股权登记日（格式：YYYY-MM-DD）
5. 除权日（格式：YYYY-MM-DD）
6. 派息日（格式：YYYY-MM-DD）
7. 分红总额（单位：元，如果公告中提及）

请以JSON格式返回，包含以下字段：
{{
    "success": true,
    "cash_dividend_per_share": 0.5,  # 每股派息（元），如果没有则为null
    "stock_dividend_ratio": 0.0,     # 送股比例（每1股送X股），例如0.5表示每10股送5股
    "capitalization_ratio": 0.0,     # 转增比例（每1股转增X股）
    "record_date": "2023-06-15",     # 股权登记日，如果没有则为null
    "ex_rights_date": "2023-06-16",  # 除权日，如果没有则为null
    "payment_date": "2023-06-20",    # 派息日，如果没有则为null
    "total_dividend_amount": 100000000.0,  # 分红总额（元），如果没有则为null
    "confidence_score": 0.85,        # 置信度（0-1）
    "notes": "额外说明"               # 任何额外说明
}}

注意：
- 只提取明确提到的数据，不要猜测
- 如果某项信息不存在，请设置为null
- 确保数值类型正确（浮点数或null）
- 日期格式必须为YYYY-MM-DD
- 置信度基于信息在文本中的明确程度
- 送股比例和转增比例按每股计算（例如：每10股送5股 = 0.5）
"""
        return prompt

    def _parse_dividend_extraction_response(self, llm_response):
        """解析LLM响应为结构化数据"""
        default_result = {
            "success": False,
            "cash_dividend_per_share": None,
            "stock_dividend_ratio": None,
            "capitalization_ratio": None,
            "record_date": None,
            "ex_rights_date": None,
            "payment_date": None,
            "total_dividend_amount": None,
            "confidence_score": 0.0,
            "notes": "",
            "error": "解析失败",
        }

        try:
            # 尝试从响应中提取JSON
            json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
            if not json_match:
                default_result["error"] = "未找到JSON格式响应"
                return default_result

            json_str = json_match.group()
            data = json.loads(json_str)

            # 验证必要字段
            if "success" not in data:
                default_result["error"] = "响应缺少success字段"
                return default_result

            if not data["success"]:
                # LLM determined no dividend data
                result = {
                    "success": False,
                    "cash_dividend_per_share": None,
                    "stock_dividend_ratio": None,
                    "capitalization_ratio": None,
                    "record_date": None,
                    "ex_rights_date": None,
                    "payment_date": None,
                    "total_dividend_amount": None,
                    "confidence_score": float(data.get("confidence_score", 0.0)),
                    "notes": str(data.get("notes", "")),
                    "error": data.get("error", "LLM返回失败"),
                }
                return result

            # 提取字段并验证类型
            result = {
                "success": True,
                "cash_dividend_per_share": self._parse_float_or_null(
                    data.get("cash_dividend_per_share")
                ),
                "stock_dividend_ratio": self._parse_float_or_null(
                    data.get("stock_dividend_ratio")
                ),
                "capitalization_ratio": self._parse_float_or_null(
                    data.get("capitalization_ratio")
                ),
                "record_date": self._parse_date_or_null(data.get("record_date")),
                "ex_rights_date": self._parse_date_or_null(data.get("ex_rights_date")),
                "payment_date": self._parse_date_or_null(data.get("payment_date")),
                "total_dividend_amount": self._parse_float_or_null(
                    data.get("total_dividend_amount")
                ),
                "confidence_score": min(
                    max(float(data.get("confidence_score", 0.0)), 0.0), 1.0
                ),
                "notes": str(data.get("notes", "")),
                "error": None,
            }

            # 验证置信度
            if result["confidence_score"] < 0.1:
                logger.warning(f"置信度过低: {result['confidence_score']}")

            return result

        except json.JSONDecodeError as e:
            default_result["error"] = f"JSON解析错误: {e}"
            logger.error(f"JSON解析错误: {e}, 响应: {llm_response[:200]}...")
            return default_result
        except Exception as e:
            default_result["error"] = f"解析错误: {e}"
            logger.error(f"解析错误: {e}")
            return default_result
