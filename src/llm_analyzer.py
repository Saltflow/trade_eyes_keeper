"""
LLM分析模块
使用DeepSeek API分析股票基本面
"""

import logging
import json
import hashlib
import time
from datetime import datetime
import sys
import io
import requests
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

# 猴子补丁：确保httpx头部编码使用UTF-8而不是ASCII
try:
    import httpx._utils as httpx_utils

    _original_normalize_header_value = httpx_utils.normalize_header_value

    def _patched_normalize_header_value(value, encoding=None):
        """确保httpx头部编码使用UTF-8"""
        if encoding is None:
            encoding = "utf-8"
        return _original_normalize_header_value(value, encoding)

    httpx_utils.normalize_header_value = _patched_normalize_header_value
except AttributeError:
    # httpx版本不兼容，跳过猴子补丁
    import sys

    print(
        "Warning: httpx._utils.normalize_header_value not found, skipping monkey patch",
        file=sys.stderr,
    )


# 设置UTF-8编码，避免中文字符编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

logger = logging.getLogger(__name__)


class LLMAnalyzer:
    """LLM分析器"""

    def __init__(self, config):
        """
        初始化LLM分析器

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
            self.client = None
        else:
            # 使用requests库直接调用API，避免httpx/OpenAI库的SSL超时问题
            self.api_key = api_key
            self.base_url = base_url
            self.model = model
            self.client = None  # 不再使用OpenAI客户端
            logger.info("LLM API配置完成，将使用requests直接调用")

    def _call_chat_completion(self, messages, temperature=0.7, max_tokens=2000, stream=False):
        """
        使用requests直接调用DeepSeek API
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
                        chunk_str = chunk.decode('utf-8')
                        if chunk_str.startswith('data: '):
                            chunk_data = chunk_str[6:]
                            if chunk_data == '[DONE]':
                                break
                            try:
                                chunk_json = json.loads(chunk_data)
                                if 'choices' in chunk_json and len(chunk_json['choices']) > 0:
                                    if 'delta' in chunk_json['choices'][0] and 'content' in chunk_json['choices'][0]['delta']:
                                        content_chunks.append(chunk_json['choices'][0]['delta']['content'])
                            except json.JSONDecodeError:
                                logger.warning(f"无法解析流式响应块: {chunk_data}")
                content = ''.join(content_chunks)
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
                return self._call_chat_completion(messages, temperature, reduced_max_tokens, stream=False)
            # 尝试非流式模式重试
            if stream:
                logger.info("尝试使用非流式模式重试...")
                return self._call_chat_completion(messages, temperature, max_tokens, stream=False)
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"API请求失败: {e}")
            # 检查是否为InvalidChunkLength错误（可能隐藏在内部异常中）
            if "InvalidChunkLength" in str(e):
                logger.error("检测到InvalidChunkLength错误，尝试减少max_tokens重试")
                reduced_max_tokens = max(500, max_tokens // 2)
                logger.info(f"尝试减少max_tokens到{reduced_max_tokens}并重试...")
                return self._call_chat_completion(messages, temperature, reduced_max_tokens, stream=False)
                
            if hasattr(e, "response") and e.response is not None:
                logger.error(
                    f"响应状态: {e.response.status_code}, 内容: {e.response.text[:500]}"
                )
            raise

    def analyze_stocks(self, stock_codes):
        """
        分析股票基本面

        Args:
            stock_codes: 股票代码列表

        Returns:
            dict: 分析结果
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

                # 获取股票基本信息
                stock_info = self._get_stock_info(stock_code)

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

    def _get_stock_info(self, stock_code):
        """
        获取股票基本信息
        实际应用中可能需要从API获取真实数据，这里返回基础信息

        Args:
            stock_code: 股票代码

        Returns:
            dict: 股票基础信息（不含模拟数据）
        """
        # 返回基础信息，避免使用模拟数据
        # 未来可扩展为从akshare或其他API获取真实股票信息
        return {
            "name": f"股票{stock_code}",
            "industry": "",
            "market_cap": "",
            "pe_ratio": "",
            "pb_ratio": "",
            "dividend_yield": "",
            "last_dividend_date": "",
            "last_dividend_amount": "",
        }

    def _call_llm_analysis(self, stock_code, stock_info):
        """
        调用LLM API进行基本面分析

        Args:
            stock_code: 股票代码
            stock_info: 股票信息

        Returns:
            dict: 分析结果
        """
        # 构建分析提示
        prompt = self._build_analysis_prompt(stock_code, stock_info)
        system_message = "你是一个专业的股票分析师，擅长分析A股公司的基本面和投资价值。"

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
                    max_tokens=1200,
                    stream=False,
                )
                # 保持与OpenAI响应格式兼容
                # 解析响应
                analysis_text = response_data["choices"][0]["message"]["content"]

                # 尝试解析为JSON（如果LLM返回结构化数据）
                try:
                    # 首先尝试从文本中提取JSON
                    import re

                    json_match = re.search(r"\{.*\}", analysis_text, re.DOTALL)
                    if json_match:
                        analysis_json = json.loads(json_match.group())
                    else:
                        # 如果没有JSON，使用文本分析
                        analysis_json = {
                            "stock_code": stock_code,
                            "stock_name": stock_info.get("name", ""),
                            "analysis_text": analysis_text,
                            "summary": self._extract_summary(analysis_text),
                            "attempts": attempt + 1,
                        }
                except json.JSONDecodeError:
                    # 如果无法解析为JSON，使用文本格式
                    analysis_json = {
                        "stock_code": stock_code,
                        "stock_name": stock_info.get("name", ""),
                        "analysis_text": analysis_text,
                        "summary": self._extract_summary(analysis_text),
                        "attempts": attempt + 1,
                    }

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
                        logger.error(f"所有{max_retries}次尝试均失败(InvalidChunkLength): {e}")
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
        构建分析提示

        Args:
            stock_code: 股票代码
            stock_info: 股票信息

        Returns:
            str: 分析提示
        """
        prompt = f"""
        请分析以下A股股票的基本面和投资价值：
        
        股票代码：{stock_code}
        股票名称：{stock_info.get("name", "")}
        行业：{stock_info.get("industry", "")}
        市值：{stock_info.get("market_cap", "")}
        市盈率(PE)：{stock_info.get("pe_ratio", "")}
        市净率(PB)：{stock_info.get("pb_ratio", "")}
        股息率：{stock_info.get("dividend_yield", "")}
        最近分红日期：{stock_info.get("last_dividend_date", "")}
        最近分红金额：{stock_info.get("last_dividend_amount", "")}
        
        请从以下角度进行分析：
        1. 基本面分析（行业地位、竞争优势、财务状况）
        2. 盈利能力分析（毛利率、净利率、ROE等）
        3. 分红情况分析（分红稳定性、股息率水平）
        4. 风险评估（行业风险、公司特定风险）
        5. 投资建议（适合的投资者类型、投资时点建议）
        
        请以专业分析师的角度提供详细分析，并给出结构化的总结。
        如果可以，请用JSON格式返回分析结果，包含以下字段：
        - 基本面评级（1-5星）
        - 盈利能力评级（1-5星）
        - 分红稳定性评级（1-5星）
        - 总体投资价值评级（1-5星）
        - 关键风险点
        - 投资建议
        """

        return prompt

    def _extract_summary(self, analysis_text):
        """
        从分析文本中提取摘要

        Args:
            analysis_text: 分析文本

        Returns:
            dict: 摘要信息
        """
        # 简单的关键词提取
        summary = {
            "has_growth": "增长" in analysis_text or "成长" in analysis_text,
            "has_risk": "风险" in analysis_text or "谨慎" in analysis_text,
            "has_dividend": "分红" in analysis_text or "股息" in analysis_text,
            "sentiment": "中性",  # 默认
        }

        # 判断情感倾向
        positive_words = ["推荐", "买入", "看好", "优质", "低估", "机会"]
        negative_words = ["谨慎", "回避", "高估", "风险", "卖出", "警告"]

        positive_count = sum(1 for word in positive_words if word in analysis_text)
        negative_count = sum(1 for word in negative_words if word in analysis_text)

        if positive_count > negative_count:
            summary["sentiment"] = "积极"
        elif negative_count > positive_count:
            summary["sentiment"] = "谨慎"
        else:
            summary["sentiment"] = "中性"

        return summary

    def extract_dividend_details_from_announcement(
        self, stock_code, title, announcement_text, content_hash=None
    ):
        """
        从公告文本中提取结构化分红数据

        Args:
            stock_code: 股票代码
            title: 公告标题
            announcement_text: 公告正文文本
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
                                stock_code, title, "", content_hash, extraction_result
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
                                    "",
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
