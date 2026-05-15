#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财报分析管理器 - 精简版
协调财报获取、分析和集成到主工作流
"""

import json
import logging
import random
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class FinancialReportManager:
    """财报分析管理器（精简版）"""

    def _get_timezone(self, timezone_str: str):
        """获取时区对象，如果pytz不可用则返回None"""
        try:
            import pytz

            return pytz.timezone(timezone_str)
        except ImportError:
            logger.warning("pytz未安装，将使用本地时间")
            return None
        except Exception as e:
            logger.warning(f"无法加载时区{timezone_str}: {e}，使用UTC")
            try:
                import pytz

                return pytz.UTC
            except ImportError:
                return None

    def _parse_date_string(self, date_str: str):
        """解析日期字符串，支持多种格式"""
        if not date_str:
            return None

        date_str = date_str.strip()
        # 支持的日期格式（与announcement_fetcher.py保持一致）
        date_formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]

        for fmt in date_formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                # 如果格式不包含时间部分，设为当天的00:00:00
                if fmt == "%Y-%m-%d" or fmt == "%Y/%m/%d" or fmt == "%Y%m%d":
                    dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                return dt
            except ValueError:
                continue

        logger.warning(f"无法解析日期字符串: {date_str}")
        return None

    def __init__(
        self,
        config: Dict[str, Any],
        announcement_fetcher=None,
        llm_analyzer=None,
        financial_report_fetcher=None,
        content_fetcher=None,
        cache_manager=None,
    ):
        """初始化管理器"""
        self.config = config
        self.announcement_fetcher = announcement_fetcher
        self.llm_analyzer = llm_analyzer
        self.cache_manager = cache_manager

        # 读取配置
        fin_config = config.get("financial_reports", {})
        self.financial_config = fin_config
        self.force_analyze = fin_config.get("force_analyze", False)
        self.enabled = fin_config.get("enable", True)
        self.auto_enabled = fin_config.get("auto_enable", True)
        self.conditional_enabled = fin_config.get("conditional_enable", True)
        self.reports_per_stock = fin_config.get("reports_per_stock", 3)
        self.conditional_days = fin_config.get("conditional_days", 30)
        self.analysis_days = fin_config.get("analysis_days", random.randint(180, 365))
        self.max_stocks_per_run = fin_config.get("max_stocks_per_run", 3)
        self.max_force_stocks = fin_config.get("max_force_stocks")
        # 新增：是否读取财报分析缓存（默认关闭以避免旧结果掩盖代码更新效果）
        self.use_financial_analysis_cache = fin_config.get("use_analysis_cache", False)

        # 获取时区配置
        scheduler_config = config.get("scheduler", {})
        self.timezone_str = scheduler_config.get("timezone", "Asia/Shanghai")
        self.timezone = self._get_timezone(self.timezone_str)

        # 初始化财报获取器
        self.financial_report_fetcher = financial_report_fetcher
        if self.financial_report_fetcher is None and announcement_fetcher:
            # 尝试创建财报获取器
            try:
                from .financial_report_fetcher import FinancialReportFetcher

                self.financial_report_fetcher = FinancialReportFetcher(
                    config, announcement_fetcher, content_fetcher
                )
                logger.info("已自动创建财报获取器")
            except ImportError as e:
                logger.warning(f"无法导入FinancialReportFetcher: {e}")
                self.financial_report_fetcher = None

        if self.cache_manager is None:
            try:
                from ..data.cache_manager import CacheManager

                self.cache_manager = CacheManager(config)
            except Exception as e:
                logger.error(f"财报缓存管理器初始化失败: {e}")
                self.cache_manager = None

        logger.info(
            f"财报管理器初始化: enabled={self.enabled}, llm_analyzer={llm_analyzer is not None}, timezone={self.timezone_str}"
        )

    def _is_yesterday(self, date_str: str) -> bool:
        """判断日期字符串是否为昨天（时区感知）"""
        # 解析日期字符串
        report_dt = self._parse_date_string(date_str)
        if report_dt is None:
            logger.debug(f"无法解析日期字符串，不视为昨天: {date_str}")
            return False

        # 获取当前时间（时区感知）
        now = None
        try:
            import pytz

            if self.timezone is not None:
                # 尝试使用配置的时区
                try:
                    if isinstance(self.timezone, str):
                        tz = pytz.timezone(self.timezone)
                    else:
                        # 假设是时区对象
                        tz = self.timezone
                    now = datetime.now(tz)
                except Exception as tz_error:
                    logger.debug(
                        f"使用时区{self.timezone}失败: {tz_error}, 使用本地时间"
                    )

            if now is None:
                # 回退到本地时间
                now = datetime.now()

        except ImportError:
            # pytz不可用，使用本地时间
            now = datetime.now()
        except Exception as e:
            logger.warning(f"获取时区时间失败，使用本地时间: {e}")
            now = datetime.now()

        # 计算昨天（考虑时区）
        yesterday = now - timedelta(days=1)

        # 比较日期部分（年-月-日）
        report_date = report_dt.date()
        yesterday_date = yesterday.date()

        is_yesterday = report_date == yesterday_date

        # 详细日志记录（仅在调试级别）
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"日期判断: 输入='{date_str}', 解析={report_dt}, 报告日期={report_date}, "
                f"当前时间={now}, 昨天日期={yesterday_date}, 是否昨天={is_yesterday}"
            )

        return is_yesterday

    def _is_financial_report_title(self, title: str) -> bool:
        """判断公告标题是否为财务报告"""
        if not title:
            return False
        title_lower = title.lower()
        financial_keywords = [
            "年报",
            "半年报",
            "季报",
            "年度报告",
            "半年度报告",
            "季度报告",
            "财务报表",
            "财务报告",
            "审计报告",
        ]
        return any(keyword in title_lower for keyword in financial_keywords)

    def _get_stocks_with_recent_reports(self) -> List[str]:
        """获取有近期财务报告发布的股票列表（昨天发布的报告）"""
        if not self.announcement_fetcher:
            logger.warning("公告抓取器未提供，无法检查近期财务报告")
            return []

        all_stocks = self.config.get("stocks", [])
        if not all_stocks:
            return []

        # 获取最近2天的公告（包含昨天和今天）
        announcements = self.announcement_fetcher.fetch_announcements(
            all_stocks, days=2
        )
        stocks_with_reports = []

        for stock_code, stock_announcements in announcements.items():
            if not stock_announcements:
                continue

            for announcement in stock_announcements:
                title = announcement.get("title", "")
                date_str = announcement.get("date", "")

                if self._is_financial_report_title(title) and self._is_yesterday(
                    date_str
                ):
                    stocks_with_reports.append(stock_code)
                    break  # 找到一份报告即可

        logger.debug(
            f"找到{len(stocks_with_reports)}只股票有昨天发布的财务报告: {stocks_with_reports}"
        )
        return stocks_with_reports

    def _filter_stocks_with_recent_reports(
        self, stocks: List[str], days: int = 30
    ) -> List[str]:
        """过滤出有近期财务报告的股票列表"""
        if not self.announcement_fetcher or not stocks:
            return []

        # 获取指定天数内的公告
        announcements = self.announcement_fetcher.fetch_announcements(stocks, days=days)
        stocks_with_reports = []

        for stock_code, stock_announcements in announcements.items():
            if not stock_announcements:
                continue

            # 检查是否有财务报告
            has_financial_report = False
            for announcement in stock_announcements:
                title = announcement.get("title", "")
                if self._is_financial_report_title(title):
                    has_financial_report = True
                    break

            if has_financial_report:
                stocks_with_reports.append(stock_code)

        logger.debug(f"过滤出{len(stocks_with_reports)}只股票有最近{days}天的财务报告")
        return stocks_with_reports

    def should_analyze_financial_reports(
        self, alert_stocks: Optional[List[str]] = None
    ) -> Tuple[bool, List[str]]:
        """判断是否需要分析财报"""
        if not self.enabled:
            return False, []

        if alert_stocks is None:
            alert_stocks = []

        stocks_to_analyze = []

        # 强制分析模式：直接分析所有股票
        if self.force_analyze:
            all_stocks = self.config.get("stocks", [])
            if all_stocks:
                logger.info(f"强制分析模式: 将分析{len(all_stocks)}只股票")
                return True, all_stocks

        # 自动触发：检查是否有昨天发布的财务报告
        if self.auto_enabled:
            auto_stocks = self._get_stocks_with_recent_reports()
            if auto_stocks:
                stocks_to_analyze.extend(auto_stocks)
                logger.info(f"自动触发: {len(auto_stocks)}只股票有昨天发布的财务报告")

        # 条件触发：检查触发条件的股票是否有近期财务报告
        if self.conditional_enabled and alert_stocks:
            cond_stocks = self._filter_stocks_with_recent_reports(
                alert_stocks, days=self.conditional_days
            )
            if cond_stocks:
                stocks_to_analyze.extend(cond_stocks)
                logger.info(
                    f"条件触发: {len(cond_stocks)}只触发条件的股票有近期财务报告"
                )

        # 去重
        stocks_to_analyze = list(set(stocks_to_analyze))
        should_analyze = len(stocks_to_analyze) > 0

        return should_analyze, stocks_to_analyze

    def analyze_financial_reports(
        self, stocks: List[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """分析财报并返回结果"""
        if not self.enabled or not stocks:
            return {}

        logger.info(f"开始财报分析: {len(stocks)}只股票")
        results = {}

        # 限制分析数量，避免过多API调用
        if self.force_analyze:
            max_allowed = self.max_force_stocks or len(stocks)
        else:
            max_allowed = self.max_stocks_per_run or len(stocks)

        max_stocks = min(max_allowed, len(stocks))
        for stock_code in stocks[:max_stocks]:
            try:
                stock_results = self._analyze_stock(stock_code)
                if stock_results:
                    results[stock_code] = stock_results
            except Exception as e:
                logger.error(f"分析{stock_code}失败: {e}")

        logger.info(f"财报分析完成: {len(results)}只有结果")
        return results

    def _analyze_stock(self, stock_code: str) -> List[Dict[str, Any]]:
        """分析单只股票的财报"""
        # 如果没有LLM分析器或财报获取器，无法进行分析
        if not self.llm_analyzer or not self.financial_report_fetcher:
            logger.warning("LLM分析器或财报获取器不可用，跳过财报分析")
            return []

        try:
            if self.use_financial_analysis_cache and self.cache_manager:
                cached = self.cache_manager.get_financial_analysis_cache(stock_code)
                if cached:
                    reports = cached.get("reports") or cached.get("analysis")
                    if reports:
                        reports = [r for r in reports if r.get("success") is True]
                        logger.info(
                            "使用财报分析缓存: %s (date=%s)",
                            stock_code,
                            cached.get("date") or "today",
                        )
                        if reports:
                            return reports

            # 使用财报获取器获取并分析报告
            analysis_results = self.financial_report_fetcher.analyze_financial_reports(
                [stock_code],
                self.llm_analyzer,
                days=self.analysis_days,
                report_type=None,  # 所有类型
            )

            # 调试日志：记录返回的analysis_results完整结构
            logger.info(
                "DEBUG: analyze_financial_reports 返回后, analysis_results类型=%s, keys=%s (键类型: %s), 各key长度=%s, 各key列表id=%s",
                type(analysis_results),
                list(analysis_results.keys()),
                [type(k) for k in analysis_results.keys()],
                {k: len(v) for k, v in analysis_results.items()},
                {k: id(v) for k, v in analysis_results.items()},
            )

            # 尝试多种键类型获取分析结果
            stock_analysis = analysis_results.get(stock_code, [])
            if not stock_analysis:
                # 如果未找到，尝试将stock_code转换为字符串（因为analysis_results键可能是字符串）
                str_key = (
                    str(stock_code) if not isinstance(stock_code, str) else stock_code
                )
                if str_key in analysis_results:
                    stock_analysis = analysis_results[str_key]
                    logger.info("DEBUG: 使用字符串键找到分析结果: %s", str_key)
                # 如果仍为空，尝试整数键（如果stock_code是字符串且可转换为数字）
                elif isinstance(stock_code, str) and stock_code.isdigit():
                    int_key = int(stock_code)
                    if int_key in analysis_results:
                        stock_analysis = analysis_results[int_key]  # type: ignore
                        logger.info("DEBUG: 使用整数键找到分析结果: %s", int_key)

            # 记录analysis_results接收详情
            logger.info(
                "analysis_results接收: stock=%s (类型: %s) analysis_results.keys=%s stock_analysis长度=%s, stock_analysis id=%s",
                stock_code,
                type(stock_code),
                list(analysis_results.keys()),
                len(stock_analysis),
                id(stock_analysis),
            )
            if stock_analysis and len(stock_analysis) > 0:
                first_analysis = stock_analysis[0]
                logger.info(
                    "  首个分析结果: success=%s numeric_fields=%s 关键字段=%s",
                    first_analysis.get("success"),
                    first_analysis.get("numeric_fields_detected", 0),
                    list(
                        k
                        for k in [
                            "cost_structure_analysis",
                            "profit_competitiveness_analysis",
                            "liquidation_value_analysis",
                            "audit_risk_insights",
                            "overall_assessment",
                        ]
                        if k in first_analysis and first_analysis[k]
                    ),
                )
            elif not stock_analysis:
                logger.info("  无分析结果: stock_analysis为空列表或None")

            if (
                not stock_analysis
                and self.use_financial_analysis_cache
                and self.cache_manager
            ):
                cache_dir = getattr(
                    self.cache_manager, "financial_analysis_cache_dir", None
                )
                if cache_dir and cache_dir.exists():
                    for cache_file in sorted(
                        cache_dir.glob(f"{stock_code}_*.json"), reverse=True
                    ):
                        try:
                            with open(cache_file, "r", encoding="utf-8") as f:
                                cached = json.load(f)
                            reports = cached.get("reports") or cached.get("analysis")
                            if reports:
                                logger.info(
                                    "使用最近缓存财报分析: %s (file=%s)",
                                    stock_code,
                                    cache_file.name,
                                )
                                return reports
                        except Exception as e:
                            logger.warning(f"财报缓存 {stock_code} 加载失败: {e}")
                            continue
            if not stock_analysis:
                logger.warning(f"股票 {stock_code} 没有找到可分析的财报")
                return []

            # 强制缓存原始分析结果，便于追踪/复用（即便后续格式化失败）
            if self.cache_manager:
                try:
                    cache_dir = self.cache_manager.financial_analysis_cache_dir
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    today_str = datetime.now().strftime("%Y%m%d")
                    raw_file = cache_dir / f"{stock_code}_{today_str}_raw.json"
                    with open(raw_file, "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "stock_code": stock_code,
                                "date": today_str,
                                "raw_analysis_results": stock_analysis,
                            },
                            f,
                            ensure_ascii=False,
                            indent=2,
                        )
                    logger.info("已写入财报原始分析缓存: %s", raw_file)
                except Exception as raw_cache_error:
                    logger.warning(
                        "写入财报原始分析缓存失败 %s: %s", stock_code, raw_cache_error
                    )

            # 转换分析结果格式以匹配电子邮件模板
            formatted_reports = []
            for analysis_result in stock_analysis[
                : self.reports_per_stock
            ]:  # 限制报告数量
                formatted_report = self._format_analysis_for_email(
                    stock_code, analysis_result
                )
                if formatted_report:
                    formatted_reports.append(formatted_report)

            # 如果存在原始分析但全部格式化失败，生成降级提示，避免结果缺失
            if not formatted_reports and stock_analysis:
                first = stock_analysis[0]
                fallback = {
                    "stock_code": stock_code,
                    "report_type": first.get("report_type", "unknown"),
                    "period_date": first.get("period_date", ""),
                    "analysis": {
                        "overall_assessment": f"财报分析失败或数据不足: {first.get('error', '原因未知')}"
                    },
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "success": False,
                }
                formatted_reports.append(fallback)
                logger.warning("格式化财报分析失败，已生成降级提示: %s", stock_code)

            if self.cache_manager and formatted_reports:
                try:
                    first = formatted_reports[0] if formatted_reports else {}
                    content_hash = first.get("content_hash")
                    self.cache_manager.set_financial_analysis_cache(
                        stock_code, formatted_reports, content_hash=content_hash
                    )
                except Exception as cache_error:
                    logger.warning(
                        "缓存财报分析结果失败 %s: %s", stock_code, cache_error
                    )

            logger.info(f"股票 {stock_code} 成功分析 {len(formatted_reports)} 份财报")
            return formatted_reports

        except Exception as e:
            logger.error(f"分析股票 {stock_code} 财报失败: {e}")
            # 分析失败时返回空结果
            return []

    def _format_analysis_for_email(
        self, stock_code: str, analysis_result: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """将LLM分析结果格式化为电子邮件需要的格式"""
        try:
            # 提取报告元数据
            report_type = analysis_result.get("report_type", "unknown")
            period_date = analysis_result.get("period_date", "")
            report_metadata = analysis_result.get("report_metadata") or {}
            content_hash = (
                report_metadata.get("content_hash")
                if isinstance(report_metadata, dict)
                else None
            )

            # 调试日志：记录分析结果中的可用字段
            available_fields = list(analysis_result.keys())
            logger.info(
                "格式化分析结果: 股票=%s, 报告类型=%s, 可用字段=%s",
                stock_code,
                report_type,
                available_fields,
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"格式化分析结果: 股票={stock_code}, 报告类型={report_type}, "
                    f"可用字段={available_fields}"
                )

            # 提取分析结果（注意字段名与financial_report_analyzer.py保持一致）
            cost_analysis = analysis_result.get("cost_structure_analysis", {})
            profit_analysis = analysis_result.get("profit_competitiveness_analysis", {})
            liquidation_analysis = analysis_result.get("liquidation_value_analysis", {})
            audit_insights = analysis_result.get("audit_risk_insights", {})
            overall_assessment = analysis_result.get("overall_assessment", {})

            # 检查字段匹配情况
            expected_fields = [
                "cost_structure_analysis",
                "profit_competitiveness_analysis",
                "liquidation_value_analysis",
                "audit_risk_insights",
                "overall_assessment",
            ]
            missing_fields = [
                field for field in expected_fields if field not in analysis_result
            ]
            if missing_fields:
                logger.info("字段匹配检查: 缺失字段=%s", missing_fields)

            # 记录提取的数据状态
            logger.info(
                "提取分析数据: 股票=%s, "
                "cost_analysis存在=%s, "
                "profit_analysis存在=%s, "
                "liquidation_analysis存在=%s, "
                "audit_insights存在=%s, "
                "overall_assessment存在=%s",
                stock_code,
                bool(cost_analysis),
                bool(profit_analysis),
                bool(liquidation_analysis),
                bool(audit_insights),
                bool(overall_assessment),
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"提取分析数据: 股票={stock_code}, "
                    f"cost_analysis存在={bool(cost_analysis)}, 类型={type(cost_analysis)}, "
                    f"profit_analysis存在={bool(profit_analysis)}, 类型={type(profit_analysis)}, "
                    f"liquidation_analysis存在={bool(liquidation_analysis)}, 类型={type(liquidation_analysis)}, "
                    f"audit_insights存在={bool(audit_insights)}, 类型={type(audit_insights)}, "
                    f"overall_assessment存在={bool(overall_assessment)}, 类型={type(overall_assessment)}"
                )
                # 记录每个分析数据的键（如果是字典）
                for name, data in [
                    ("cost_analysis", cost_analysis),
                    ("profit_analysis", profit_analysis),
                    ("liquidation_analysis", liquidation_analysis),
                    ("audit_insights", audit_insights),
                    ("overall_assessment", overall_assessment),
                ]:
                    if isinstance(data, dict):
                        logger.debug(f"  {name} 键: {list(data.keys())}")

            # 转换为电子邮件格式；若分析失败/短路，输出明确原因并附字段名
            short_circuited = analysis_result.get("short_circuited", False)
            if analysis_result.get("success", True) and not short_circuited:
                email_analysis = {
                    "cost_structure": self._extract_summary_from_analysis(
                        cost_analysis, "成本结构"
                    ),
                    "profit_changes": self._extract_summary_from_analysis(
                        profit_analysis, "利润变化"
                    ),
                    "liquidation_value": self._extract_summary_from_analysis(
                        liquidation_analysis, "清算价值"
                    ),
                    "audit_risks": self._extract_summary_from_analysis(
                        audit_insights, "审计风险"
                    ),
                    "overall_assessment": self._extract_summary_from_analysis(
                        overall_assessment, "总体评估"
                    ),
                }
            else:
                reason = analysis_result.get("error") or "分析失败，原因未知"
                numeric_fields = analysis_result.get("numeric_fields_detected")
                field_names = analysis_result.get("numeric_field_names") or []
                detail = (
                    f"(字段 {numeric_fields}): {', '.join(field_names[:15])}"
                    if numeric_fields is not None
                    else ""
                )
                email_analysis = {
                    "overall_assessment": f"财报分析未完成：{reason} {detail}"
                }

            # 构建报告字典
            report = {
                "stock_code": stock_code,
                "report_type": report_type,
                "period_date": period_date,
                "analysis": email_analysis,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "success": analysis_result.get("success", True),
                "short_circuited": short_circuited,
            }

            numeric_fields = analysis_result.get("numeric_fields_detected")
            if numeric_fields is not None:
                report["numeric_fields_detected"] = numeric_fields

            if content_hash:
                report["content_hash"] = content_hash
            if report_metadata:
                report["report_metadata"] = report_metadata

            # 调试日志：记录最终生成的报告
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"生成报告: 股票={stock_code}, 报告类型={report_type}, "
                    f"分析字段数={len(email_analysis)}"
                )

            return report

        except Exception as e:
            logger.error(f"格式化分析结果失败: {e}")
            return None

    def _extract_summary_from_analysis(
        self, analysis_data: Dict[str, Any], default_prefix: str
    ) -> str:
        """从分析数据中提取摘要文本"""
        if not analysis_data:
            return f"{default_prefix}: 无分析数据"

        # 尝试提取关键信息
        if isinstance(analysis_data, dict):
            # 检查是否包含success字段的分析结果结构
            if analysis_data.get("success") is False:
                error = analysis_data.get("error", "未知错误")
                return f"{default_prefix}分析失败: {error}"

            # 尝试从嵌套的分析结果中提取信息
            # 查找可能的嵌套分析字段（如cost_structure_analysis, overall_assessment等）
            nested_fields = [
                "cost_structure_analysis",
                "profit_competitiveness_analysis",
                "liquidation_value_analysis",
                "audit_risk_insights",
                "overall_assessment",
            ]

            for field in nested_fields:
                if field in analysis_data:
                    nested_data = analysis_data[field]
                    if isinstance(nested_data, dict):
                        # 如果有summary字段，使用它
                        if "summary" in nested_data:
                            return f"{default_prefix}: {nested_data['summary']}"
                        if "liquidation_analysis" in nested_data:
                            la = nested_data.get("liquidation_analysis", {})
                            if isinstance(la, dict):
                                if la.get("summary"):
                                    return f"{default_prefix}: {la['summary']}"
                                if "fair_value_per_share" in la:
                                    fv = la.get("fair_value_per_share")
                                    sm = la.get("safety_margin")
                                    method = la.get("method", "DCF")
                                    return (
                                        f"{default_prefix}: {method} 每股内在价值={fv}, "
                                        f"安全边际={sm or '未知'}"
                                    )
                        # DCF/估值特有字段
                        if "fair_value_per_share" in nested_data:
                            fv = nested_data.get("fair_value_per_share")
                            sm = nested_data.get("safety_margin")
                            method = nested_data.get("method", "DCF")
                            return (
                                f"{default_prefix}: {method} 每股内在价值={fv}, "
                                f"安全边际={sm or '未知'}"
                            )
                        # 尝试提取其他关键信息
                        elif (
                            "key_findings" in nested_data
                            and nested_data["key_findings"]
                        ):
                            return f"{default_prefix}: {', '.join(nested_data['key_findings'][:3])}"
                        elif "change_assessment" in nested_data:
                            return (
                                f"{default_prefix}: {nested_data['change_assessment']}"
                            )
                        elif "risk_level" in nested_data:
                            return f"{default_prefix}: 风险等级={nested_data['risk_level']}"
                        elif "investment_recommendation" in nested_data:
                            return f"{default_prefix}: {nested_data['investment_recommendation']}"

            # 如果analysis_data本身有summary字段（非嵌套情况）
            if "summary" in analysis_data:
                return f"{default_prefix}: {analysis_data['summary']}"
            if "fair_value_per_share" in analysis_data:
                fv = analysis_data.get("fair_value_per_share")
                sm = analysis_data.get("safety_margin")
                method = analysis_data.get("method", "DCF")
                return f"{default_prefix}: {method} 每股内在价值={fv}, 安全边际={sm or '未知'}"

            # 最后手段：转换为字符串表示
            return f"{default_prefix}: {str(analysis_data)[:200]}..."

        # 如果是字符串，直接返回
        if isinstance(analysis_data, str):
            return f"{default_prefix}: {analysis_data[:200]}"

        # 其他类型
        return f"{default_prefix}: {str(analysis_data)[:200]}..."

    def get_status(self) -> Dict[str, Any]:
        """获取管理器状态"""
        return {
            "enabled": self.enabled,
            "auto_enabled": self.auto_enabled,
            "conditional_enabled": self.conditional_enabled,
            "reports_per_stock": self.reports_per_stock,
        }
