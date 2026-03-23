"""
LLM分析模块重构
使用DeepSeek API分析股票基本面和提取分红数据
重构为三个类：BaseLLMClient, LLMAnalyzer, DividendExtractor
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
from .cache_manager import CacheManager

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
        self.max_llm_calls_per_run = config.get("announcements", {}).get(
            "max_llm_calls_per_run", 5
        )

        # 初始化DeepSeek API配置（使用requests直接调用，避免httpx/OpenAI库问题）
        api_key = self.llm_config.get("api_key", "")
        base_url = self.llm_config.get("base_url", "https://api.deepseek.com/v1")
        model = self.llm_config.get("model", "deepseek-chat")

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


class LLMAnalyzer(BaseLLMClient):
    """LLM股票分析器，专注于分红可持续性和股价稳定性分析"""

    def analyze_stocks(self, stock_codes, stock_data=None):
        """
        分析股票基本面，重点关注分红可持续性和股价稳定性

        Args:
            stock_codes: 股票代码列表
            stock_data: 可选的股票数据字典（映射股票代码到最新数据行），
                        包含真实的分红和财务数据

        Returns:
            dict: 分析结果，键为股票代码，值为分析结果字典
        """
        if not hasattr(self, "api_key") or not self.api_key:
            logger.warning("LLM API密钥未配置，跳过分析")
            return {}

        analysis_results = {}

        for stock_code in stock_codes:
            try:
                # 首先尝试从缓存获取分析结果
                cached_analysis = self.cache_manager.get_analysis_cache(stock_code)
                if cached_analysis and "analysis" in cached_analysis:
                    logger.info(f"股票 {stock_code} 使用缓存分析结果")
                    analysis_results[stock_code] = cached_analysis["analysis"]
                    continue

                logger.info(f"开始分析股票 {stock_code} 的基本面")

                # 获取股票信息（优先使用传入的真实数据）
                stock_info = self._get_stock_info(stock_code, stock_data)

                if not stock_info:
                    logger.warning(f"无法获取股票 {stock_code} 的信息")
                    continue

                # 调用LLM进行分析
                analysis_result = self._call_llm_analysis(stock_code, stock_info)

                if analysis_result:
                    analysis_results[stock_code] = analysis_result
                    logger.info(f"股票 {stock_code} 分析完成")
                    # 缓存分析结果
                    try:
                        self.cache_manager.set_analysis_cache(
                            stock_code, analysis_result
                        )
                        logger.debug(f"股票 {stock_code} 分析结果已缓存")
                    except Exception as cache_error:
                        logger.warning(
                            f"缓存股票 {stock_code} 分析结果失败: {cache_error}"
                        )
                else:
                    logger.warning(f"股票 {stock_code} 分析失败")

            except Exception as e:
                logger.error(f"分析股票 {stock_code} 时发生错误: {e}")

        return analysis_results

    def _get_stock_info(self, stock_code, stock_data=None):
        """
        获取股票基本信息，优先使用真实数据

        Args:
            stock_code: 股票代码
            stock_data: 可选的股票数据字典（映射股票代码到最新数据行）

        Returns:
            dict: 股票基础信息
        """
        # 基础信息模板
        stock_info = {
            "name": f"股票{stock_code}",
            "industry": "",
            "market_cap": "",
            "pe_ratio": "",
            "pb_ratio": "",
            "dividend_yield": "",
            "last_dividend_date": "",
            "last_dividend_amount": "",
            # 新增字段用于分红可持续性和股价稳定性分析
            "dividend_per_share": "",
            "roe": "",
            "debt_ratio": "",
            "earnings_growth": "",
            "current_price": "",
            "ma60": "",
            "price_volatility": "",
        }

        # 如果提供了真实数据，优先使用真实数据
        if stock_data and stock_code in stock_data:
            data_row = stock_data[stock_code]

            # 映射字段：target_field (stock_info中的字段) -> source_field (data_row中的字段)
            field_mapping = {
                "dividend_per_share": "dividend_per_share",
                "dividend_yield": "dividend_yield",
                "pe_ratio": "pe_ratio",
                "pb_ratio": "pb_ratio",
                "roe": "roe",
                "debt_ratio": "debt_ratio",
                "earnings_growth": "earnings_growth",
                "current_price": "close",  # 修正：stock_info["current_price"] = data_row["close"]
                "ma60": "ma60",
            }

            for target_field, source_field in field_mapping.items():
                if source_field in data_row and data_row[source_field] is not None:
                    stock_info[target_field] = data_row[source_field]

            # 尝试获取分红历史（通过web_crawler或缓存）
            try:
                # 这里可以扩展为获取真实的分红历史数据
                # 目前先使用现有数据
                pass
            except Exception as e:
                logger.debug(f"获取股票 {stock_code} 分红历史失败: {e}")

        return stock_info

    def _call_llm_analysis(self, stock_code, stock_info):
        """
        调用LLM API进行基本面分析，重点关注分红可持续性和股价稳定性

        Args:
            stock_code: 股票代码
            stock_info: 股票信息

        Returns:
            dict: 分析结果
        """
        # 构建分析提示
        prompt = self._build_analysis_prompt(stock_code, stock_info)
        system_message = (
            "你是一个专业的股票分析师，特别擅长分析A股公司的分红可持续性和股价稳定性。"
        )

        # 重试逻辑（最多5次）
        max_retries = 5
        retry_delay = 2  # 秒

        for attempt in range(max_retries):
            try:
                # 调用API (使用requests直接调用)
                logger.info(
                    f"尝试第{attempt + 1}/{max_retries}次LLM调用分析股票 {stock_code}"
                )
                response_data = self._call_chat_completion(
                    messages=[
                        {"role": "system", "content": system_message},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.7,
                    max_tokens=1500,  # 增加token数以包含更多分析细节
                    stream=False,
                )

                # 解析响应
                analysis_text = response_data["choices"][0]["message"]["content"]

                # 尝试解析为结构化JSON
                structured_result = self._parse_structured_analysis_response(
                    analysis_text
                )

                if structured_result:
                    # 成功解析为结构化数据
                    analysis_json = {
                        "stock_code": stock_code,
                        "stock_name": stock_info.get("name", ""),
                        "analysis_text": analysis_text,
                        "structured_summary": structured_result,
                        "summary": self._extract_summary_from_structured(
                            structured_result, analysis_text
                        ),
                        "attempts": attempt + 1,
                    }
                else:
                    # 无法解析为结构化数据，记录错误并重试（如果还有重试次数）
                    logger.warning(
                        f"第{attempt + 1}次尝试：LLM未返回有效的结构化JSON格式，"
                        f"响应长度: {len(analysis_text)}"
                    )

                    if attempt < max_retries - 1:
                        # 还有重试次数，继续重试
                        logger.info(f"等待{retry_delay}秒后重试...")
                        time.sleep(retry_delay)
                        continue
                    else:
                        # 最后一次尝试也失败，返回None表示分析失败
                        logger.error(
                            f"所有{max_retries}次尝试均未能获取有效的结构化分析结果"
                        )
                        return None

                # 成功解析结构化数据
                logger.info(f"股票 {stock_code} 分析完成 (第{attempt + 1}次尝试)")
                return analysis_json

            except UnicodeEncodeError as e:
                logger.error(f"第{attempt + 1}次尝试LLM API编码错误: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"所有{max_retries}次尝试均失败: {e}")
                    return None
            except requests.exceptions.RequestException as e:
                logger.error(f"第{attempt + 1}次尝试LLM API连接失败: {e}")
                # 检查是否为InvalidChunkLength错误（响应过大）
                if "InvalidChunkLength" in str(e):
                    logger.error(f"检测到InvalidChunkLength错误，响应可能过大: {e}")
                    if attempt < max_retries - 1:
                        logger.info("等待后重试，可能会减少请求大小...")
                        time.sleep(retry_delay * 2)
                        continue
                    else:
                        logger.error(
                            f"所有{max_retries}次尝试均失败(InvalidChunkLength): {e}"
                        )
                        return None
                # 检查是否为认证错误（401）或速率限制（429）
                if hasattr(e, "response") and e.response is not None:
                    status_code = e.response.status_code
                    if status_code == 401:
                        logger.error("API密钥无效或认证失败")
                        return None  # 认证错误不需要重试
                    elif status_code == 429:
                        logger.error("API速率限制，等待后重试")
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (attempt + 2)
                            logger.info(f"等待{wait_time}秒后重试...")
                            time.sleep(wait_time)
                            continue
                        else:
                            logger.error(f"所有{max_retries}次尝试均失败: {e}")
                            return None
                # 其他网络错误可以重试
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"所有{max_retries}次尝试均失败: {e}")
                    return None

            except Exception as e:
                logger.error(f"第{attempt + 1}次尝试调用LLM API失败: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"所有{max_retries}次尝试均失败: {e}")
                    return None

    def _build_analysis_prompt(self, stock_code, stock_info):
        """
        构建分析提示，重点关注分红可持续性和股价稳定性

        Args:
            stock_code: 股票代码
            stock_info: 股票信息

        Returns:
            str: 分析提示
        """
        # 提取关键指标
        dividend_per_share = stock_info.get("dividend_per_share", "")
        dividend_yield = stock_info.get("dividend_yield", "")
        pe_ratio = stock_info.get("pe_ratio", "")
        pb_ratio = stock_info.get("pb_ratio", "")
        roe = stock_info.get("roe", "")
        debt_ratio = stock_info.get("debt_ratio", "")
        earnings_growth = stock_info.get("earnings_growth", "")
        current_price = stock_info.get("current_price", "")
        ma60 = stock_info.get("ma60", "")

        prompt = f"""
