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
        
        # 分红数据缓存
        self._dividend_cache = {}
        
    def fetch_announcements(self, stock_codes, days=7):
        """
        获取股票公告
        
        Args:
            stock_codes: 股票代码列表
            days: 获取最近几天的公告（默认7天）
            
        Returns:
            dict: 按股票代码组织的公告列表
        """
        announcements = {}
        
        for stock_code in stock_codes:
            try:
                stock_code = str(stock_code)
                logger.info(f"开始获取股票 {stock_code} 的公告")
                
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
                
                # 获取公告
                stock_announcements = self._fetch_from_exchange(stock_code, exchange, days)
                
                if stock_announcements:
                    announcements[stock_code] = stock_announcements
                    logger.info(f"股票 {stock_code} 获取到 {len(stock_announcements)} 条公告")
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
    
    def _fetch_from_exchange(self, stock_code, exchange, days):
        """
        从指定交易所获取公告
        
        Args:
            stock_code: 股票代码
            exchange: 交易所 ('sse' 或 'szse')
            days: 最近天数
            
        Returns:
            list: 公告列表，每个公告为字典
        """
        try:
            logger.info(f"_fetch_from_exchange: stock_code={stock_code}, exchange={exchange}, days={days}")
            # 首先尝试从akshare获取新闻/公告（更可靠）
            akshare_result = self._fetch_from_akshare(stock_code, days)
            if akshare_result:
                logger.info(f"股票 {stock_code} 从akshare获取到 {len(akshare_result)} 条新闻/公告")
                return akshare_result
            
            logger.info(f"股票 {stock_code} akshare未获取到数据，尝试交易所官方接口")
            
            # akshare失败，尝试交易所官方接口
            if exchange == 'sse':
                result = self._fetch_from_sse(stock_code, days)
            elif exchange == 'szse':
                result = self._fetch_from_szse(stock_code, days)
            else:
                logger.error(f"不支持的交易所: {exchange}")
                result = []
            
            # 如果获取到公告，返回结果
            if result:
                return result
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
    
    def get_recent_important_announcements(self, stock_codes, days=3):
        """
        获取近期重要公告（如业绩预告、分红预案等）
        
        Args:
            stock_code: 股票代码列表
            days: 最近天数
            
        Returns:
            dict: 重要公告列表
        """
        all_announcements = self.fetch_announcements(stock_codes, days)
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
        if not AKSHARE_AVAILABLE:
            return {}
        
        # akshare模块已在模块级别导入，这里确保可用
        import akshare as ak
        
        # 检查缓存
        cache_key = stock_code
        if cache_key in self._dividend_cache:
            return self._dividend_cache[cache_key]
        
        try:
            # 获取分红数据
            dividend_df = ak.stock_dividend_cninfo(symbol=stock_code)
            if dividend_df.empty:
                self._dividend_cache[cache_key] = {}
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
                # 使用原始列名
                dividend_details = {}
                for i in range(len(dividend_df.columns)):
                    col = str(dividend_df.columns[i])
                    dividend_details[col] = str(dividend_df.iloc[0][col]) if pd.notna(dividend_df.iloc[0][col]) else ''
            else:
                # 创建列映射
                column_mapping = {str(dividend_df.columns[i]): column_names[i] for i in range(len(column_names))}
                dividend_df = dividend_df.rename(columns=column_mapping)
                
                # 按公告日期降序排序
                if 'announcement_date' in dividend_df.columns:
                    dividend_df = dividend_df.sort_values('announcement_date', ascending=False)
                
                # 获取最新记录
                latest = dividend_df.iloc[0]
                dividend_details = {}
                
                # 提取所有字段
                for col in dividend_df.columns:
                    value = latest[col]
                    if pd.notna(value):
                        dividend_details[col] = str(value)
            
            # 缓存结果
            self._dividend_cache[cache_key] = dividend_details
            return dividend_details
                
        except Exception as e:
            logger.error(f"获取股票 {stock_code} 分红详情失败: {e}")
            self._dividend_cache[cache_key] = {}
            return {}
    
