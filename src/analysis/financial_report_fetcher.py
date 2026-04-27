#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财报数据获取模块
从上市公司公告中获取财务报告（年报、半年报、季报）并提取文本内容
"""

import copy
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class FinancialReportFetcher:
    """财报数据获取器"""

    def __init__(self, config, announcement_fetcher=None, content_fetcher=None):
        """
        初始化财报获取器

        Args:
            config: 配置字典
            announcement_fetcher: 可选的公告抓取器实例
            content_fetcher: 可选的内容抓取器实例
        """
        self.config = config
        self.announcement_fetcher = announcement_fetcher
        self.content_fetcher = content_fetcher

        # 财报类型配置
        self.report_types = {
            "annual": ["年报", "年度报告"],
            "semiannual": ["半年报", "半年度报告", "中期报告"],
            "quarterly": [
                "季报",
                "季度报告",
                "一季度报告",
                "二季度报告",
                "三季度报告",
                "四季度报告",
            ],
        }

        # 财报关键词（用于筛选公告）
        self.report_keywords = []
        for keywords in self.report_types.values():
            self.report_keywords.extend(keywords)

        # 缓存最近获取的财报，避免重复处理
        self._recent_reports_cache = {}

    def fetch_financial_reports(
        self,
        stock_codes: List[str],
        days: int = 365,
        report_type: Optional[str] = None,
        max_reports_per_stock: int = 5,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        获取指定股票的财务报告

        Args:
            stock_codes: 股票代码列表
            days: 获取最近多少天的公告（默认365天，因为财报发布频率较低）
            report_type: 指定报告类型 ('annual', 'semiannual', 'quarterly')，为None时获取所有类型
            max_reports_per_stock: 每只股票最大报告数量

        Returns:
            dict: 按股票代码组织的财报列表，每个财报包含：
                {
                    'stock_code': str,
                    'title': str,
                    'date': str,
                    'report_type': str,
                    'period_date': str,  # 报告期间（如2024-12-31）
                    'url': str,
                    'content_text': str,  # 提取的文本内容（如果成功）
                    'content_hash': str,
                    'success': bool,
                    'error': Optional[str]
                }
        """
        if not self.announcement_fetcher:
            logger.error("公告抓取器未提供，无法获取财报")
            return {code: [] for code in stock_codes}

        logger.info(
            f"开始获取财报数据: {len(stock_codes)}只股票, 最近{days}天, 报告类型: {report_type or '全部'}"
        )

        # 获取公告
        all_announcements = self.announcement_fetcher.fetch_announcements(
            stock_codes, days=days
        )

        # 筛选财报公告
        financial_reports = {}
        for stock_code, announcements in all_announcements.items():
            stock_reports = self._filter_financial_reports(
                stock_code, announcements, report_type, max_reports_per_stock
            )
            financial_reports[stock_code] = stock_reports

        # 获取财报内容（如果配置了内容抓取器）
        if self.content_fetcher:
            financial_reports = self._fetch_report_contents(financial_reports)

        # 统计结果
        total_reports = sum(len(reports) for reports in financial_reports.values())
        total_with_content = sum(
            1
            for reports in financial_reports.values()
            for report in reports
            if report.get("content_text")
        )
        logger.info(
            f"财报获取完成: 共找到{total_reports}份财报，其中{total_with_content}份成功获取内容"
        )

        return financial_reports

    def _filter_financial_reports(
        self,
        stock_code: str,
        announcements: List[Dict[str, Any]],
        report_type: Optional[str],
        max_reports_per_stock: int,
    ) -> List[Dict[str, Any]]:
        """从公告列表中筛选财务报告"""
        financial_reports = []

        for announcement in announcements:
            title = announcement.get("title", "")
            date = announcement.get("date", "")
            url = announcement.get("url", "")

            # 检查是否为财报公告
            is_financial_report, detected_type = self._is_financial_report(
                title, report_type
            )
            if not is_financial_report:
                continue

            # 尝试从标题中提取报告期间
            period_date = self._extract_period_date(title, date, detected_type)

            report = {
                "stock_code": stock_code,
                "title": title,
                "date": date,
                "report_type": detected_type,
                "period_date": period_date,
                "url": url,
                "content_text": "",  # 稍后获取
                "content_hash": "",
                "success": False,
                "error": None,
            }

            financial_reports.append(report)

            if len(financial_reports) >= max_reports_per_stock:
                break

        # 按日期排序（最新的在前）
        financial_reports.sort(key=lambda x: x["date"], reverse=True)

        logger.debug(f"股票 {stock_code} 找到 {len(financial_reports)} 份财报")
        return financial_reports

    def _is_financial_report(
        self, title: str, target_report_type: Optional[str]
    ) -> Tuple[bool, Optional[str]]:
        """判断公告是否为财务报告，并返回报告类型"""
        title_lower = title.lower()

        for report_type, keywords in self.report_types.items():
            # 如果指定了报告类型，只检查该类型
            if target_report_type and report_type != target_report_type:
                continue

            for keyword in keywords:
                if keyword in title_lower:
                    return True, report_type

        return False, None

    def _extract_period_date(
        self, title: str, announcement_date: str, report_type: str
    ) -> str:
        """
        从标题中提取报告期间
        例如："2024年年度报告" -> "2024-12-31"
        """
        import re

        # 尝试从标题中提取年份
        year_patterns = [
            r"(\d{4})年",  # 2024年
            r"(\d{4})年度",  # 2024年度
            r"(\d{4})[-/](\d{2})[-/](\d{2})",  # 2024-12-31
        ]

        for pattern in year_patterns:
            match = re.search(pattern, title)
            if match:
                if len(match.groups()) == 1:
                    year = match.group(1)
                    # 根据报告类型设置默认日期
                    if report_type == "annual":
                        return f"{year}-12-31"
                    elif report_type == "semiannual":
                        # 假设半年报在6月30日
                        return f"{year}-06-30"
                    elif report_type == "quarterly":
                        # 季度报告，需要更多信息
                        # 尝试提取季度
                        quarter_match = re.search(r"[第]?([一二三四1234])季度", title)
                        if quarter_match:
                            quarter = quarter_match.group(1)
                            quarter_map = {
                                "一": "03-31",
                                "二": "06-30",
                                "三": "09-30",
                                "四": "12-31",
                                "1": "03-31",
                                "2": "06-30",
                                "3": "09-30",
                                "4": "12-31",
                            }
                            if quarter in quarter_map:
                                return f"{year}-{quarter_map[quarter]}"
                        # 默认使用季度末
                        return f"{year}-03-31"
                elif len(match.groups()) == 3:
                    # 完整的日期
                    year, month, day = match.groups()
                    return f"{year}-{month}-{day}"

        # 无法从标题提取，使用公告日期作为近似值
        try:
            # 尝试解析公告日期，支持多种格式
            date_obj = None
            date_str = announcement_date.strip()
            date_formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]

            for fmt in date_formats:
                try:
                    date_obj = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue

            if date_obj is None:
                raise ValueError(f"无法解析日期字符串: {announcement_date}")

            # 根据报告类型调整
            if report_type == "annual":
                # 年报通常报告上一年度
                return f"{date_obj.year - 1}-12-31"
            elif report_type == "semiannual":
                # 半年报报告上半年度
                if date_obj.month <= 6:
                    return f"{date_obj.year - 1}-12-31"
                else:
                    return f"{date_obj.year}-06-30"
            elif report_type == "quarterly":
                # 季度报告
                if date_obj.month <= 3:
                    return f"{date_obj.year - 1}-12-31"
                elif date_obj.month <= 6:
                    return f"{date_obj.year}-03-31"
                elif date_obj.month <= 9:
                    return f"{date_obj.year}-06-30"
                else:
                    return f"{date_obj.year}-09-30"
        except (ValueError, TypeError) as e:
            logger.debug(f"无法解析公告日期{announcement_date}: {e}")
            pass

        # 最后手段：使用公告日期
        return announcement_date

    def _fetch_report_contents(
        self, financial_reports: Dict[str, List[Dict[str, Any]]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """获取财报内容文本"""
        for stock_code, reports in financial_reports.items():
            for report in reports:
                if report.get("content_text"):
                    # 已有内容，跳过
                    continue

                url = report.get("url")
                date = report.get("date")
                if not url or not date:
                    report["error"] = "缺少URL或日期"
                    continue

                try:
                    content_result = self.content_fetcher.fetch_content(
                        url, stock_code, date
                    )

                    if content_result and content_result.get("success", False):
                        report["content_text"] = content_result.get(
                            "extracted_text", ""
                        )
                        report["content_hash"] = content_result.get("content_hash", "")
                        report["success"] = True
                        logger.debug(
                            f"成功获取财报内容: {stock_code} {report['report_type']} {report['period_date']}"
                        )
                    else:
                        error_msg = (
                            content_result.get("error", "未知错误")
                            if content_result
                            else "获取失败"
                        )
                        report["error"] = f"内容获取失败: {error_msg}"
                        logger.warning(
                            f"获取财报内容失败: {stock_code} {report['report_type']} {report['period_date']}: {error_msg}"
                        )

                except Exception as e:
                    report["error"] = f"内容获取异常: {e}"
                    logger.error(
                        f"获取财报内容异常: {stock_code} {report['report_type']} {report['period_date']}: {e}"
                    )

        return financial_reports

    def get_latest_financial_report(
        self, stock_code: str, report_type: Optional[str] = None, days: int = 365
    ) -> Optional[Dict[str, Any]]:
        """
        获取指定股票的最新财务报告

        Args:
            stock_code: 股票代码
            report_type: 报告类型
            days: 查找天数

        Returns:
            dict: 最新财报，如果没有找到返回None
        """
        reports = self.fetch_financial_reports(
            [stock_code], days=days, report_type=report_type, max_reports_per_stock=1
        )

        stock_reports = reports.get(stock_code, [])
        if stock_reports:
            return stock_reports[0]
        return None

    def analyze_financial_reports(
        self,
        stock_codes: List[str],
        llm_analyzer,
        days: int = 365,
        report_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取并分析财务报告（一站式方法）

        Args:
            stock_codes: 股票代码列表
            llm_analyzer: LLM分析器实例（需支持analyze_financial_report方法）
            days: 查找天数
            report_type: 报告类型

        Returns:
            dict: 分析结果
        """
        # 获取财报
        financial_reports = self.fetch_financial_reports(
            stock_codes, days=days, report_type=report_type
        )

        analysis_results = {}
        for stock_code, reports in financial_reports.items():
            # 仅保留最长且非“摘要”优先的报告，避免短版拖低字段
            def _sort_key(r):
                title = (r.get("title") or "").lower()
                is_summary = any(
                    key in title for key in ["摘要", "summary", "简报", "简要"]
                )
                content_len = len(r.get("content_text") or "")
                return (is_summary, -content_len)

            reports = sorted(reports, key=_sort_key)
            if reports:
                # 记录筛选详情
                filtered_reports = []
                for i, report in enumerate(reports):
                    title = report.get("title") or ""
                    title_lower = title.lower()
                    is_summary = any(
                        key in title_lower
                        for key in ["摘要", "summary", "简报", "简要"]
                    )
                    success = report.get("success", False)
                    content_len = len(report.get("content_text") or "")
                    filtered_reports.append(
                        {
                            "index": i,
                            "title": title[:50] + "..." if len(title) > 50 else title,
                            "is_summary": is_summary,
                            "success": success,
                            "content_len": content_len,
                        }
                    )

                chosen = reports[0]
                logger.info(
                    "财报筛选详情: stock=%s 总报告数=%s",
                    stock_code,
                    len(reports),
                )
                for fr in filtered_reports[:3]:  # 最多显示前3个报告
                    logger.info(
                        "  报告[%s]: title=%s is_summary=%s success=%s len=%s",
                        fr["index"],
                        fr["title"],
                        fr["is_summary"],
                        fr["success"],
                        fr["content_len"],
                    )
                if len(filtered_reports) > 3:
                    logger.info("  ... 还有%s个报告未显示", len(filtered_reports) - 3)

                logger.info(
                    "财报选择结果: stock=%s chosen_title=%s len=%s success=%s is_summary=%s",
                    stock_code,
                    chosen.get("title"),
                    len(chosen.get("content_text") or ""),
                    chosen.get("success", False),
                    any(
                        key in (chosen.get("title") or "").lower()
                        for key in ["摘要", "summary", "简报", "简要"]
                    ),
                )
                reports = [chosen]

            stock_analysis = []
            skip_count = 0
            for report in reports:
                if not report.get("success") or not report.get("content_text"):
                    # 没有内容，跳过分析
                    skip_count += 1
                    logger.info(
                        "跳过分析: stock=%s title=%s success=%s content_len=%s 原因=%s",
                        stock_code,
                        report.get("title"),
                        report.get("success"),
                        len(report.get("content_text") or ""),
                        "success=False"
                        if not report.get("success")
                        else "content_text为空",
                    )
                    continue

                try:
                    # 调用LLM分析财报
                    logger.info(
                        "LLM分析器调用: stock=%s report_type=%s title=%s content_len=%s",
                        stock_code,
                        report["report_type"],
                        report["title"][:80] + "..."
                        if len(report["title"]) > 80
                        else report["title"],
                        len(report["content_text"]),
                    )

                    analysis_result = llm_analyzer.analyze_financial_report(
                        stock_code=stock_code,
                        report_text=report["content_text"],
                        report_type=report["report_type"],
                        period_date=report["period_date"],
                        report_title=report["title"],
                    )

                    # 记录分析结果关键字段
                    success = analysis_result.get("success", False)
                    numeric_fields = analysis_result.get("numeric_fields_detected", 0)
                    analysis_fields = [
                        "cost_structure_analysis",
                        "profit_competitiveness_analysis",
                        "liquidation_value_analysis",
                        "audit_risk_insights",
                        "overall_assessment",
                    ]
                    filled_fields = sum(
                        1 for field in analysis_fields if analysis_result.get(field)
                    )

                    logger.info(
                        "LLM分析结果: stock=%s success=%s numeric_fields=%s analysis_fields_filled=%s/%s",
                        stock_code,
                        success,
                        numeric_fields,
                        filled_fields,
                        len(analysis_fields),
                    )

                    if success and numeric_fields >= 15:
                        logger.info(
                            "分析结果验证: 满足年报分析条件 (numeric_fields≥15)"
                        )
                    elif success:
                        logger.info(
                            "分析结果验证: 成功但数值字段不足 (numeric_fields=%s)",
                            numeric_fields,
                        )
                    else:
                        logger.info("分析结果验证: 分析失败")

                    analysis_result["report_metadata"] = {
                        "title": report["title"],
                        "date": report["date"],
                        "url": report["url"],
                        "content_hash": report["content_hash"],
                    }

                    logger.info(
                        "DEBUG: 准备添加分析结果到stock_analysis, 当前长度=%s",
                        len(stock_analysis),
                    )
                    stock_analysis.append(analysis_result)
                    logger.info(
                        "DEBUG: 已添加分析结果, 新的长度=%s, analysis_result keys=%s",
                        len(stock_analysis),
                        list(analysis_result.keys())[:10],
                    )

                except Exception as e:
                    logger.error(f"分析财报失败 {stock_code} {report['title']}: {e}")

            if skip_count > 0:
                logger.info(
                    "报告跳过统计: stock=%s 总报告数=%s 跳过数=%s 分析结果数=%s",
                    stock_code,
                    len(reports),
                    skip_count,
                    len(stock_analysis),
                )

            # 如果存在报告但未生成任何分析结果，写入占位以便上层感知并告警
            if reports and not stock_analysis:
                placeholder = {
                    "success": False,
                    "error": "LLM分析未返回结果或被跳过",
                    "report_type": reports[0].get("report_type", "unknown"),
                    "period_date": reports[0].get("period_date", ""),
                    "report_metadata": {
                        "title": reports[0].get("title", ""),
                        "date": reports[0].get("date", ""),
                        "url": reports[0].get("url", ""),
                        "content_hash": reports[0].get("content_hash", ""),
                    },
                }
                logger.info(
                    "DEBUG: 准备添加占位符到stock_analysis, 当前长度=%s",
                    len(stock_analysis),
                )
                stock_analysis.append(placeholder)
                logger.info("DEBUG: 已添加占位符, 新的长度=%s", len(stock_analysis))
                logger.warning(
                    "未生成财报分析结果，已写入占位: %s (%s)",
                    stock_code,
                    placeholder.get("report_type"),
                )

            logger.info(
                "DEBUG: 最终stock_analysis长度=%s, 准备赋值给analysis_results[%s]",
                len(stock_analysis),
                stock_code,
            )
            logger.info(
                "DEBUG: 复制前stock_analysis id=%s, 第一个元素id=%s",
                id(stock_analysis),
                id(stock_analysis[0]) if stock_analysis else None,
            )
            analysis_results[stock_code] = copy.deepcopy(stock_analysis)
            logger.info(
                "DEBUG: 复制后analysis_results[%s] id=%s, 长度=%s",
                stock_code,
                id(analysis_results[stock_code]),
                len(analysis_results[stock_code]),
            )

        # 最终返回前的调试日志
        logger.info(
            "DEBUG: analyze_financial_reports 返回前, analysis_results keys=%s, 各key长度=%s",
            list(analysis_results.keys()),
            {k: len(v) for k, v in analysis_results.items()},
        )
        return analysis_results
