"""
股票公告抓取模块
从上交所、深交所官方网站抓取上市公司公告
"""

import logging
import requests
import pandas as pd
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import re
import time
import json

logger = logging.getLogger(__name__)

# 尝试导入内容抓取和LLM分析模块
try:
    from .content_fetcher import ContentFetcher
    CONTENT_FETCHER_AVAILABLE = True
except ImportError:
    CONTENT_FETCHER_AVAILABLE = False
    logger.warning("ContentFetcher不可用，内容抓取功能受限")

try:
    from .llm_analyzer import LLMAnalyzer
    LLM_ANALYZER_AVAILABLE = True
except ImportError:
    LLM_ANALYZER_AVAILABLE = False
    logger.warning("LLMAnalyzer不可用，LLM提取功能受限")

# 尝试导入akshare，如果失败则标记为不可用
try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
    logger.info(f"akshare 模块导入成功，版本: {ak.__version__}")
except ImportError:
    logger.warning("akshare 模块未安装，将无法使用akshare获取公告数据")
    AKSHARE_AVAILABLE = False

class AnnouncementFetcher:
    """公告抓取器"""
    
    def __init__(self, config):
        """
        初始化公告抓取器
        
        Args:
            config: 配置字典
        """
        self.config = config
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.timeout = 30
        self.retry_times = 3
        self.retry_delay = 2
        
        # 缓存配置
        self.cache_days = config.get('storage', {}).get('cache_days', 7)
        
        # 公告配置
        announcement_config = config.get('announcements', {})
        self.dividend_days = announcement_config.get('dividend_days', 420)
        self.enable_content_fetching = announcement_config.get('enable_content_fetching', False)
        self.enable_llm_extraction = announcement_config.get('enable_llm_extraction', False)
        self.max_llm_calls = announcement_config.get('max_llm_calls_per_run', 5)
        self.max_pdf_size_mb = announcement_config.get('max_pdf_size_mb', 10)
        
        # 分红数据缓存
        self._dividend_cache = {}
        
        # 初始化内容抓取器和LLM分析器（如果可用）
        self.content_fetcher = None
        self.llm_analyzer = None
        
        if CONTENT_FETCHER_AVAILABLE and self.enable_content_fetching:
            try:
                self.content_fetcher = ContentFetcher(config)
                logger.info("内容抓取器初始化成功")
            except Exception as e:
                logger.warning(f"内容抓取器初始化失败: {e}")
        
        if LLM_ANALYZER_AVAILABLE and self.enable_llm_extraction:
            try:
                self.llm_analyzer = LLMAnalyzer(config)
                logger.info("LLM分析器初始化成功")
            except Exception as e:
                logger.warning(f"LLM分析器初始化失败: {e}")
        
    def fetch_announcements(self, stock_codes, days=7, dividend_days=None):
        """
        获取股票公告，支持分红公告的扩展时间窗口
        
        Args:
            stock_codes: 股票代码列表
            days: 获取最近几天的公告（默认7天）
            dividend_days: 分红公告的扩展时间窗口（默认None，使用配置中的dividend_days）
            
        Returns:
            dict: 按股票代码组织的公告列表
        """
        if dividend_days is None:
            dividend_days = self.dividend_days
        
        # 使用最大时间窗口获取公告
        fetch_days = max(days, dividend_days)
        announcements = {}
        
        for stock_code in stock_codes:
            try:
                stock_code = str(stock_code)
                logger.info(f"开始获取股票 {stock_code} 的公告（窗口: {fetch_days}天）")
                
                # 跳过ETF基金（它们没有公司公告，只有基金公告）
                # ETF代码通常以51、52开头，使用不同的公告系统
                if stock_code.startswith(('51', '52', '15', '16', '18')):
                    logger.info(f"跳过ETF基金 {stock_code}，ETF使用不同的公告系统")
                    announcements[stock_code] = []
                    continue
                
                # 根据股票代码判断交易所
                if stock_code.startswith(('6', '5', '9')):
                    exchange = 'sse'  # 上海证券交易所
                elif stock_code.startswith(('0', '3', '2')):
                    exchange = 'szse'  # 深圳证券交易所
                else:
                    logger.warning(f"无法识别的股票代码 {stock_code}，跳过")
                    continue
                
                # 获取公告（使用扩展窗口）
                stock_announcements = self._fetch_from_exchange(
                    stock_code, exchange, fetch_days, days, dividend_days
                )
                
                if stock_announcements:
                    announcements[stock_code] = stock_announcements
                    logger.info(f"股票 {stock_code} 获取到 {len(stock_announcements)} 条公告（经过窗口过滤）")
                else:
                    logger.info(f"股票 {stock_code} 未找到公告")
                    announcements[stock_code] = []
                    
            except Exception as e:
                logger.error(f"获取股票 {stock_code} 公告失败: {e}")
                announcements[stock_code] = []
        
        return announcements
    
    def _fetch_from_akshare(self, stock_code, days):
        """
        从akshare获取股票公司公告（非新闻）
        
        Args:
            stock_code: 股票代码
            days: 最近天数
            
        Returns:
            list: 公司公告列表，每个公告为字典
        """
        logger.info(f"_fetch_from_akshare called, AKSHARE_AVAILABLE={AKSHARE_AVAILABLE}")
        if not AKSHARE_AVAILABLE:
            logger.warning("akshare不可用，跳过")
            return []
        
        try:
            logger.info(f"尝试从akshare获取股票 {stock_code} 的公司公告（cninfo）")
            
            # 获取公司公告数据（从巨潮资讯网）
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
            
            # 确保akshare模块可用（已在模块级别检查过）
            if not AKSHARE_AVAILABLE:
                logger.warning("akshare不可用，跳过")
                return []
            
            # akshare模块已在模块级别导入，这里确保可用
            import akshare as ak
            
            try:
                announcements_df = ak.stock_zh_a_disclosure_report_cninfo(
                    symbol=stock_code,
                    market='沪深京',  # 默认市场
                    start_date=start_date,
                    end_date=end_date
                )
            except KeyError as ke:
                # 处理akshare内部的列名错误：当API返回空数据或不同列结构时会发生
                error_msg = str(ke)
                if "None of [Index([" in error_msg and "are in the [columns]" in error_msg:
                    logger.warning(f"akshare列名错误（可能无近期公告），尝试更长时间范围: {error_msg[:100]}...")
                    # 尝试30天范围
                    start_date_30 = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
                    try:
                        announcements_df = ak.stock_zh_a_disclosure_report_cninfo(
                            symbol=stock_code,
                            market='沪深京',
                            start_date=start_date_30,
                            end_date=end_date
                        )
                        logger.info(f"使用30天范围成功获取数据")
                    except Exception as e2:
                        logger.error(f"即使使用30天范围也失败: {e2}")
                        return []
                else:
                    # 其他KeyError（如ETF代码不在map中）
                    logger.error(f"akshare KeyError: {ke}")
                    return []
            except Exception as e:
                # 其他错误（如股票代码无效）
                logger.error(f"调用akshare API失败: {e}")
                return []
            
            if announcements_df.empty:
                logger.info(f"akshare未找到股票 {stock_code} 的公司公告")
                return []
            
            # 重命名列名为英文以便访问（原始列名是中文但可能有编码问题）
            # 注意：列数可能不同（5列或6列），我们处理常见情况
            column_mapping = {}
            num_cols = len(announcements_df.columns)
            
            # 处理5列情况：代码, 简称, 公告标题, 公告时间, 公告链接
            if num_cols >= 5:
                column_mapping[str(announcements_df.columns[0])] = 'code'
                column_mapping[str(announcements_df.columns[1])] = 'name'
                column_mapping[str(announcements_df.columns[2])] = 'title'
                column_mapping[str(announcements_df.columns[3])] = 'date_str'
                column_mapping[str(announcements_df.columns[4])] = 'url'
            
            # 如果有第6列，可能是announcementId或orgId，忽略或存储
            if num_cols >= 6:
                column_mapping[str(announcements_df.columns[5])] = 'extra1'
            
            if num_cols >= 7:
                column_mapping[str(announcements_df.columns[6])] = 'extra2'
                
            if not column_mapping:
                logger.warning(f"无法处理列结构: 只有{num_cols}列")
                return []
                
            announcements_df = announcements_df.rename(columns=column_mapping)
            
            # 转换为公告格式
            announcements = []
            cutoff_date = datetime.now() - timedelta(days=days)
            
            for _, row in announcements_df.iterrows():
                try:
                    # 使用英文列名访问数据，处理可能的缺失列
                    try:
                        ann_stock_code = str(row['code']) if 'code' in row.index and not pd.isna(row['code']) else stock_code
                    except:
                        ann_stock_code = stock_code
                    
                    try:
                        company_name = str(row['name']) if 'name' in row.index and not pd.isna(row['name']) else ''
                    except:
                        company_name = ''
                    
                    try:
                        title = str(row['title']) if 'title' in row.index and not pd.isna(row['title']) else ''
                    except:
                        title = ''
                    
                    try:
                        pub_date_str = str(row['date_str']) if 'date_str' in row.index and not pd.isna(row['date_str']) else ''
                    except:
                        pub_date_str = ''
                    
                    try:
                        url = str(row['url']) if 'url' in row.index and not pd.isna(row['url']) else ''
                    except:
                        url = ''
                    
                    # 解析公告时间
                    pub_date = None
                    if pub_date_str:
                        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d', '%Y%m%d'):
                            try:
                                pub_date = datetime.strptime(pub_date_str.strip(), fmt)
                                break
                            except ValueError:
                                continue
                    
                    if not pub_date:
                        logger.debug(f"无法解析公告时间: {pub_date_str}")
                        continue
                    
                    # 检查是否在指定天数内
                    if pub_date.date() < cutoff_date.date():
                        continue
                    
                    # 构建公告字典
                    announcement = {
                        'stock_code': stock_code,
                        'exchange': 'cninfo',  # 标记为cninfo来源
                        'title': title,
                        'date': pub_date.strftime('%Y-%m-%d'),
                        'url': url,
                        'type': '公司公告',
                        'summary': title[:200]  # 使用标题作为摘要
                    }
                    
                    # 根据标题内容确定公告类型
                    important_keywords = {
                        '分红': '分红公告',
                        '利润分配': '利润分配公告',
                        '派息': '派息公告',
                        '转增': '转增股本公告',
                        '送股': '送股公告',
                        '年报': '年度报告',
                        '中报': '中期报告',
                        '季报': '季度报告',
                        '业绩预告': '业绩预告',
                        '业绩快报': '业绩快报',
                        '业绩变动': '业绩变动公告',
                        '业绩修正': '业绩修正公告',
                        '停牌': '停牌公告',
                        '复牌': '复牌公告',
                        '重大合同': '重大合同公告',
                        '资产重组': '资产重组公告',
                        '并购': '并购公告',
                        '减持': '减持公告',
                        '增持': '增持公告',
                        '回购': '股份回购公告',
                        '股权激励': '股权激励公告',
                        '风险提示': '风险提示公告',
                        '问询函': '监管问询函',
                        '关注函': '监管关注函',
                        '监管函': '监管函',
                        '立案调查': '立案调查公告',
                        '诉讼': '诉讼公告',
                        '担保': '担保公告',
                        '贷款': '贷款公告',
                        '债券': '债券相关公告',
                        '可转债': '可转债公告',
                        '非公开发行': '非公开发行公告',
                        '定向增发': '定向增发公告',
                        '配股': '配股公告',
                        '退市': '退市风险公告',
                        'ST': '特别处理公告',
                        '*ST': '退市风险警示公告'
                    }
                    
                    # 检查标题是否包含关键词
                    title_lower = title.lower()
                    for keyword, ann_type in important_keywords.items():
                        if keyword in title:
                            announcement['type'] = ann_type
                            break
                    
                    # 如果是分红相关公告，尝试获取详细分红数据
                    if any(kw in title for kw in ['分红', '利润分配', '派息', '送股', '转增']):
                        dividend_details = self._get_dividend_details(stock_code, announcement_date=pub_date)
                        if dividend_details:
                            announcement['dividend_details'] = dividend_details
                            logger.debug(f"为分红公告添加详细分红数据: {list(dividend_details.keys())}")
                    
                    announcements.append(announcement)
                    
                except Exception as e:
                    logger.debug(f"处理公司公告行时出错: {e}")
                    continue
            
            logger.info(f"从akshare(cninfo)获取到股票 {stock_code} 的 {len(announcements)} 条公司公告")
            return announcements
            
        except Exception as e:
            logger.error(f"从akshare获取股票 {stock_code} 公司公告失败: {e}")
            # 失败时返回空列表（不尝试获取新闻，因为用户需要的是官方公告不是新闻）
            return []
    
    def _filter_announcements_by_window(self, announcements, original_days, dividend_days):
        """
        根据公告类型和时间窗口过滤公告
        
        Args:
            announcements: 公告列表
            original_days: 普通公告的时间窗口
            dividend_days: 分红公告的扩展时间窗口
            
        Returns:
            list: 过滤后的公告列表
        """
        filtered = []
        dividend_keywords = ['分红', '利润分配', '派息', '送股', '转增']
        cutoff_date_original = datetime.now() - timedelta(days=original_days)
        cutoff_date_dividend = datetime.now() - timedelta(days=dividend_days)
        
        for announcement in announcements:
            title = announcement.get('title', '')
            date_str = announcement.get('date', '')
            
            # 解析公告日期
            pub_date = None
            if date_str:
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d', '%Y%m%d'):
                    try:
                        pub_date = datetime.strptime(date_str.strip(), fmt)
                        break
                    except ValueError:
                        continue
            
            if not pub_date:
                # 如果无法解析日期，跳过该公告
                continue
            
            # 检查是否为分红相关公告
            is_dividend = any(keyword in title for keyword in dividend_keywords)
            
            # 应用相应的时间窗口
            if is_dividend:
                if pub_date.date() >= cutoff_date_dividend.date():
                    filtered.append(announcement)
            else:
                if pub_date.date() >= cutoff_date_original.date():
                    filtered.append(announcement)
        
        return filtered
    
    def _enrich_announcements(self, announcements):
        """
        丰富公告信息：对分红相关公告获取内容并提取分红详情
        
        Args:
            announcements: 公告列表
            
        Returns:
            list: 丰富后的公告列表
        """
        if not announcements:
            return announcements
        
        # 检查是否启用内容抓取和LLM提取
        if not self.enable_content_fetching or not self.content_fetcher:
            logger.debug("内容抓取未启用，跳过公告丰富")
            return announcements
        
        if not self.enable_llm_extraction or not self.llm_analyzer:
            logger.debug("LLM提取未启用，跳过公告丰富")
            return announcements
        
        dividend_keywords = ['分红', '利润分配', '派息', '送股', '转增']
        enriched = []
        llm_calls_made = 0
        
        for announcement in announcements:
            title = announcement.get('title', '')
            stock_code = announcement.get('stock_code', '')
            date = announcement.get('date', '')
            
            # 检查是否为分红相关公告
            is_dividend = any(keyword in title for keyword in dividend_keywords)
            
            if not is_dividend:
                enriched.append(announcement)
                continue
            
            # 分红相关公告：尝试获取内容并提取分红详情
            url = announcement.get('url', '')
            if not url:
                logger.debug(f"公告无URL，跳过内容抓取: {title[:50]}...")
                enriched.append(announcement)
                continue
            
            # 检查LLM调用限制
            if llm_calls_made >= self.max_llm_calls:
                logger.info(f"已达到最大LLM调用限制 ({self.max_llm_calls})，跳过剩余公告的LLM提取")
                enriched.append(announcement)
                continue
            
            try:
                # 获取公告内容
                content_result = self.content_fetcher.fetch_content(url, stock_code, date)
                if not content_result or not content_result.get('success', False):
                    logger.debug(f"无法获取公告内容: {url}")
                    enriched.append(announcement)
                    continue
                
                extracted_text = content_result.get('extracted_text', '')
                content_hash = content_result.get('content_hash', '')
                
                if not extracted_text:
                    logger.debug(f"公告内容为空: {url}")
                    enriched.append(announcement)
                    continue
                
                # 使用LLM提取分红详情
                extraction_result = self.llm_analyzer.extract_dividend_details_from_announcement(
                    stock_code=stock_code,
                    title=title,
                    announcement_text=extracted_text,
                    content_hash=content_hash
                )
                
                if extraction_result and extraction_result.get('success', False):
                    # 添加LLM提取结果到公告
                    announcement['llm_extracted_dividend'] = extraction_result
                    logger.info(f"成功提取分红详情: {title[:50]}...")
                    llm_calls_made += 1
                else:
                    logger.debug(f"LLM提取分红详情失败: {title[:50]}...")
                
            except Exception as e:
                logger.error(f"丰富公告时出错: {e}")
            
            enriched.append(announcement)
        
        logger.info(f"公告丰富完成，处理 {len(announcements)} 条公告，其中 {llm_calls_made} 条使用了LLM提取")
        return enriched
    
    def _fetch_from_exchange(self, stock_code, exchange, fetch_days, original_days, dividend_days):
        """
        从指定交易所获取公告，并应用时间窗口过滤
        
        Args:
            stock_code: 股票代码
            exchange: 交易所 ('sse' 或 'szse')
            fetch_days: 获取公告的时间窗口（最大天数）
            original_days: 普通公告的时间窗口
            dividend_days: 分红公告的扩展时间窗口
            
        Returns:
            list: 公告列表，每个公告为字典
        """
        try:
            logger.info(f"_fetch_from_exchange: stock_code={stock_code}, exchange={exchange}, fetch_days={fetch_days}, original_days={original_days}, dividend_days={dividend_days}")
            # 首先尝试从akshare获取新闻/公告（更可靠）
            akshare_result = self._fetch_from_akshare(stock_code, fetch_days)
            if akshare_result:
                logger.info(f"股票 {stock_code} 从akshare获取到 {len(akshare_result)} 条新闻/公告（过滤前）")
                # 应用时间窗口过滤
                filtered_result = self._filter_announcements_by_window(
                    akshare_result, original_days, dividend_days
                )
                logger.info(f"股票 {stock_code} 过滤后保留 {len(filtered_result)} 条公告")
                # 丰富公告信息（内容抓取和LLM提取）
                enriched_result = self._enrich_announcements(filtered_result)
                return enriched_result
            
            logger.info(f"股票 {stock_code} akshare未获取到数据，尝试交易所官方接口")
            
            # akshare失败，尝试交易所官方接口
            if exchange == 'sse':
                result = self._fetch_from_sse(stock_code, fetch_days)
            elif exchange == 'szse':
                result = self._fetch_from_szse(stock_code, fetch_days)
            else:
                logger.error(f"不支持的交易所: {exchange}")
                result = []
            
            # 如果获取到公告，返回结果
            if result:
                # 应用时间窗口过滤
                filtered_result = self._filter_announcements_by_window(
                    result, original_days, dividend_days
                )
                logger.info(f"股票 {stock_code} 从{exchange}获取到 {len(result)} 条公告，过滤后保留 {len(filtered_result)} 条")
                # 丰富公告信息（内容抓取和LLM提取）
                enriched_result = self._enrich_announcements(filtered_result)
                return enriched_result
            else:
                # 没有获取到公告，返回空列表（不生成模拟数据）
                logger.info(f"从{exchange}未获取到{stock_code}的公告")
                return []
                
        except Exception as e:
            logger.error(f"从 {exchange} 获取公告失败: {e}")
            # 发生异常时返回空列表
            logger.info(f"获取公告失败，返回空列表")
            return []
    

    def _fetch_from_sse(self, stock_code, days):
        """
        从上海证券交易所获取公告
        
        Args:
            stock_code: 股票代码
            days: 最近天数
            
        Returns:
            list: 公告列表
        """
        try:
            # 上海证券交易所公告查询API（需要分析实际接口）
            # 这里使用一个公开的查询接口示例
            url = "http://www.sse.com.cn/disclosure/listedinfo/announcement/"
            
            # 构建查询参数
            params = {
                'stockCode': stock_code,
                'startDate': (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d'),
                'endDate': datetime.now().strftime('%Y-%m-%d'),
                'pageSize': '50'
            }
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': 'http://www.sse.com.cn/'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            
            # 尝试解析JSON响应（如果API返回JSON）
            try:
                data = response.json()
                return self._parse_sse_api_response(data, stock_code)
            except json.JSONDecodeError:
                # 如果返回的是HTML，则解析HTML
                return self._parse_sse_html(response.text, stock_code)
                
        except Exception as e:
            logger.error(f"从上交所获取公告失败: {e}")
            # 尝试备用方法
            return self._fetch_from_sse_backup(stock_code, days)
    
    def _parse_sse_api_response(self, data, stock_code):
        """
        解析上交所API响应
        
        Args:
            data: API返回的JSON数据
            stock_code: 股票代码
            
        Returns:
            list: 公告列表
        """
        announcements = []
        
        try:
            # 根据实际API结构解析
            # 这里是一个示例结构，需要根据实际API调整
            if isinstance(data, dict) and 'result' in data:
                items = data['result']
            elif isinstance(data, list):
                items = data
            else:
                logger.warning(f"上交所API返回格式未知: {data}")
                return announcements
            
            for item in items:
                announcement = {
                    'stock_code': stock_code,
                    'exchange': 'sse',
                    'title': item.get('title', ''),
                    'date': item.get('date', ''),
                    'url': item.get('url', ''),
                    'type': item.get('type', ''),
                    'summary': item.get('summary', '')[:200]  # 截断摘要
                }
                announcements.append(announcement)
                
        except Exception as e:
            logger.error(f"解析上交所API响应失败: {e}")
        
        return announcements
    
    def _parse_sse_html(self, html, stock_code):
        """
        解析上交所HTML页面
        
        Args:
            html: HTML内容
            stock_code: 股票代码
            
        Returns:
            list: 公告列表
        """
        announcements = []
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # 查找公告表格（根据实际页面结构调整）
            tables = soup.find_all('table')
            
            for table in tables:
                rows = table.find_all('tr')
                for row in rows[1:]:  # 跳过表头
                    cols = row.find_all('td')
                    if len(cols) >= 4:
                        try:
                            date = cols[0].text.strip()
                            title_elem = cols[1].find('a')
                            title = title_elem.text.strip() if title_elem else cols[1].text.strip()
                            url = title_elem.get('href') if title_elem else ''
                            type_text = cols[2].text.strip() if len(cols) > 2 else ''
                            
                            # 构建完整URL
                            if url and not url.startswith('http'):
                                url = f"http://www.sse.com.cn{url}"
                            
                            announcement = {
                                'stock_code': stock_code,
                                'exchange': 'sse',
                                'title': title,
                                'date': date,
                                'url': url,
                                'type': type_text,
                                'summary': title  # 使用标题作为摘要
                            }
                            announcements.append(announcement)
                        except Exception as e:
                            logger.debug(f"解析表格行失败: {e}")
                            continue
            
        except Exception as e:
            logger.error(f"解析上交所HTML失败: {e}")
        
        return announcements
    
    def _fetch_from_sse_backup(self, stock_code, days):
        """
        上交所备用获取方法（使用第三方数据源）
        
        Args:
            stock_code: 股票代码
            days: 最近天数
            
        Returns:
            list: 公告列表
        """
        try:
            # 尝试使用新浪财经公告接口
            url = f"http://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllBulletin/stockid/{stock_code}.phtml"
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': f'http://finance.sina.com.cn/realstock/company/sh{stock_code}/nc.shtml'
            }
            
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.encoding = 'gb2312'
            
            if response.status_code != 200:
                logger.warning(f"新浪财经公告页面请求失败: {response.status_code}")
                return []
            
            return self._parse_sina_announcements(response.text, stock_code, 'sse')
            
        except Exception as e:
            logger.error(f"上交所备用方法失败: {e}")
            return []
    
    def _fetch_from_szse(self, stock_code, days):
        """
        从深圳证券交易所获取公告
        
        Args:
            stock_code: 股票代码
            days: 最近天数
            
        Returns:
            list: 公告列表
        """
        try:
            # 深圳证券交易所公告查询
            url = "http://www.szse.cn/api/disc/announcement/annList"
            
            # 构建请求参数
            params = {
                'se-date': (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d') + '~' + datetime.now().strftime('%Y-%m-%d'),
                'stock': stock_code,
                'pagesize': '50',
                'pageno': '1'
            }
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': 'http://www.szse.cn/disclosure/listed/notice/index.html'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            
            # 尝试解析JSON响应
            try:
                data = response.json()
                return self._parse_szse_api_response(data, stock_code)
            except json.JSONDecodeError:
                # 如果返回的是HTML，则解析HTML
                return self._parse_szse_html(response.text, stock_code)
                
        except Exception as e:
            logger.error(f"从深交所获取公告失败: {e}")
            # 尝试备用方法
            return self._fetch_from_szse_backup(stock_code, days)
    
    def _parse_szse_api_response(self, data, stock_code):
        """
        解析深交所API响应
        
        Args:
            data: API返回的JSON数据
            stock_code: 股票代码
            
        Returns:
            list: 公告列表
        """
        announcements = []
        
        try:
            # 根据实际API结构解析
            if isinstance(data, dict) and 'data' in data:
                items = data['data']
            elif isinstance(data, list):
                items = data
            else:
                logger.warning(f"深交所API返回格式未知: {data}")
                return announcements
            
            for item in items:
                announcement = {
                    'stock_code': stock_code,
                    'exchange': 'szse',
                    'title': item.get('title', ''),
                    'date': item.get('date', ''),
                    'url': item.get('url', ''),
                    'type': item.get('type', ''),
                    'summary': item.get('summary', '')[:200]
                }
                announcements.append(announcement)
                
        except Exception as e:
            logger.error(f"解析深交所API响应失败: {e}")
        
        return announcements
    
    def _parse_szse_html(self, html, stock_code):
        """
        解析深交所HTML页面
        
        Args:
            html: HTML内容
            stock_code: 股票代码
            
        Returns:
            list: 公告列表
        """
        announcements = []
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # 查找公告列表（根据实际页面结构调整）
            announcement_list = soup.find_all('div', class_='article-list')
            
            for article_list in announcement_list:
                articles = article_list.find_all('li')
                for article in articles:
                    try:
                        date_elem = article.find('span', class_='time')
                        title_elem = article.find('a')
                        
                        date = date_elem.text.strip() if date_elem else ''
                        title = title_elem.text.strip() if title_elem else ''
                        url = title_elem.get('href') if title_elem else ''
                        
                        # 构建完整URL
                        if url and not url.startswith('http'):
                            url = f"http://www.szse.cn{url}"
                        
                        announcement = {
                            'stock_code': stock_code,
                            'exchange': 'szse',
                            'title': title,
                            'date': date,
                            'url': url,
                            'type': '',  # 深交所页面可能没有类型
                            'summary': title
                        }
                        announcements.append(announcement)
                    except Exception as e:
                        logger.debug(f"解析文章项失败: {e}")
                        continue
            
        except Exception as e:
            logger.error(f"解析深交所HTML失败: {e}")
        
        return announcements
    
    def _fetch_from_szse_backup(self, stock_code, days):
        """
        深交所备用获取方法
        
        Args:
            stock_code: 股票代码
            days: 最近天数
            
        Returns:
            list: 公告列表
        """
        try:
            # 尝试使用新浪财经公告接口（深交所股票）
            url = f"http://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllBulletin/stockid/{stock_code}.phtml"
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': f'http://finance.sina.com.cn/realstock/company/sz{stock_code}/nc.shtml'
            }
            
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.encoding = 'gb2312'
            
            if response.status_code != 200:
                logger.warning(f"新浪财经公告页面请求失败: {response.status_code}")
                return []
            
            return self._parse_sina_announcements(response.text, stock_code, 'szse')
            
        except Exception as e:
            logger.error(f"深交所备用方法失败: {e}")
            return []
    
    def _parse_sina_announcements(self, html, stock_code, exchange):
        """
        解析新浪财经公告页面
        
        Args:
            html: HTML内容
            stock_code: 股票代码
            exchange: 交易所
            
        Returns:
            list: 公告列表
        """
        announcements = []
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # 查找公告表格
            tables = soup.find_all('table', class_='datatbl')
            
            for table in tables:
                rows = table.find_all('tr')
                for row in rows[1:]:  # 跳过表头
                    cols = row.find_all('td')
                    if len(cols) >= 3:
                        try:
                            date = cols[0].text.strip()
                            title_elem = cols[1].find('a')
                            title = title_elem.text.strip() if title_elem else cols[1].text.strip()
                            url = title_elem.get('href') if title_elem else ''
                            
                            # 构建完整URL
                            if url and not url.startswith('http'):
                                url = f"http://vip.stock.finance.sina.com.cn{url}"
                            
                            announcement = {
                                'stock_code': stock_code,
                                'exchange': exchange,
                                'title': title,
                                'date': date,
                                'url': url,
                                'type': cols[2].text.strip() if len(cols) > 2 else '',
                                'summary': title
                            }
                            announcements.append(announcement)
                        except Exception as e:
                            logger.debug(f"解析新浪公告行失败: {e}")
                            continue
            
        except Exception as e:
            logger.error(f"解析新浪财经公告页面失败: {e}")
        
        return announcements
    
    def get_recent_important_announcements(self, stock_codes, days=3, dividend_days=None):
        """
        获取近期重要公告（如业绩预告、分红预案等），支持分红公告的扩展时间窗口
        
        Args:
            stock_code: 股票代码列表
            days: 最近天数
            dividend_days: 分红公告的扩展时间窗口（默认None，使用配置中的dividend_days）
            
        Returns:
            dict: 重要公告列表
        """
        if dividend_days is None:
            dividend_days = self.dividend_days
        all_announcements = self.fetch_announcements(stock_codes, days, dividend_days)
        important_announcements = {}
        
        # 定义重要公告关键词
        important_keywords = [
            '业绩预告', '业绩快报', '业绩变动', '业绩修正', '年报', '半年报', '季报',
            '分红', '利润分配', '派息', '转增', '送股',
            '重大合同', '重大投资', '资产重组', '并购',
            '减持', '增持', '回购', '股权激励',
            '风险提示', '澄清公告', '问询函', '关注函', '监管函',
            '立案调查', '诉讼', '担保', '贷款', '债券', '可转债',
            '非公开发行', '定向增发', '配股', '退市', 'ST', '*ST'
        ]
        
        for stock_code, announcements in all_announcements.items():
            important_list = []
            other_list = []
            
            for announcement in announcements:
                title = announcement.get('title', '')
                # 检查标题是否包含重要关键词
                if any(keyword in title for keyword in important_keywords):
                    important_list.append(announcement)
                else:
                    other_list.append(announcement)
            
            # 如果有重要公告，返回重要公告
            if important_list:
                important_announcements[stock_code] = important_list[:5]  # 最多5条
            # 如果没有重要公告但其他公告，返回最近的其他公告（最多3条）
            elif other_list:
                important_announcements[stock_code] = other_list[:3]
            # 如果完全没有公告，返回空列表
            else:
                important_announcements[stock_code] = []
        
        return important_announcements
    

    

    
    def _get_dividend_details(self, stock_code, announcement_date=None):
        """
        获取股票的详细分红数据
        
        Args:
            stock_code: 股票代码
            announcement_date: 公告日期（可选，用于匹配最近的分红方案）
            
        Returns:
            dict: 分红详情，如果找不到则返回空字典
        """
        # 确保股票代码为字符串类型
        stock_code = str(stock_code)
        if not AKSHARE_AVAILABLE:
            return {}
        
        # akshare模块已在模块级别导入，这里确保可用
        import akshare as ak
        
        # 检查缓存 - 缓存整个分红历史DataFrame
        cache_key = stock_code
        if cache_key in self._dividend_cache:
            dividend_df = self._dividend_cache[cache_key]
        else:
            try:
                # 获取分红数据
                dividend_df = ak.stock_dividend_cninfo(symbol=stock_code)
                if dividend_df.empty:
                    self._dividend_cache[cache_key] = pd.DataFrame()
                    return {}
                
                # 重命名列名为英文以便访问（按位置映射）
                # 列顺序：实施方案公告日期, 分红类型, 送股比例, 转增比例, 派息比例, 
                #        股权登记日, 除权日, 派息日, 股份到账日, 实施方案分红说明, 报告时间
                column_names = [
                    'announcement_date',    # 实施方案公告日期
                    'dividend_type',        # 分红类型
                    'stock_dividend_ratio', # 送股比例
                    'capitalization_ratio', # 转增比例
                    'cash_dividend_ratio',  # 派息比例
                    'record_date',          # 股权登记日
                    'ex_rights_date',       # 除权日
                    'payment_date',         # 派息日
                    'settlement_date',      # 股份到账日
                    'dividend_description', # 实施方案分红说明
                    'report_period'         # 报告时间
                ]
                
                # 确保列数匹配
                if len(dividend_df.columns) != len(column_names):
                    logger.warning(f"分红数据列数不匹配: 期望{len(column_names)}，实际{len(dividend_df.columns)}")
                    # 使用原始列名，但创建映射
                    column_mapping = {}
                    for i in range(len(dividend_df.columns)):
                        orig_col = str(dividend_df.columns[i])
                        if i < len(column_names):
                            column_mapping[orig_col] = column_names[i]
                        else:
                            column_mapping[orig_col] = f'col_{i}'
                    dividend_df = dividend_df.rename(columns=column_mapping)
                else:
                    # 创建列映射
                    column_mapping = {str(dividend_df.columns[i]): column_names[i] for i in range(len(column_names))}
                    dividend_df = dividend_df.rename(columns=column_mapping)
                
                # 确保announcement_date列为datetime类型
                if 'announcement_date' in dividend_df.columns:
                    # 尝试转换为datetime，如果已经是datetime则保持
                    dividend_df['announcement_date'] = pd.to_datetime(dividend_df['announcement_date'], errors='coerce')
                    # 按公告日期降序排序
                    dividend_df = dividend_df.sort_values('announcement_date', ascending=False)
                
                # 缓存整个DataFrame
                self._dividend_cache[cache_key] = dividend_df
                
            except Exception as e:
                logger.error(f"获取股票 {stock_code} 分红详情失败: {e}")
                self._dividend_cache[cache_key] = pd.DataFrame()
                return {}
        
        # 如果缓存中是空的DataFrame，返回空字典
        if self._dividend_cache[cache_key].empty:
            return {}
        
        dividend_df = self._dividend_cache[cache_key]
        
        # 如果没有提供公告日期，返回最新记录
        if announcement_date is None:
            latest = dividend_df.iloc[0]
            dividend_details = {}
            for col in dividend_df.columns:
                value = latest[col]
                if pd.notna(value):
                    dividend_details[col] = str(value)
            return dividend_details
        
        # 将公告日期转换为datetime（如果已经是datetime则保持）
        try:
            if isinstance(announcement_date, str):
                target_date = pd.to_datetime(announcement_date, errors='coerce')
            else:
                target_date = pd.to_datetime(announcement_date)
            
            if pd.isna(target_date):
                logger.warning(f"无法解析公告日期: {announcement_date}，返回最新记录")
                latest = dividend_df.iloc[0]
                dividend_details = {}
                for col in dividend_df.columns:
                    value = latest[col]
                    if pd.notna(value):
                        dividend_details[col] = str(value)
                return dividend_details
        except Exception as e:
            logger.warning(f"处理公告日期时出错: {e}，返回最新记录")
            latest = dividend_df.iloc[0]
            dividend_details = {}
            for col in dividend_df.columns:
                value = latest[col]
                if pd.notna(value):
                    dividend_details[col] = str(value)
            return dividend_details
        
        # 查找最接近的分红记录
        max_date_diff_days = 180  # 最大允许日期差异（天）
        best_match_pos = -1
        min_date_diff = float('inf')
        
        # 按位置迭代（不是按标签），因为DataFrame已排序
        for pos in range(len(dividend_df)):
            row = dividend_df.iloc[pos]
            div_date = row['announcement_date']
            if pd.isna(div_date):
                continue
            
            date_diff = abs((div_date - target_date).days)
            if date_diff < min_date_diff:
                min_date_diff = date_diff
                best_match_pos = pos
        
        # 检查是否找到匹配且日期差异在允许范围内
        if best_match_pos >= 0 and min_date_diff <= max_date_diff_days:
            matched_row = dividend_df.iloc[best_match_pos]
            dividend_details = {}
            for col in dividend_df.columns:
                value = matched_row[col]
                if pd.notna(value):
                    dividend_details[col] = str(value)
            
            logger.debug(f"为股票 {stock_code} 找到匹配的分红记录: 公告日期 {announcement_date}, 匹配日期 {matched_row['announcement_date']}, 日期差异 {min_date_diff}天")
            return dividend_details
        else:
            # 没有找到合适的匹配
            if best_match_pos >= 0:
                logger.debug(f"股票 {stock_code} 的分红记录日期差异过大: {min_date_diff}天 > {max_date_diff_days}天，返回空字典")
            else:
                logger.debug(f"股票 {stock_code} 未找到有效的分红记录")
            return {}
    def _get_annual_dividend_per_share(self, stock_code):
        """尝试从年均股息数据获取每股分红"""
        try:
            import akshare as ak
            dividend_df = ak.stock_history_dividend()
            stock_dividend = dividend_df[dividend_df["代码"] == stock_code]
            if not stock_dividend.empty:
                latest_dividend = stock_dividend.iloc[0]
                annual_dividend = latest_dividend.get("年均股息", 0)
                if annual_dividend and annual_dividend > 0:
                    original = annual_dividend
                    unit_note = "原始值"
                    
                    # 单位修正：
                    # 1. 如果数值过大（>100），可能以分为单位，转换为元
                    if annual_dividend > 100:
                        annual_dividend = annual_dividend / 100.0
                        unit_note = "分转元"
                        logger.info(f"股票 {stock_code} 分红数据单位修正({unit_note}): {original:.3f} → {annual_dividend:.3f}元")
                    # 2. 如果数值在2-100之间，可能为每10股金额，转换为每股
                    elif annual_dividend > 2:
                        annual_dividend = annual_dividend / 10.0
                        unit_note = "每10股转每股"
                        logger.info(f"股票 {stock_code} 分红数据单位修正({unit_note}): {original:.3f} → {annual_dividend:.3f}元/股")
                    
                    if 0.01 <= annual_dividend <= 5.0:
                        per_share = round(annual_dividend, 3)
                        logger.info(f"股票 {stock_code} 使用年均股息: {per_share:.3f}元/股 (原始值: {original:.3f}, {unit_note})")
                        return per_share
                    else:
                        logger.warning(f"股票 {stock_code} 年均股息数据不合理: {annual_dividend:.3f}元/股 (原始值: {original:.3f}, {unit_note})")
        except Exception as e:
            logger.debug(f"获取股票 {stock_code} 年均股息失败: {e}")
        return None

    def get_latest_dividend_per_share(self, stock_code):
        """
        获取股票最新的每股分红（元）
        
        Args:
            stock_code: 股票代码
            
        Returns:
            float: 每股分红（元），如果找不到则返回None
        """
        # 首先尝试cninfo最新分红数据（最准确）
        dividend_details = self._get_dividend_details(stock_code)
        if dividend_details:
            # 检查分红数据是否过时（超过3年）
            announcement_date_str = dividend_details.get('announcement_date')
            if announcement_date_str:
                try:
                    from datetime import datetime
                    date_str = announcement_date_str.split()[0]
                    announcement_date = datetime.strptime(date_str, '%Y-%m-%d')
                    current_date = datetime.now()
                    years_diff = (current_date - announcement_date).days / 365.25
                    
                    if years_diff <= 3:
                        cash_dividend_ratio = dividend_details.get('cash_dividend_ratio')
                        if cash_dividend_ratio:
                            try:
                                per_share = float(cash_dividend_ratio) / 10.0
                                if 0.01 <= per_share <= 5:
                                    logger.info(f"股票 {stock_code} 使用cninfo最新分红: {per_share:.3f}元/股 (公告日期: {announcement_date_str})")
                                    return per_share
                                else:
                                    logger.warning(f"股票 {stock_code} cninfo分红数据不合理: {per_share:.3f}元/股 (超出合理范围0.01-5元)")
                            except (ValueError, TypeError):
                                pass
                    else:
                        logger.warning(f"股票 {stock_code} 分红数据过时: {announcement_date_str} ({years_diff:.1f}年前)，尝试年均股息数据")
                except Exception as e:
                    logger.debug(f"解析分红日期失败: {e}")
        
        # 如果cninfo数据不可用或过时，尝试年均股息数据
        per_share = self._get_annual_dividend_per_share(stock_code)
        if per_share is not None:
            return per_share
        
        # 如果都没有，返回None
        return None
    
    def get_total_dividends_last_12months(self, stock_code):
        """
        获取股票过去12个月的总每股分红（元），包括年度分红和中期分红
        
        Args:
            stock_code: 股票代码
            
        Returns:
            float: 过去12个月的总每股分红（元），如果找不到则返回None
        """
        # 首先尝试从cninfo获取详细分红数据
        stock_code = str(stock_code)
        if not AKSHARE_AVAILABLE:
            logger.debug(f"akshare不可用，无法获取股票 {stock_code} 的分红数据")
            return None
        
        import akshare as ak
        
        try:
            # 获取分红数据
            dividend_df = ak.stock_dividend_cninfo(symbol=stock_code)
            if dividend_df.empty:
                logger.debug(f"股票 {stock_code} 无分红数据")
                return None
            
            # 列映射（基于已知的列顺序）
            # 0: 实施方案公告日期, 1: 分红类型, 4: 派息比例, 9: 实施方案分红说明
            if len(dividend_df.columns) < 10:
                logger.warning(f"股票 {stock_code} 分红数据列数不足: {len(dividend_df.columns)}")
                return None
            
            # 处理日期列
            date_col = dividend_df.columns[0]
            dividend_df['announcement_date'] = pd.to_datetime(dividend_df.iloc[:, 0], errors='coerce')
            
            # 按日期降序排序
            dividend_df = dividend_df.sort_values('announcement_date', ascending=False)
            
            # 计算12个月前的时间点
            twelve_months_ago = datetime.now() - timedelta(days=365)
            
            # 过滤过去12个月的分红
            recent_dividends = dividend_df[dividend_df['announcement_date'] >= twelve_months_ago]
            
            if recent_dividends.empty:
                logger.debug(f"股票 {stock_code} 过去12个月内无分红记录")
                return None
            
            total_per_share = 0.0
            dividend_count = 0
            
            for _, row in recent_dividends.iterrows():
                # 获取现金分红比例（第4列）
                cash_ratio = row.iloc[4]
                if pd.isna(cash_ratio) or cash_ratio <= 0:
                    continue
                
                # 获取分红描述（第9列）用于解析每股金额
                dividend_desc = row.iloc[9] if len(dividend_df.columns) > 9 else None
                
                # 解析每股分红金额
                shares_for_dividend = 10.0  # 默认每10股
                if dividend_desc and isinstance(dividend_desc, str):
                    # 尝试从描述中解析股数，例如 "10��1.7Ԫ(��˰)" -> 10股
                    import re
                    match = re.search(r'(\d+)\s*��', dividend_desc)
                    if match:
                        shares_for_dividend = float(match.group(1))
                
                # 计算每股分红
                per_share = cash_ratio / shares_for_dividend
                
                # 验证合理性
                if 0.001 <= per_share <= 5.0:  # 放宽下限，允许小金额分红
                    total_per_share += per_share
                    dividend_count += 1
                    
                    # 记录分红详情
                    div_type = row.iloc[1] if len(dividend_df.columns) > 1 else "未知"
                    div_date = row['announcement_date']
                    logger.debug(f"股票 {stock_code} 分红记录: {div_date.strftime('%Y-%m-%d')}, 类型: {div_type}, 每股: {per_share:.3f}元, 累计: {total_per_share:.3f}元")
                else:
                    logger.warning(f"股票 {stock_code} 分红数据不合理: {per_share:.3f}元/股 (现金比例: {cash_ratio}, 描述: {dividend_desc})")
            
            if dividend_count > 0:
                logger.info(f"股票 {stock_code} 过去12个月分红总计: {total_per_share:.3f}元/股 ({dividend_count}次分红)")
                return round(total_per_share, 3)
            else:
                logger.debug(f"股票 {stock_code} 无有效的近期分红记录")
                return None
                
        except Exception as e:
            logger.error(f"计算股票 {stock_code} 过去12个月分红总计失败: {e}")
            return None
    