请分析以下A股股票的基本面，重点关注分红可持续性和股价稳定性：

股票代码：{stock_code}
股票名称：{stock_info.get("name", "")}

**关键财务指标：**
- 每股分红：{dividend_per_share if dividend_per_share else "未知"} 元
- 股息率：{dividend_yield if dividend_yield else "未知"} %
- 市盈率(PE)：{pe_ratio if pe_ratio else "未知"}
- 市净率(PB)：{pb_ratio if pb_ratio else "未知"}
- 净资产收益率(ROE)：{roe if roe else "未知"} %
- 资产负债率：{debt_ratio if debt_ratio else "未知"} %
- 业绩增长：{earnings_growth if earnings_growth else "未知"} %

**价格技术指标：**
- 当前价格：{current_price if current_price else "未知"} 元
- 60日移动平均线(MA60)：{ma60 if ma60 else "未知"} 元

请从以下角度进行专业分析：

**1. 分红可持续性分析：**
- 历史分红稳定性（如有可能）
- 当前分红水平与行业对比
- 公司盈利能力支撑分红的能力
- 现金流状况是否支持持续分红
- 未来分红政策预期

**2. 股价稳定性分析：**
- 当前价格与MA60的关系
- 历史价格波动性
- 技术面支撑和阻力位
- 市场情绪和资金流向
- 行业周期位置

