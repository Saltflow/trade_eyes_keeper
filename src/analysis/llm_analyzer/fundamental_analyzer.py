"""
LLM基本面分析器
专注于股票基本面分析，特别是分红可持续性和股价稳定性
"""

import logging
import json
import re
import time
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional

from .base import BaseLLMClient

logger = logging.getLogger(__name__)


class FundamentalAnalyzer(BaseLLMClient):
    """LLM股票基本面分析器，专注于分红可持续性和股价稳定性分析"""

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
                # 计算当前数据哈希（如果提供了股票数据）
                current_data_hash = None
                if stock_data and stock_code in stock_data:
                    current_data_hash = self._calculate_data_hash(
                        stock_data[stock_code]
                    )
                    logger.debug(f"股票 {stock_code} 数据哈希: {current_data_hash}")

                # 首先尝试从缓存获取分析结果（验证数据哈希）
                cached_analysis = self.cache_manager.get_analysis_cache(
                    stock_code, current_data_hash
                )
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
                    # 缓存分析结果（包含数据哈希）
                    try:
                        self.cache_manager.set_analysis_cache(
                            stock_code, analysis_result, current_data_hash
                        )
                        logger.debug(
                            f"股票 {stock_code} 分析结果已缓存，数据哈希: {current_data_hash}"
                        )
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
