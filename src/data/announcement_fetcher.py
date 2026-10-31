"""
股票公告抓取模块
从新浪财经采集沪深公告、披露易采集港股公告、SEC 采集美股披露
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

# 为类型检查预先定义可选依赖
ContentFetcher: Any = None
LLMAnalyzer: Any = None

logger = logging.getLogger(__name__)


def _announcement_budget(cancelled, deadline):
    if cancelled is not None and cancelled():
        raise ValueError("announcement collection cancelled")
    remaining = deadline - time.monotonic() if deadline is not None else None
    if remaining is not None and remaining <= 0:
        raise ValueError("announcement collection budget exhausted")
    return remaining


class _AnnouncementBudgetHTTP:
    """Apply one poll's remaining deadline to every official HTTP request."""

    def __init__(self, http, cancelled, deadline):
        self.http, self.cancelled, self.deadline = http, cancelled, deadline

    def _request(self, method, url, **kwargs):
        remaining = _announcement_budget(self.cancelled, self.deadline)
        if remaining is not None:
            original = kwargs.get("timeout")
            if isinstance(original, tuple):
                kwargs["timeout"] = tuple(
                    min(value, remaining) if value is not None else remaining
                    for value in original
                )
            else:
                kwargs["timeout"] = (
                    min(original, remaining) if original is not None else remaining
                )
        return getattr(self.http, method)(url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("post", url, **kwargs)


DIVIDEND_KEYWORDS = ("分红", "利润分配", "权益分派", "派息", "送股", "转增")
HK_DIVIDEND_KEYWORDS = (
    *DIVIDEND_KEYWORDS,
    "分紅",
    "利潤分配",
    "權益分派",
    "股息",
    "分派",
    "紅股",
    "轉增",
    "以股代息",
    "dividend",
    "distribution",
    "bonus issue",
)
HK_IMPORTANT_KEYWORDS = (
    *HK_DIVIDEND_KEYWORDS,
    "業績",
    "年報",
    "年度報告",
    "中期報告",
    "季報",
    "盈利",
    "盈警",
    "收購",
    "出售",
    "重組",
    "合併",
    "增持",
    "減持",
    "購回",
    "回購",
    "關連交易",
    "股份獎勵",
    "股權激勵",
    "訴訟",
    "擔保",
    "債券",
    "票據",
    "配股",
    "供股",
    "配售",
    "停牌",
    "復牌",
    "內幕消息",
    "results",
    "annual report",
    "interim report",
    "profit warning",
    "acquisition",
    "disposal",
    "repurchase",
    "connected transaction",
)


def _is_dividend_announcement(announcement: dict) -> bool:
    title = str(announcement.get("title", ""))
    if announcement.get("exchange") == "hkex":
        text = f"{title} {announcement.get('type', '')}".lower()
        return any(keyword in text for keyword in HK_DIVIDEND_KEYWORDS)
    if announcement.get("exchange") == "sec":
        return any(keyword in title.lower() for keyword in ("dividend", "distribution"))
    return any(keyword in title for keyword in DIVIDEND_KEYWORDS)


# 尝试导入内容抓取和LLM分析模块
try:
    from .content_fetcher import ContentFetcher

    CONTENT_FETCHER_AVAILABLE = True
except ImportError as e:
    CONTENT_FETCHER_AVAILABLE = False
    ContentFetcher = None  # type: ignore
    logger.warning(f"ContentFetcher不可用，内容抓取功能受限: {e}")
    logger.debug("ContentFetcher import error details", exc_info=True)

try:
    from .llm import LLMAnalyzer

    LLM_ANALYZER_AVAILABLE = True
except ImportError as e:
    LLM_ANALYZER_AVAILABLE = False
    LLMAnalyzer = None  # type: ignore
    logger.warning(f"LLMAnalyzer不可用，LLM提取功能受限: {e}")
    logger.debug("Import error details", exc_info=True)


class AnnouncementFetcher:
    """公告抓取器"""

    def fetch_announcements_status(
        self,
        stock_codes,
        start: date,
        end: date,
        *,
        observed_at=None,
        max_pages=8,
        cancelled=None,
        deadline=None,
    ) -> dict[str, dict]:
        """Strict, unenriched disclosure windows; failures are never empty success.

        This additive API deliberately bypasses legacy list fallbacks, PDF reads,
        caches and LLM extraction. A-share/fund discovery uses official CNINFO
        identity and pagination; HK/US use the existing strict official sources.
        """
        observed = observed_at or datetime.now(ZoneInfo("Asia/Shanghai"))
        if observed.tzinfo is None or start > end:
            raise ValueError("announcement observation must be aware; invalid window")
        if type(max_pages) is not int or not 1 <= max_pages <= 32:
            raise ValueError("announcement max_pages must be between 1 and 32")
        if deadline is not None and (
            type(deadline) not in (int, float) or not math.isfinite(deadline)
        ):
            raise ValueError("announcement deadline must be a finite monotonic time")
        result = {}
        for raw_code in stock_codes:
            code = str(raw_code).strip().upper().replace(".", "-")
            status = {
                "available": False,
                "items": [],
                "error": None,
                "observed_at": observed,
                "coverage_start": start,
                "coverage_end": end,
                "source": "official_disclosures",
            }
            try:
                _announcement_budget(cancelled, deadline)
                if re.fullmatch(r"\d{6}", code):
                    status["source"] = "cninfo"
                    items = self._fetch_cninfo_status_window(
                        code,
                        start,
                        end,
                        max_pages=max_pages,
                        cancelled=cancelled,
                        deadline=deadline,
                    )
                elif re.fullmatch(r"\d{5}", code):
                    from .hk_announcements import HkexAnnouncementProvider

                    status["source"] = "hkex"
                    provider = self._hk_announcements or HkexAnnouncementProvider(
                        self.config,
                        http=_AnnouncementBudgetHTTP(
                            requests.Session(), cancelled, deadline
                        ),
                    )
                    items = provider.fetch(code, start, end)
                elif re.fullmatch(r"[A-Z][A-Z0-9-]{0,14}", code):
                    from .us_announcements import SecAnnouncementProvider

                    status["source"] = "sec"
                    provider = self._us_announcements or SecAnnouncementProvider(
                        self.config,
                        http=_AnnouncementBudgetHTTP(
                            requests.Session(), cancelled, deadline
                        ),
                    )
                    items = provider.fetch(code, start, end)
                else:
                    raise ValueError("unsupported disclosure security identity")
                if not isinstance(items, list):
                    raise TypeError("disclosure source returned an invalid schema")
                status.update(available=True, items=items)
            except Exception as exc:  # noqa: BLE001 - per-security source availability
                status["error"] = str(exc)
            result[code] = status
        return result

    def _fetch_cninfo_status_window(
        self, code, start, end, *, max_pages, cancelled=None, deadline=None
    ):
        from ..instruments.point_in_time import CninfoAnnualReportProvider

        _announcement_budget(cancelled, deadline)
        provider = CninfoAnnualReportProvider(self.config)
        if cancelled is not None or deadline is not None:
            provider.http = _AnnouncementBudgetHTTP(provider.http, cancelled, deadline)
        org = provider._org_id(code)
        records, seen, expected_pages, expected_total = [], set(), None, None
        for page in range(1, max_pages + 1):
            _announcement_budget(cancelled, deadline)
            response = provider.http.post(
                provider.ANNOUNCEMENT_URL,
                data={
                    "pageNum": page,
                    "pageSize": 50,
                    "column": "sse" if code.startswith(("5", "6", "9")) else "szse",
                    "tabName": "fulltext",
                    "stock": f"{code},{org}",
                    "searchkey": "",
                    "category": "",
                    "seDate": f"{start.isoformat()}~{end.isoformat()}",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "false",
                },
                headers=provider._headers,
                timeout=provider.timeout,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or "announcements" not in body:
                raise TypeError("CNINFO announcement schema missing")
            total = body.get("totalAnnouncement", body.get("totalRecordNum"))
            if total is not None and (type(total) is not int or total < 0):
                raise ValueError("CNINFO invalid total count")
            if (
                "totalAnnouncement" in body
                and "totalRecordNum" in body
                and body["totalAnnouncement"] != body["totalRecordNum"]
            ):
                raise ValueError("CNINFO conflicting total counts")
            pages = body.get("totalpages")
            if type(pages) is not int or pages < 0:
                raise ValueError("CNINFO pagination coverage unavailable")
            # The live official API returns totalpages=0 even for nonempty
            # responses; exact total count + requested pageSize proves coverage.
            if pages == 0 and total is not None:
                pages = (total + 49) // 50
            if pages > max_pages:
                raise ValueError("CNINFO pagination coverage unavailable")
            if expected_pages is None:
                expected_pages, expected_total = pages, total
            elif expected_pages != pages or expected_total != total:
                raise ValueError("CNINFO pagination changed during observation")
            rows = body["announcements"]
            if rows is None and total == 0 and body.get("hasMore") is False:
                rows = []  # Explicit official empty-result contract, not missing schema.
            if not isinstance(rows, list):
                raise TypeError("CNINFO announcement schema missing")
            if not rows:
                if page != 1 or pages > 1 or total not in (None, 0):
                    raise ValueError("CNINFO empty page lacks complete coverage")
                if body.get("hasMore") is not False:
                    raise ValueError("CNINFO empty response lacks terminal proof")
                return []
            if pages < page or len(rows) > 50:
                raise ValueError("CNINFO invalid pagination rows")
            digest = hashlib.sha256(response.content).hexdigest()
            for row in rows:
                if not isinstance(row, dict) or str(row.get("secCode")) != code:
                    raise ValueError("CNINFO announcement security mismatch")
                title = re.sub(r"<[^>]*>", "", str(row.get("announcementTitle", "")))
                adjunct = str(row.get("adjunctUrl", ""))
                url = urljoin(provider.STATIC_ROOT, adjunct)
                target = urlsplit(url)
                if (
                    not title.strip()
                    or not adjunct
                    or target.scheme != "https"
                    or target.hostname != "static.cninfo.com.cn"
                    or target.username
                    or target.password
                    or not target.path.lower().endswith(".pdf")
                ):
                    raise ValueError("CNINFO invalid announcement identity")
                timestamp = row.get("announcementTime")
                if type(timestamp) not in (int, float):
                    raise ValueError("CNINFO announcement publication time missing")
                published = datetime.fromtimestamp(
                    timestamp / 1000, ZoneInfo("Asia/Shanghai")
                )
                if not start <= published.date() <= end:
                    raise ValueError("CNINFO publication outside requested window")
                if url in seen:
                    raise ValueError("CNINFO repeated announcement across pages")
                seen.add(url)
                records.append(
                    {
                        "stock_code": code,
                        "title": title.strip(),
                        "date": published.date().isoformat(),
                        "published_at": published.isoformat(),
                        "url": url,
                        "type": row.get("announcementType", ""),
                        "exchange": "cninfo",
                        "source_sha256": digest,
                        "announcement_id": str(row.get("announcementId", url)),
                    }
                )
            if page == pages:
                if body.get("hasMore") is True or (
                    total is not None and len(records) != total
                ):
                    raise ValueError("CNINFO terminal coverage count mismatch")
                return records
        raise ValueError("CNINFO pagination coverage limit exceeded")

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
        self._hk_announcements = None
        self._us_announcements = None

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
                stock_code = str(stock_code).strip()
                logger.info(f"开始获取股票 {stock_code} 的公告（窗口: {fetch_days}天）")

                # 跳过ETF基金（它们没有公司公告，只有基金公告）
                # ETF代码通常以51、52开头，使用不同的公告系统
                if len(stock_code) == 6 and stock_code.startswith(
                    ("51", "52", "15", "16", "18")
                ):
                    logger.info(f"跳过ETF基金 {stock_code}，ETF使用不同的公告系统")
                    announcements[stock_code] = []
                    continue

                # 根据股票代码判断交易所
                if stock_code.isdigit() and len(stock_code) == 5:
                    exchange = "hkex"
                elif len(stock_code) == 6 and stock_code.startswith(("6", "5", "9")):
                    exchange = "sse"  # 上海证券交易所
                elif len(stock_code) == 6 and stock_code.startswith(("0", "3", "2")):
                    exchange = "szse"  # 深圳证券交易所
                elif re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,14}", stock_code):
                    exchange = "sec"
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
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        cutoff_date_original = today - timedelta(days=original_days)
        cutoff_date_dividend = today - timedelta(days=dividend_days)

        for announcement in announcements:
            date_str = announcement.get("date", "")

            # 解析公告日期
            pub_date = None
            if date_str:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
                    try:
                        pub_date = datetime.strptime(date_str.strip(), fmt).replace(
                            tzinfo=ZoneInfo("Asia/Shanghai")
                        )
                        break
                    except ValueError:
                        continue

            if not pub_date:
                # 如果无法解析日期，跳过该公告
                continue
            if pub_date.date() > today:
                continue

            # 检查是否为分红相关公告
            is_dividend = _is_dividend_announcement(announcement)

            # 应用相应的时间窗口
            if is_dividend:
                if pub_date.date() >= cutoff_date_dividend:
                    filtered.append(announcement)
            else:
                if pub_date.date() >= cutoff_date_original:
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

        enriched = []
        llm_calls_made = 0

        for announcement in announcements:
            title = announcement.get("title", "")
            stock_code = announcement.get("stock_code", "")
            date = announcement.get("date", "")

            # 检查是否为分红相关公告
            is_dividend = any(keyword in title for keyword in DIVIDEND_KEYWORDS)

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
        logger.info(
            f"_fetch_from_exchange: stock_code={stock_code}, exchange={exchange}, fetch_days={fetch_days}, original_days={original_days}, dividend_days={dividend_days}"
        )

        if exchange == "hkex":
            if self._hk_announcements is None:
                from .hk_announcements import HkexAnnouncementProvider

                self._hk_announcements = HkexAnnouncementProvider(self.config)
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            result = self._hk_announcements.fetch(
                stock_code, today - timedelta(days=fetch_days), today
            )
        elif exchange == "sec":
            if self._us_announcements is None:
                from .us_announcements import SecAnnouncementProvider

                self._us_announcements = SecAnnouncementProvider(self.config)
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            result = self._us_announcements.fetch(
                stock_code, today - timedelta(days=fetch_days), today
            )
        # 沪深交易所旧接口失效，沿用新浪公告页。
        elif exchange == "sse":
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
            # 现有 LLM 分红金额结构没有币种；港股金额沿用独立分红事件源。
            if exchange in {"hkex", "sec"}:
                return filtered_result
            enriched_result = self._enrich_announcements(filtered_result)
            return enriched_result

        logger.info(f"未获取到{stock_code}的公告")
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
                    logger.info(f"从新浪财经datelist解析到 {len(announcements)} 条公告")
                    return announcements

        # 备用方法：查找旧样式公告表格
        tables = soup.find_all("table", class_="datatbl")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows[1:]:  # 跳过表头
                cols = row.find_all("td")
                if len(cols) < 2:
                    logger.debug("跳过缺少标题列的公告行")
                    continue

                date = cols[0].text.strip()
                title_elem = cols[1].find("a")
                title = title_elem.text.strip() if title_elem else cols[1].text.strip()
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
            *DIVIDEND_KEYWORDS,
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
                if announcement.get("exchange") == "hkex":
                    text = f"{title} {announcement.get('type', '')}".lower()
                    is_important = any(k in text for k in HK_IMPORTANT_KEYWORDS)
                elif announcement.get("exchange") == "sec":
                    from .us_announcements import IMPORTANT_FORMS

                    is_important = announcement.get("type", "").removesuffix(
                        "/A"
                    ) in IMPORTANT_FORMS or _is_dividend_announcement(announcement)
                else:
                    is_important = any(k in title for k in important_keywords)
                if is_important:
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
