"""
LLM分析模块
使用DeepSeek API分析股票基本面
"""

import logging
import json
import ssl
import sys
import io
import httpx
from httpx import Timeout, Limits
from .cache_manager import CacheManager

# 猴子补丁：确保JSON序列化使用UTF-8编码，不转义非ASCII字符
_original_json_dumps = json.dumps
def _patched_json_dumps(obj, *args, **kwargs):
    """确保所有JSON序列化使用UTF-8编码"""
    kwargs['ensure_ascii'] = False
    return _original_json_dumps(obj, *args, **kwargs)
json.dumps = _patched_json_dumps

_original_json_dump = json.dump
def _patched_json_dump(obj, fp, *args, **kwargs):
    """确保所有JSON序列化使用UTF-8编码"""
    kwargs['ensure_ascii'] = False
    return _original_json_dump(obj, fp, *args, **kwargs)
json.dump = _patched_json_dump

# 猴子补丁：确保httpx头部编码使用UTF-8而不是ASCII
import httpx._utils as httpx_utils
_original_normalize_header_value = httpx_utils.normalize_header_value
def _patched_normalize_header_value(value, encoding=None):
    """确保httpx头部编码使用UTF-8"""
    if encoding is None:
        encoding = 'utf-8'
    return _original_normalize_header_value(value, encoding)
httpx_utils.normalize_header_value = _patched_normalize_header_value

from openai import OpenAI

# 设置UTF-8编码，避免中文字符编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

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
        self.llm_config = config.get('llm', {})
        # 初始化缓存管理器
        self.cache_manager = CacheManager(config)
        
        # 初始化OpenAI客户端（兼容DeepSeek API）
        api_key = self.llm_config.get('api_key', '')
        base_url = self.llm_config.get('base_url', 'https://api.deepseek.com/v1')
        model = self.llm_config.get('model', 'deepseek-chat')
        
        if not api_key:
            logger.warning("LLM API密钥未配置，LLM分析功能不可用")
            self.client = None
        else:
            # 创建自定义HTTP客户端，尝试解决SSL超时问题
            http_client = httpx.Client(
                headers={
                    'Content-Type': 'application/json; charset=utf-8',
                    'Accept': 'application/json; charset=utf-8'
                },
                timeout=Timeout(connect=120.0, read=180.0, write=120.0, pool=120.0),
                verify=True,  # 启用SSL验证（如遇SSL超时，可临时设置为False）
                limits=Limits(max_connections=5, max_keepalive_connections=5),
                follow_redirects=True
            )
            
            self.client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=http_client
            )
            self.model = model
    
    def analyze_stocks(self, stock_codes):
        """
        分析股票基本面
        
        Args:
            stock_codes: 股票代码列表
            
        Returns:
            dict: 分析结果
        """
        if not self.client:
            logger.warning("LLM客户端未初始化，跳过分析")
            return {}
        
        analysis_results = {}
        
        for stock_code in stock_codes:
            try:
                # 首先尝试从缓存获取分析结果
                cached_analysis = self.cache_manager.get_analysis_cache(stock_code)
                if cached_analysis and 'analysis' in cached_analysis:
                    logger.info(f"股票 {stock_code} 使用缓存分析结果")
                    analysis_results[stock_code] = cached_analysis['analysis']
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
                        self.cache_manager.set_analysis_cache(stock_code, analysis_result)
                        logger.debug(f"股票 {stock_code} 分析结果已缓存")
                    except Exception as cache_error:
                        logger.warning(f"缓存股票 {stock_code} 分析结果失败: {cache_error}")
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
            'name': f'股票{stock_code}',
            'industry': '',
            'market_cap': '',
            'pe_ratio': '',
            'pb_ratio': '',
            'dividend_yield': '',
            'last_dividend_date': '',
            'last_dividend_amount': ''
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
        
        try:
            # 调用API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=2000
            )
            
            # 解析响应
            analysis_text = response.choices[0].message.content
            
            # 尝试解析为JSON（如果LLM返回结构化数据）
            try:
                # 首先尝试从文本中提取JSON
                import re
                json_match = re.search(r'\{.*\}', analysis_text, re.DOTALL)
                if json_match:
                    analysis_json = json.loads(json_match.group())
                else:
                    # 如果没有JSON，使用文本分析
                    analysis_json = {
                        'stock_code': stock_code,
                        'stock_name': stock_info.get('name', ''),
                        'analysis_text': analysis_text,
                        'summary': self._extract_summary(analysis_text)
                    }
            except json.JSONDecodeError:
                # 如果无法解析为JSON，使用文本格式
                analysis_json = {
                    'stock_code': stock_code,
                    'stock_name': stock_info.get('name', ''),
                    'analysis_text': analysis_text,
                    'summary': self._extract_summary(analysis_text)
                }
            
            return analysis_json
            
        except UnicodeEncodeError as e:
            logger.error(f"LLM API编码错误: {e}")
            logger.error(f"错误对象类型: {type(e.object)}")
            logger.error(f"错误对象repr: {repr(e.object)}")
            logger.error(f"错误位置: {e.start}-{e.end}")
            logger.error(f"错误对象切片: {repr(e.object[e.start:e.end])}")
            logger.error(f"系统消息长度: {len(system_message)}")
            logger.error(f"系统消息前50字符: {system_message[:50]}")
            logger.error(f"提示长度: {len(prompt)}")
            logger.error(f"提示前100字符: {prompt[:100]}")
            import traceback
            logger.error(traceback.format_exc())
            return None
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, ssl.SSLError) as e:
            logger.error(f"LLM API连接失败（超时/SSL）: {e}")
            logger.warning("LLM分析失败，返回空结果")
            return None
        except Exception as e:
            logger.error(f"调用LLM API失败: {e}", exc_info=True)
            logger.warning("LLM分析失败，返回空结果")
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
        股票名称：{stock_info.get('name', '')}
        行业：{stock_info.get('industry', '')}
        市值：{stock_info.get('market_cap', '')}
        市盈率(PE)：{stock_info.get('pe_ratio', '')}
        市净率(PB)：{stock_info.get('pb_ratio', '')}
        股息率：{stock_info.get('dividend_yield', '')}
        最近分红日期：{stock_info.get('last_dividend_date', '')}
        最近分红金额：{stock_info.get('last_dividend_amount', '')}
        
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
            'has_growth': '增长' in analysis_text or '成长' in analysis_text,
            'has_risk': '风险' in analysis_text or '谨慎' in analysis_text,
            'has_dividend': '分红' in analysis_text or '股息' in analysis_text,
            'sentiment': '中性'  # 默认
        }
        
        # 判断情感倾向
        positive_words = ['推荐', '买入', '看好', '优质', '低估', '机会']
        negative_words = ['谨慎', '回避', '高估', '风险', '卖出', '警告']
        
        positive_count = sum(1 for word in positive_words if word in analysis_text)
        negative_count = sum(1 for word in negative_words if word in analysis_text)
        
        if positive_count > negative_count:
            summary['sentiment'] = '积极'
        elif negative_count > positive_count:
            summary['sentiment'] = '谨慎'
        else:
            summary['sentiment'] = '中性'
        
        return summary
    

    
