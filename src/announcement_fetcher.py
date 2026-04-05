"""
股票公告抓取模块
从上交所、深交所官方网站抓取上市公司公告
"""

import logging
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from typing import Any

# 为类型检查预先定义可选依赖
ContentFetcher: Any = None
LLMAnalyzer: Any = None

logger = logging.getLogger(__name__)

# 尝试导入内容抓取和LLM分析模块
try:
    from .content_fetcher import ContentFetcher

    CONTENT_FETCHER_AVAILABLE = True
except ImportError as e:
    CONTENT_FETCHER_AVAILABLE = False
    ContentFetcher = None  # type: ignore
    logger.warning(f"ContentFetcher不可用，内容抓取功能受限: {e}")
    logger.debug(f"ContentFetcher import error details", exc_info=True)

try:
    from .llm_analyzer import LLMAnalyzer

    LLM_ANALYZER_AVAILABLE = True
except ImportError as e:
    LLM_ANALYZER_AVAILABLE = False
    LLMAnalyzer = None  # type: ignore
    logger.warning(f"LLMAnalyzer不可用，LLM提取功能受限: {e}")
    logger.debug(f"Import error details", exc_info=True)


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
        self.cache_days = config.get("storage", {}).get("cache_days", 7)

        # 公告配置
        announcement_config = config.get("announcements", {})
        self.dividend_days = announcement_config.get("dividend_days", 420)
        self.enable_content_fetching = announcement_config.get(
            "enable_content_fetching", False
        )
        self.enable_llm_extraction = announcement_config.get(
            "enable_llm_extraction", False
        )
        self.max_llm_calls = announcement_config.get("max_llm_calls_per_run", 5)
        self.max_pdf_size_mb = announcement_config.get("max_pdf_size_mb", 10)

        # 分红数据缓存
        self._dividend_cache = {}

        # 初始化内容抓取器和LLM分析器（如果可用）
        self.content_fetcher = None
        self.llm_analyzer = None

        if CONTENT_FETCHER_AVAILABLE and self.enable_content_fetching:
            try:
                self.content_fetcher = ContentFetcher(config)  # type: ignore[operator]
                logger.info("内容抓取器初始化成功")
            except Exception as e:
                logger.warning(f"内容抓取器初始化失败: {e}")

        if LLM_ANALYZER_AVAILABLE and self.enable_llm_extraction:
            try:
                self.llm_analyzer = LLMAnalyzer(config)  # type: ignore[operator]
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
                if stock_code.startswith(("51", "52", "15", "16", "18")):
                    logger.info(f"跳过ETF基金 {stock_code}，ETF使用不同的公告系统")
                    announcements[stock_code] = []
                    continue

                # 根据股票代码判断交易所
                if stock_code.startswith(("6", "5", "9")):
                    exchange = "sse"  # 上海证券交易所
                elif stock_code.startswith(("0", "3", "2")):
                    exchange = "szse"  # 深圳证券交易所
                else:
                    logger.warning(f"无法识别的股票代码 {stock_code}，跳过")
                    continue

                # 获取公告（使用扩展窗口）
                stock_announcements = self._fetch_from_exchange(
                    stock_code, exchange, fetch_days, days, dividend_days
                )

                if stock_announcements:
                    announcements[stock_code] = stock_announcements
                    logger.info(
                        f"股票 {stock_code} 获取到 {len(stock_announcements)} 条公告（经过窗口过滤）"
                    )
                else:
                    logger.info(f"股票 {stock_code} 未找到公告")
                    announcements[stock_code] = []

            except Exception as e:
                logger.error(f"获取股票 {stock_code} 公告失败: {e}")
                announcements[stock_code] = []

        return announcements

    def _filter_announcements_by_window(
        self, announcements, original_days, dividend_days
    ):
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
        dividend_keywords = ["分红", "利润分配", "派息", "送股", "转增"]
        cutoff_date_original = datetime.now() - timedelta(days=original_days)
        cutoff_date_dividend = datetime.now() - timedelta(days=dividend_days)

        for announcement in announcements:
            title = announcement.get("title", "")
            date_str = announcement.get("date", "")

            # 解析公告日期
            pub_date = None
            if date_str:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
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

        dividend_keywords = ["分红", "利润分配", "派息", "送股", "转增"]
        enriched = []
        llm_calls_made = 0

        for announcement in announcements:
            title = announcement.get("title", "")
            stock_code = announcement.get("stock_code", "")
            date = announcement.get("date", "")

            # 检查是否为分红相关公告
            is_dividend = any(keyword in title for keyword in dividend_keywords)

            if not is_dividend:
                enriched.append(announcement)
                continue

            # 分红相关公告：尝试获取内容并提取分红详情
            url = announcement.get("url", "")
            if not url:
                logger.debug(f"公告无URL，跳过内容抓取: {title[:50]}...")
                enriched.append(announcement)
                continue

            # 检查LLM调用限制
            if llm_calls_made >= self.max_llm_calls:
                logger.info(
                    f"已达到最大LLM调用限制 ({self.max_llm_calls})，跳过剩余公告的LLM提取"
                )
                enriched.append(announcement)
                continue

            try:
                # 获取公告内容
                content_result = self.content_fetcher.fetch_content(
                    url, stock_code, date
                )
                if not content_result or not content_result.get("success", False):
                    logger.debug(f"无法获取公告内容: {url}")
                    enriched.append(announcement)
                    continue

                extracted_text = content_result.get("extracted_text", "")
                content_hash = content_result.get("content_hash", "")

                if not extracted_text:
                    logger.debug(f"公告内容为空: {url}")
                    enriched.append(announcement)
                    continue

                # 使用LLM提取分红详情
                extraction_result = (
                    self.llm_analyzer.extract_dividend_details_from_announcement(
                        stock_code=stock_code,
                        title=title,
                        announcement_text=extracted_text,
                        content_hash=content_hash,
                        date=date,
                    )
                )

                if extraction_result and extraction_result.get("success", False):
                    # 添加LLM提取结果到公告
                    announcement["llm_extracted_dividend"] = extraction_result
                    logger.info(f"成功提取分红详情: {title[:50]}...")
                    llm_calls_made += 1
                else:
                    logger.debug(f"LLM提取分红详情失败: {title[:50]}...")

            except Exception as e:
                logger.error(f"丰富公告时出错: {e}")

            enriched.append(announcement)

        logger.info(
            f"公告丰富完成，处理 {len(announcements)} 条公告，其中 {llm_calls_made} 条使用了LLM提取"
        )
        return enriched

    def _fetch_from_exchange(
        self, stock_code, exchange, fetch_days, original_days, dividend_days
    ):
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
            logger.info(
                f"_fetch_from_exchange: stock_code={stock_code}, exchange={exchange}, fetch_days={fetch_days}, original_days={original_days}, dividend_days={dividend_days}"
            )

            # 交易所官方接口长期失效，直接使用新浪公告页作为唯一来源
            if exchange == "sse":
                result = self._fetch_from_sse_backup(stock_code, fetch_days)
            elif exchange == "szse":
                result = self._fetch_from_szse_backup(stock_code, fetch_days)
            else:
                logger.error(f"不支持的交易所: {exchange}")
                result = []

            if result:
                filtered_result = self._filter_announcements_by_window(
                    result, original_days, dividend_days
                )
                logger.info(
                    f"股票 {stock_code} 获取到 {len(result)} 条公告，过滤后保留 {len(filtered_result)} 条"
                )
                enriched_result = self._enrich_announcements(filtered_result)
                return enriched_result

            logger.info(f"未获取到{stock_code}的公告")
            return []

        except Exception as e:
            logger.error(f"从 {exchange} 获取公告失败: {e}")
            # 发生异常时返回空列表
            logger.info("获取公告失败，返回空列表")
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
        logger.warning("SSE主接口已弃用，直接返回空列表")
        return []

    def _parse_sse_api_response(self, data, stock_code):
        """
        解析上交所API响应

        Args:
            data: API返回的JSON数据
            stock_code: 股票代码

        Returns:
            list: 公告列表
        """
        return []

    def _parse_sse_html(self, html, stock_code):
        """
        解析上交所HTML页面

        Args:
            html: HTML内容
            stock_code: 股票代码

        Returns:
            list: 公告列表
        """
        return []

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
                "User-Agent": self.user_agent,
                "Referer": f"http://finance.sina.com.cn/realstock/company/sh{stock_code}/nc.shtml",
            }

            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.encoding = "gb2312"

            if response.status_code != 200:
                logger.warning(f"新浪财经公告页面请求失败: {response.status_code}")
                return []

            return self._parse_sina_announcements(response.text, stock_code, "sse")

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
        logger.warning("SZSE主接口已弃用，直接返回空列表")
        return []

    def _parse_szse_api_response(self, data, stock_code):
        """
        解析深交所API响应

        Args:
            data: API返回的JSON数据
            stock_code: 股票代码

        Returns:
            list: 公告列表
        """
        return []

    def _parse_szse_html(self, html, stock_code):
        """
        解析深交所HTML页面

        Args:
            html: HTML内容
            stock_code: 股票代码

        Returns:
            list: 公告列表
        """
        return []

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
                "User-Agent": self.user_agent,
                "Referer": f"http://finance.sina.com.cn/realstock/company/sz{stock_code}/nc.shtml",
            }

            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.encoding = "gb2312"

            if response.status_code != 200:
                logger.warning(f"新浪财经公告页面请求失败: {response.status_code}")
                return []

            return self._parse_sina_announcements(response.text, stock_code, "szse")

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
            soup = BeautifulSoup(html, "html.parser")

            # 尝试新样式：查找公告列表 (div.datelist > ul)
            datelist_div = soup.find("div", class_="datelist")
            if datelist_div:
                ul = datelist_div.find("ul")
                if ul:
                    # 解析ul中的内容：日期和链接被<br>分隔
                    # 方法：遍历ul的所有子节点
                    current_date = None
                    for child in ul.children:
                        if child.name is None:  # 文本节点
                            text = str(child)
                            # 查找日期模式 YYYY-MM-DD
                            import re

                            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
                            if date_match:
                                current_date = date_match.group(1)
                        elif child.name == "a":  # 链接标签
                            if current_date:
                                href = child.get("href", "")
                                title = child.get_text(strip=True)
                                # 构建完整URL
                                if href and not href.startswith("http"):
                                    url = f"http://vip.stock.finance.sina.com.cn{href}"
                                else:
                                    url = href

                                announcement = {
                                    "stock_code": stock_code,
                                    "exchange": exchange,
                                    "title": title,
                                    "date": current_date,
                                    "url": url,
                                    "type": "",  # 新浪页面没有类型信息
                                    "summary": title,
                                }
                                announcements.append(announcement)
                                # 重置当前日期，避免重复使用
                                current_date = None
                        # <br>标签忽略，继续处理下一个节点

                    if announcements:
                        logger.info(
                            f"从新浪财经datelist解析到 {len(announcements)} 条公告"
                        )
                        return announcements

            # 备用方法：查找旧样式公告表格
            tables = soup.find_all("table", class_="datatbl")
            for table in tables:
                rows = table.find_all("tr")
                for row in rows[1:]:  # 跳过表头
                    cols = row.find_all("td")
                    if len(cols) >= 3:
                        try:
                            date = cols[0].text.strip()
                            title_elem = cols[1].find("a")
                            title = (
                                title_elem.text.strip()
                                if title_elem
                                else cols[1].text.strip()
                            )
                            url = title_elem.get("href") if title_elem else ""

                            # 构建完整URL
                            if url and not url.startswith("http"):
                                url = f"http://vip.stock.finance.sina.com.cn{url}"

                            announcement = {
                                "stock_code": stock_code,
                                "exchange": exchange,
                                "title": title,
                                "date": date,
                                "url": url,
                                "type": cols[2].text.strip() if len(cols) > 2 else "",
                                "summary": title,
                            }
                            announcements.append(announcement)
                        except Exception as e:
                            logger.debug(f"解析新浪公告行失败: {e}")
                            continue

        except Exception as e:
            logger.error(f"解析新浪财经公告页面失败: {e}")

        return announcements

    def get_recent_important_announcements(
        self, stock_codes, days=3, dividend_days=None
    ):
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
            "业绩预告",
            "业绩快报",
            "业绩变动",
            "业绩修正",
            "年报",
            "半年报",
            "季报",
            "分红",
            "利润分配",
            "派息",
            "转增",
            "送股",
            "重大合同",
            "重大投资",
            "资产重组",
            "并购",
            "减持",
            "增持",
            "回购",
            "股权激励",
            "风险提示",
            "澄清公告",
            "问询函",
            "关注函",
            "监管函",
            "立案调查",
            "诉讼",
            "担保",
            "贷款",
            "债券",
            "可转债",
            "非公开发行",
            "定向增发",
            "配股",
            "退市",
            "ST",
            "*ST",
        ]

        for stock_code, announcements in all_announcements.items():
            important_list = []
            other_list = []

            for announcement in announcements:
                title = announcement.get("title", "")
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