**3. 风险因素评估：**
- 行业特定风险
- 公司治理风险
- 宏观经济风险
- 政策风险

**4. 投资建议：**
- 适合的投资者类型（价值/成长/股息投资者）
- 投资时间框架建议
- 关键监控指标

请以JSON格式返回分析结果，包含以下结构化字段：
{{
    "sustainability_score": 1-5,        # 分红可持续性评分（1-5分，5分最优）
    "stability_score": 1-5,            # 股价稳定性评分（1-5分，5分最优）
    "overall_rating": 1-5,             # 总体投资评级（1-5分，5分最优）
    "key_factors": ["因子1", "因子2"], # 关键影响因素列表
    "dividend_sustainability_analysis": "详细分析文本",  # 分红可持续性详细分析
    "price_stability_analysis": "详细分析文本",         # 股价稳定性详细分析
    "major_risks": ["风险1", "风险2"], # 主要风险列表
    "investment_recommendation": "投资建议文本",        # 投资建议
    "monitoring_points": ["监控点1", "监控点2"]        # 需要监控的关键点
}}

请确保分析基于提供的财务指标，如指标缺失请基于行业常识分析。
"""
        return prompt

    def _parse_structured_analysis_response(self, analysis_text):
        """
        解析LLM的结构化分析响应

        Args:
            analysis_text: LLM响应文本

        Returns:
            dict: 结构化分析结果，如果解析失败返回None
        """
        try:
            # 尝试从响应中提取JSON
            json_match = re.search(r"\{.*\}", analysis_text, re.DOTALL)
            if not json_match:
                return None

            json_str = json_match.group()
            data = json.loads(json_str)

            # 验证必要字段
            required_fields = [
                "sustainability_score",
                "stability_score",
                "overall_rating",
            ]
            if not all(field in data for field in required_fields):
                return None

            # 确保分数在合理范围
            structured_result = {
                "sustainability_score": min(
                    max(int(data.get("sustainability_score", 3)), 1), 5
                ),
                "stability_score": min(max(int(data.get("stability_score", 3)), 1), 5),
                "overall_rating": min(max(int(data.get("overall_rating", 3)), 1), 5),
                "key_factors": data.get("key_factors", []),
                "dividend_sustainability_analysis": data.get(
                    "dividend_sustainability_analysis", ""
                ),
                "price_stability_analysis": data.get("price_stability_analysis", ""),
                "major_risks": data.get("major_risks", []),
                "investment_recommendation": data.get("investment_recommendation", ""),
                "monitoring_points": data.get("monitoring_points", []),
            }

            # 验证列表类型
            list_fields = ["key_factors", "major_risks", "monitoring_points"]
            for field in list_fields:
                if not isinstance(structured_result[field], list):
                    structured_result[field] = [str(structured_result[field])]

            return structured_result

        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"解析结构化分析响应失败: {e}")
            return None

    def _extract_summary_from_structured(self, structured_result, analysis_text):
        """
        从结构化结果提取摘要信息

        Args:
            structured_result: 结构化分析结果
            analysis_text: 原始分析文本

        Returns:
            dict: 摘要信息
        """
        sustainability_score = structured_result.get("sustainability_score", 3)
        stability_score = structured_result.get("stability_score", 3)
        overall_rating = structured_result.get("overall_rating", 3)

        # 根据评分判断情感
        total_score = sustainability_score + stability_score + overall_rating
        if total_score >= 12:
            sentiment = "积极"
        elif total_score >= 9:
            sentiment = "中性"
        else:
            sentiment = "谨慎"

        # 判断是否有增长潜力（基于ROE和增长指标）
        has_growth = sustainability_score >= 3 and stability_score >= 3

        # 判断分红情况
        has_dividend = sustainability_score >= 2

        # 判断风险
        has_risk = len(structured_result.get("major_risks", [])) > 0

        summary = {
            "sustainability_score": sustainability_score,
            "stability_score": stability_score,
            "overall_rating": overall_rating,
            "has_growth": has_growth,
            "has_dividend": has_dividend,
            "has_risk": has_risk,
            "sentiment": sentiment,
            "key_factors_count": len(structured_result.get("key_factors", [])),
            "major_risks_count": len(structured_result.get("major_risks", [])),
        }

        return summary

    def _extract_summary(self, analysis_text):
        """
        从分析文本中提取摘要（备用方法，但不应被调用）
        根据要求，统一使用模板（结构化JSON），此方法仅返回默认值

        Args:
            analysis_text: 分析文本

        Returns:
            dict: 默认摘要信息
        """
        # 不使用关键词提取，统一返回默认值
        # 此方法仅作为后备，正常情况下不应被调用
        logger.warning("使用默认摘要提取方法（结构化JSON解析失败时的后备）")

        summary = {
            "sustainability_score": 3,  # 默认值
            "stability_score": 3,  # 默认值
            "overall_rating": 3,  # 默认值
            "has_growth": False,  # 默认值，不使用关键词提取
            "has_dividend": False,  # 默认值，不使用关键词提取
            "has_risk": True,  # 默认值，不使用关键词提取
            "sentiment": "中性",  # 默认值
            "key_factors_count": 0,
            "major_risks_count": 0,
        }

        return summary

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
        import re

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

    def _parse_float_or_null(self, value):
        """解析浮点数或返回null"""
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def _parse_date_or_null(self, value):
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
