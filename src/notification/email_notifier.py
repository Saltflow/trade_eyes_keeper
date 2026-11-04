"""
邮件通知模块
发送股票提醒邮件
"""

from __future__ import annotations

import logging
import platform
import smtplib
import socket
import ssl
import subprocess
from datetime import datetime
from email import policy
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import numpy as np
import pandas as pd

from ..markets import _detect_fine_group
from ..utils.in_kind_reporting import render_in_kind_ledger
from .base import BaseNotifier
from .receipts import combine_receipts, legacy_tuple, normalize_receipt
from .report_builders import (  # noqa: F401
    SIGNAL_NAMES,
    _alert_value,
    _benchmark_label,
    _build_optimizer_run_summary,
    _build_signal_label_map,
    _fmt,
    _format_benchmark_comparison,
    _html_escape,
    _readable_signal,
    _safe_html_url,
    _signed_text_metric,
    build_brief_entries,
    build_daily_event_rows,
    build_optimizer_summary,
    build_strategy_suggestions,
    build_strategy_text_summary,
    group_daily_event_rows,
    optimizer_notification_title,
    pick_best_anchor,
)
from .report_policy import (
    PRICE_COLORS,
    ROE_ESTIMATE_NOTE,
    format_roe,
    fundamental_rows,
    generate_return_curve_png,
    implied_ke,
    price_movement,
    report_view,
    return_curves_html,
    returns_only,
    technical_signals_enabled,
)
from .settings import channel_enabled

logger = logging.getLogger(__name__)


def generate_combined_chart(*args, **kwargs):
    """Load the plotting backend only when a report needs a chart."""
    from .chart_generator import generate_combined_chart as render

    return render(*args, **kwargs)


def generate_portfolio_overview_chart(*args, **kwargs):
    from .chart_generator import generate_portfolio_overview_chart as render

    return render(*args, **kwargs)


def generate_daily_pdf(*args, **kwargs):
    from .daily_pdf import generate_daily_pdf as render

    return render(*args, **kwargs)


class EmailNotifier(BaseNotifier):
    """邮件通知器"""

    def __init__(self, config):
        """
        初始化邮件通知器

        Args:
            config: 配置字典
        """
        self.config = config
        self.email_config = config.get("email", {})

        # SMTP服务器配置 (从 config.yaml 读取)
        self.smtp_server = self.email_config.get("smtp_server", "")
        self.smtp_port = self.email_config.get("smtp_port", 465)
        self.sender_email = self.email_config.get("sender_email", "")
        self.sender_password = self.email_config.get("sender_password", "")
        self.receiver_email = self.email_config.get("receiver_email", "")
        self.enable_tls = self.email_config.get("enable_tls", False)
        self.enable_ssl = self.email_config.get("enable_ssl", True)

        # 邮件副本配置：使用项目根目录的绝对路径避免工作目录漂移
        archive_dir_config = self.email_config.get("archive_dir", "data/email_archive")
        archive_dir_path = Path(archive_dir_config)
        if not archive_dir_path.is_absolute():
            project_root = Path(__file__).resolve().parent.parent.parent
            archive_dir_path = (project_root / archive_dir_config).resolve()

        self.email_archive_dir = archive_dir_path
        self.email_archive_dir.mkdir(parents=True, exist_ok=True)
        logger.info("邮件副本目录已初始化为: %s", self.email_archive_dir)

        if not self.sender_email or not self.sender_password or not self.receiver_email:
            logger.warning("邮件配置不完整，邮件通知功能可能无法正常工作")

    def send_from_session(self, session):
        """
        从Session读取数据并发送邮件（新数据流）

        Args:
            session: SessionContext对象
        """
        session = report_view(session, self.config)
        curves_only = returns_only(self.config) or not technical_signals_enabled(
            self.config
        )
        try:
            # 从Session读取所有数据
            alert_stocks = [] if curves_only else session.get_alerts_as_dicts()
            stock_data = session.get_all_dataframe()
            announcements = session.announcements
            # 历史数据（由 data_fetcher 暂存，供图表使用）
            historical_data = getattr(session, "_historical", {})

            # 生成走势图表（PNG bytes + CID 内嵌，兼容 Gmail）
            chart_png_bytes = None
            if not curves_only:
                _, chart_png_bytes = generate_combined_chart(
                    historical_data=historical_data,
                    alerts=alert_stocks,
                    stock_data=stock_data,
                    trading_days=60,
                )

            # 构建邮件主题
            subject = f"股票日报 - {datetime.now().astimezone().strftime('%Y-%m-%d')}"

            # 获取投资组合策略结果（唯一收口）
            evaluation_reports = getattr(session, "evaluation_reports", None)

            # 日报只嵌入一张归一化三市场总览图，避免三张纵向大图淹没正文。
            portfolio_chart_dict = None
            if curves_only or evaluation_reports:
                try:
                    overview_png = (
                        generate_return_curve_png(
                            getattr(session, "strategy_return_curves", None)
                        )
                        if curves_only
                        else generate_portfolio_overview_chart(evaluation_reports)
                    )
                    portfolio_chart_dict = (
                        {"overview": overview_png} if overview_png else None
                    )
                    n_charts = len(portfolio_chart_dict) if portfolio_chart_dict else 0
                    logger.info(
                        f"投资组合图表生成: {n_charts}张"
                        if n_charts
                        else "投资组合图表跳过"
                    )
                except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
                    logger.error(f"投资组合图表生成失败: {e}")

            # 获取策略信号扫描结果
            signal_scan = getattr(session, "signal_scan", None)
            backtest = getattr(session, "backtest", None)

            # 生成日报 PDF 附件
            pdf_bytes = None
            try:
                pdf_bytes = self._generate_daily_pdf(
                    session,
                    alert_stocks,
                    signal_scan,
                    backtest,
                    stock_data,
                )
                if pdf_bytes:
                    logger.info("日报 PDF 生成成功 (%d bytes)", len(pdf_bytes))
            except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
                logger.warning("日报 PDF 生成失败: %s", e)

            # 构建邮件内容（精简正文）
            body = self._build_email_body(
                alert_stocks,
                stock_data,
                announcements,
                chart_png_bytes=chart_png_bytes,
                portfolio_chart_dict=portfolio_chart_dict,
                signal_scan=signal_scan,
                backtest=backtest,
                evaluation_reports=evaluation_reports,
                placements=getattr(session, "placements", None),
                dividend_events=getattr(session, "dividend_events", None),
                strategy_return_curves=getattr(session, "strategy_return_curves", None),
                report_as_of=getattr(session, "created_at", None),
            )

            # 发送邮件（PDF 作为附件）
            receipt = self._send_email(
                subject,
                body,
                chart_png_bytes=chart_png_bytes,
                portfolio_chart_dict=portfolio_chart_dict,
                pdf_bytes=pdf_bytes,
            )

            logger.info(
                f"邮件任务完成 ({self.receiver_email}) "
                f"(来自Session: {len(alert_stocks)}个警报)"
            )
            return receipt

        except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
            logger.error("日报邮件准备失败: %s", type(e).__name__)
            return {"status": "failed", "reason": "email_preparation_failed"}

    def send_daily_report_from_session(self, session):
        """
        从Session读取数据并发送每日报告（新数据流）

        Args:
            session: SessionContext对象
        """
        session = report_view(session, self.config)
        curves_only = returns_only(self.config) or not technical_signals_enabled(
            self.config
        )
        try:
            # 从Session读取所有数据
            stock_data = session.get_all_dataframe()
            announcements = session.announcements

            # 构建邮件主题
            subject = f"股票日报 - {datetime.now().astimezone().strftime('%Y-%m-%d')}"

            # 获取投资组合策略结果（唯一收口）
            evaluation_reports = getattr(session, "evaluation_reports", None)

            # 无告警日报同样使用单张三市场组合总览图。
            portfolio_chart_dict = None
            if curves_only or evaluation_reports:
                try:
                    overview_png = (
                        generate_return_curve_png(
                            getattr(session, "strategy_return_curves", None)
                        )
                        if curves_only
                        else generate_portfolio_overview_chart(evaluation_reports)
                    )
                    portfolio_chart_dict = (
                        {"overview": overview_png} if overview_png else None
                    )
                except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
                    logger.error(f"投资组合图表生成失败: {e}")

            # 获取策略信号扫描结果
            signal_scan = getattr(session, "signal_scan", None)
            backtest = getattr(session, "backtest", None)

            # 生成日报 PDF 附件
            pdf_bytes = None
            try:
                pdf_bytes = self._generate_daily_pdf(
                    session,
                    [],
                    signal_scan,
                    backtest,
                    stock_data,
                )
            except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
                logger.warning("日报 PDF 生成失败: %s", e)

            # 构建邮件内容（使用空警报列表）
            body = self._build_email_body(
                [],
                stock_data,
                announcements,
                portfolio_chart_dict=portfolio_chart_dict,
                signal_scan=signal_scan,
                backtest=backtest,
                evaluation_reports=evaluation_reports,
                placements=getattr(session, "placements", None),
                dividend_events=getattr(session, "dividend_events", None),
                strategy_return_curves=getattr(session, "strategy_return_curves", None),
                report_as_of=getattr(session, "created_at", None),
            )

            # 发送邮件
            receipt = self._send_email(
                subject,
                body,
                portfolio_chart_dict=portfolio_chart_dict,
                pdf_bytes=pdf_bytes,
            )

            logger.info(f"每日报告邮件任务完成 ({self.receiver_email}) (来自Session)")
            return receipt

        except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
            logger.error("每日报告邮件准备失败: %s", type(e).__name__)
            return {"status": "failed", "reason": "email_preparation_failed"}

    @staticmethod
    def _daily_get(item, key, default=None):
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    @staticmethod
    def _daily_metric(value, unit="", precision=2, default="—") -> str:
        if value is None:
            return default
        try:
            if pd.isna(value):
                return default
        except (TypeError, ValueError):
            pass
        try:
            return f"{float(value):,.{precision}f}{unit}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _daily_row_map(stock_data) -> dict[str, object]:
        if stock_data is None or not hasattr(stock_data, "iterrows"):
            return {}
        rows = {}
        for _, row in stock_data.iterrows():
            code = row.get("stock_code", "")
            if code is not None:
                rows[str(code)] = row
        return rows

    @staticmethod
    def _daily_number(value):
        """Return a finite float for report rendering, otherwise ``None``."""
        try:
            if value is None or pd.isna(value):
                return None
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if np.isfinite(number) else None

    def _daily_alert_code_sets(self, alert_stocks, signal_scan=None):
        alert_codes = set()
        for alert in alert_stocks or []:
            code = str(_alert_value(alert, "stock_code", "") or "").strip()
            if code:
                alert_codes.add(code)
        signal_codes = set()
        for alert in (
            (getattr(signal_scan, "alerts", None) or []) if signal_scan else []
        ):
            code = str(_alert_value(alert, "stock_code", "") or "").strip()
            if code:
                signal_codes.add(code)
        return alert_codes, signal_codes

    def _daily_configured_symbols(self) -> list[tuple[str, str]]:
        """Return the deduplicated daily universe declared in configuration."""
        symbols = []
        seen = set()
        for configured in self.config.get("stocks", []) or []:
            if isinstance(configured, dict):
                raw_code = configured.get("stock_code", configured.get("code", ""))
                raw_name = configured.get("stock_name", configured.get("name", ""))
            else:
                raw_code = configured
                raw_name = ""
            code = str(raw_code or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            symbols.append((code, str(raw_name or "").strip()))
        return symbols

    def _daily_watchlist_rows(self, stock_data, alert_stocks, signal_scan=None):
        """Build one compact, market-aware row for every configured symbol."""
        source_rows = {}
        if stock_data is not None and hasattr(stock_data, "iterrows"):
            for _, source_row in stock_data.iterrows():
                code = str(source_row.get("stock_code", "") or "").strip()
                if code:
                    source_rows[code] = source_row
        configured_symbols = self._daily_configured_symbols()
        symbols = configured_symbols or [(code, "") for code in source_rows]
        if not symbols:
            return []
        fresh_entries = (
            {
                str(entry["code"]): entry
                for entry in build_brief_entries(stock_data, datetime.now().astimezone())
            }
            if stock_data is not None and hasattr(stock_data, "iterrows")
            else {}
        )
        alert_codes, signal_codes = self._daily_alert_code_sets(
            alert_stocks, signal_scan
        )
        group_order = {"a_share": 0, "hk": 1, "us": 2, "sg": 3}
        rows = []
        for code, configured_name in symbols:
            source_row = source_rows.get(code)
            entry = fresh_entries.get(code)
            raw_name = (
                source_row.get("stock_name", configured_name or code)
                if source_row is not None
                else configured_name or code
            )
            name = code if raw_name is None or pd.isna(raw_name) else str(raw_name)
            group = _detect_fine_group(code)
            has_signal = code in signal_codes
            has_alert = code in alert_codes
            priority = 0 if has_signal else 1 if has_alert else 2
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "group": group,
                    "entry": entry,
                    "pe_ratio": source_row.get("pe_ratio")
                    if source_row is not None
                    else None,
                    "pb_ratio": source_row.get("pb_ratio")
                    if source_row is not None
                    else None,
                    "roe": source_row.get("roe") if source_row is not None else None,
                    "priority": priority,
                    "sort_key": entry.get("sort_key", float("inf"))
                    if entry
                    else float("inf"),
                }
            )
        return sorted(
            rows,
            key=lambda item: (
                group_order.get(item["group"], 99),
                item["priority"],
                item["sort_key"],
                item["code"],
            ),
        )

    def _daily_scan_outcomes(self, signal_scan):
        outcomes = getattr(signal_scan, "group_outcomes", None)
        outcomes = outcomes if isinstance(outcomes, dict) else {}
        return {
            group: outcomes.get(group)
            or {"status": "not_run", "reason": "缺少扫描完成状态"}
            for group in ("a_share", "hk", "us")
        }

    def _daily_scan_status_text(self, outcome):
        labels = {
            "completed": "扫描完成",
            "completed_no_signal": "扫描完成，无触发信号",
            "blocked": "扫描受阻",
            "failed": "扫描失败",
            "not_run": "未执行扫描",
            "not_applicable": "无需扫描",
        }
        label = labels.get(outcome.get("status"), "扫描状态未知")
        reason = str(outcome.get("reason") or "")
        text = f"{label}：{reason}" if reason else label
        if "expected_codes" in outcome:
            expected = set(outcome["expected_codes"])
            scanned = set(outcome.get("scanned_codes", [])) & expected
            text += f"（覆盖{len(scanned)}/{len(expected)}只）"
        return text

    def _daily_scan_complete(self, signal_scan):
        return all(
            outcome.get("status")
            in {"completed", "completed_no_signal", "not_applicable"}
            for outcome in self._daily_scan_outcomes(signal_scan).values()
        )

    def _build_daily_summary_section(self, rows, alert_stocks, signal_scan=None):
        alert_codes, signal_codes = self._daily_alert_code_sets(
            alert_stocks, signal_scan
        )
        active_count = sum(1 for item in rows if item["entry"] is not None)
        cells = [
            ("行情就绪", f"{active_count}/{len(rows)}"),
            ("告警标的", len(alert_codes)),
            ("触发条件", len(alert_stocks or [])),
            (
                "策略信号",
                len(signal_codes)
                if self._daily_scan_complete(signal_scan)
                else f"{len(signal_codes)}（扫描未完成）",
            ),
        ]
        if returns_only(self.config) or not technical_signals_enabled(self.config):
            cells = [
                ("行情就绪", f"{active_count}/{len(rows)}"),
                ("监控标的", len(rows)),
                ("策略展示", "Ke收益曲线"),
                ("技术信号", "已停用"),
            ]
        html = [
            ('<section class="daily-section summary-section">'
            '<div class="section-heading">今日概览</div>'
            '<table role="presentation" class="summary-grid"><tr>')
        ]
        for index, (label, value) in enumerate(cells):
            if index and index % 2 == 0:
                html.append("</tr><tr>")
            html.append(
                '<td class="summary-cell"><div class="summary-label">'
                f'{_html_escape(label)}</div><div class="summary-value">'
                f"{_html_escape(value)}</div></td>"
            )
        html.append("</tr></table></section>")
        return "".join(html)

    def _build_daily_watchlist_section(self, watchlist_rows) -> str:
        from .report_policy import DISPLAY_MARKETS

        labels = DISPLAY_MARKETS
        grouped = {group: [] for group in labels}
        for row in watchlist_rows:
            grouped.setdefault(row["group"], []).append(row)

        sections = [
            (
                '<section class="daily-section watchlist-section">'
                '<div class="section-heading">完整行情矩阵'
                f'<span class="section-count">{len(watchlist_rows)}只</span></div>'
            )
        ]
        for group in labels:
            rows = grouped.get(group, [])
            if not rows:
                continue
            sections.append(
                f'<div class="group-heading">{_html_escape(labels[group])} · {len(rows)}只</div>'
                '<table role="presentation" class="watchlist-table">'
                "<thead><tr><th>标的</th><th>今开</th><th>昨收</th><th>现价</th>"
                '<th>PE</th><th class="mobile-hide">PB</th><th>隐含 Ke</th>'
                '<th class="mobile-hide">ROE%</th></tr></thead><tbody>'
            )
            for index, row in enumerate(rows):
                entry = row["entry"]
                if entry is None:
                    open_text = prev_close_text = close_text = pe_text = pb_text = (
                        implied_ke_text
                    ) = "—"
                    row_class = " watch-row-stale"
                    status = '<span class="status-badge status-stale">未就绪</span>'
                else:
                    open_text = self._daily_metric(entry.get("open"), "", 2)
                    prev_close_text = self._daily_metric(entry.get("prev_close"), "", 2)
                    close_text = self._daily_metric(entry.get("close"), "", 2)
                    pe_text = self._daily_metric(row.get("pe_ratio"), "", 1)
                    pb_text = self._daily_metric(row.get("pb_ratio"), "", 2)
                    implied_ke_text = self._daily_metric(
                        implied_ke(row.get("pe_ratio")), "%", 1
                    )
                    # Signals and price alerts stay prioritised in the action
                    # list and sort order, but do not repeat as badges beside
                    # every name in the full market matrix.
                    row_class = ""
                    status = ""
                direction, change = price_movement(
                    entry.get("close") if entry else None,
                    entry.get("prev_close") if entry else None,
                )
                color = PRICE_COLORS[direction]
                background = "#f5f8fc" if index % 2 else "#ffffff"
                sections.append(
                    f'<tr class="{row_class.strip()}" style="background:{background}"><td class="watch-instrument">'
                    f"<strong>{_html_escape(row['code'])}</strong><span>"
                    f"{_html_escape(row['name'])}</span>{status}</td>"
                    f"<td>{_html_escape(open_text)}</td>"
                    f"<td>{_html_escape(prev_close_text)}</td>"
                    f'<td class="current-quote" style="color:{color}"><strong style="color:{color}">{_html_escape(close_text)}</strong>'
                    f'<span class="price-change" style="color:{color}">{_html_escape(change)}</span></td>'
                    f"<td>{_html_escape(pe_text)}</td>"
                    f'<td class="mobile-hide">{_html_escape(pb_text)}</td>'
                    f"<td>{_html_escape(implied_ke_text)}</td>"
                    f'<td class="mobile-hide">{_html_escape(format_roe(row))}</td></tr>'
                )
            sections.append("</tbody></table>")
        sections.append(
            '<div class="muted-note">价格对应各市场最近行情交易日。昨收优先使用行情源前收盘价，'
            "缺失时取同口径前一交易日收盘。隐含 Ke = TTM 每股收益 ÷ 收盘价 + 2% "
            "（即 1 ÷ PE + 2%；PE≤0 或缺失时不显示）。ETF 的 PE/PB 按跟踪指数或"
            "公开成分股权重聚合；黄金、REIT 等非企业资产不展示企业 PE/PB。"
            + _html_escape(ROE_ESTIMATE_NOTE)
            + "</div>"
        )
        sections.append("</section>")
        return "".join(sections)

    def _daily_holdout_payload(self, report):
        """Return only a complete, numerically trustworthy 22/16/2/4 holdout."""
        selection = self._daily_get(report, "selection_diagnostics", {}) or {}
        if not isinstance(selection, dict):
            return None
        summary = selection.get("holdout_summary", {}) or {}
        if not isinstance(summary, dict):
            return None
        summary_keys = (
            "return_pct",
            "excess_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
        )
        if any(self._daily_number(summary.get(key)) is None for key in summary_keys):
            return None
        windows = [
            item
            for item in selection.get("windows", []) or []
            if isinstance(item, dict) and item.get("role") == "holdout"
        ]
        if len(windows) != 4:
            return None
        try:
            windows = sorted(windows, key=lambda item: int(item["role_index"]))
        except (KeyError, TypeError, ValueError):
            return None
        if [int(item["role_index"]) for item in windows] != [1, 2, 3, 4]:
            return None
        window_keys = ("return", "excess_return", "max_drawdown", "sharpe_ratio")
        if any(
            self._daily_number(window.get(key)) is None
            for window in windows
            for key in window_keys
        ):
            return None
        return {"summary": summary, "windows": windows}

    def _build_daily_signal_section(self, signal_scan, stock_data) -> str:
        if returns_only(self.config) or not technical_signals_enabled(self.config):
            return ""
        """Show scan details even when historical evaluation reports are absent."""
        parts = [
            ('<section class="daily-section signal-section">'
            '<div class="section-heading">今日策略信号')
        ]
        if signal_scan is None:
            parts.append(
                '</div><div class="muted-note">策略扫描结果不可用；'
                "不能据此判断为无信号。</div></section>"
            )
            return "".join(parts)

        alerts = getattr(signal_scan, "alerts", None) or []
        codes = {str(_alert_value(alert, "stock_code", "?")) for alert in alerts}
        parts.append(
            f'<span class="section-count">{len(alerts)}条 / {len(codes)}只</span></div>'
        )
        for group, outcome in self._daily_scan_outcomes(signal_scan).items():
            label = {"a_share": "A股", "hk": "港股", "us": "美股"}[group]
            parts.append(
                '<div class="muted-note">'
                f"{label}：{_html_escape(self._daily_scan_status_text(outcome))}</div>"
            )
        if not self._daily_scan_complete(signal_scan):
            parts.append(
                '<div class="muted-note">策略扫描未全部完成；'
                "已列出的信号来自实际扫描子集，不能据此判断全部市场无信号。</div>"
            )
        if not alerts:
            if not self._daily_scan_complete(signal_scan):
                return "".join(parts) + "</section>"
            no_signal_text = (
                "本次无待扫描标的。"
                if all(
                    outcome.get("status") == "not_applicable"
                    for outcome in self._daily_scan_outcomes(signal_scan).values()
                )
                else "本次扫描无触发信号。"
            )
            parts.append(f'<div class="muted-note">{no_signal_text}</div></section>')
            return "".join(parts)

        rows = self._daily_row_map(stock_data)
        map_a = _build_signal_label_map("a_share")
        map_hk = _build_signal_label_map("hk") or _build_signal_label_map("non_a_share")
        map_us = _build_signal_label_map("us") or _build_signal_label_map("non_a_share")
        for alert in alerts:
            code = str(_alert_value(alert, "stock_code", "?"))
            name = _alert_value(
                alert, "stock_name", rows.get(code, {}).get("stock_name", "")
            )
            raw_label = _alert_value(
                alert, "rule_label", _alert_value(alert, "label", "规则未提供")
            )
            rule = _readable_signal(code, raw_label, map_a, map_hk, map_us)
            side = str(_alert_value(alert, "side", "")).lower()
            if not side:
                kind = str(_alert_value(alert, "type", "")).lower()
                side = {"strategy_buy": "buy", "strategy_sell": "sell"}.get(kind, "")
            direction = {"buy": "买入", "sell": "卖出"}.get(side, side or "方向未提供")
            current_value = self._daily_get(alert, "current_value")
            detail = _alert_value(alert, "detail", _alert_value(alert, "reason", ""))
            signal_date = _alert_value(alert, "signal_date", "")
            parts.append(
                '<div class="signal-row"><div class="signal-title"><strong>'
                f"{_html_escape(code)} · {_html_escape(name, '')}</strong>"
                f" <span>{_html_escape(direction)}</span></div>"
                f"<div>规则：{_html_escape(rule)} · "
                f"当前值：{_html_escape(current_value)}</div>"
            )
            if detail:
                parts.append(f"<div>触发依据：{_html_escape(detail)}</div>")
            if signal_date:
                parts.append(f"<div>信号日期：{_html_escape(signal_date)}</div>")
            parts.append("</div>")
        parts.append("</section>")
        return "".join(parts)

    def _build_daily_strategy_section(
        self,
        evaluation_reports,
        signal_scan=None,
        backtest=None,
        alert_codes=None,
        signal_codes=None,
    ) -> str:
        """Render one compact decision card per independent market."""
        if returns_only(self.config) or not technical_signals_enabled(self.config):
            return ""
        reports = evaluation_reports if isinstance(evaluation_reports, dict) else {}
        alert_codes = set(alert_codes or ())
        signal_codes = set(signal_codes or ())
        labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
        alert_counts = {group: 0 for group in labels}
        signal_counts = {group: 0 for group in labels}
        for code in alert_codes:
            alert_counts[_detect_fine_group(code)] = (
                alert_counts.get(_detect_fine_group(code), 0) + 1
            )
        for code in signal_codes:
            signal_counts[_detect_fine_group(code)] = (
                signal_counts.get(_detect_fine_group(code), 0) + 1
            )

        parts = [
            ('<section class="daily-section decision-section">'
            '<div class="section-heading">三市场决策板</div>')
        ]
        for group, label in labels.items():
            report = reports.get(group)
            outcome = self._daily_scan_outcomes(signal_scan)[group]
            action_text = (
                f"{self._daily_scan_status_text(outcome)} · "
                f"已发现策略信号 {signal_counts[group]} · 价格预警 {alert_counts[group]}"
            )
            if report is None:
                parts.append(
                    '<div class="market-card market-card-inactive"><div class="market-title">'
                    f"<strong>{_html_escape(label)}</strong><span>{_html_escape(action_text)}</span>"
                    '</div><div class="muted-note">该市场历史评估报告不可用；扫描状态独立列示。</div></div>'
                )
                continue

            total_return = self._daily_get(report, "total_return")
            return_number = self._daily_number(total_return)
            return_color = (
                "metric-positive"
                if return_number is not None and return_number >= 0
                else "metric-negative"
            )
            primary = str(self._daily_get(report, "primary_benchmark", "") or "")
            benchmark_returns = self._daily_get(report, "benchmark_returns", {}) or {}
            benchmark_return = (
                benchmark_returns.get(primary)
                if isinstance(benchmark_returns, dict) and primary
                else None
            )
            benchmark_line = (
                f"主基准 ★{primary} {_html_escape(self._daily_metric(benchmark_return, '%', 1))}"
                if primary
                else "主基准未就绪"
            )
            parts.append(
                '<div class="market-card"><div class="market-title"><strong>'
                f"{_html_escape(label)} · {_html_escape(self._daily_get(report, 'strategy_label', '策略评估'))}"
                f"</strong><span>{_html_escape(action_text)}</span></div>"
                f'<div class="market-benchmark">{benchmark_line}</div>'
                '<table role="presentation" class="metric-grid"><tr>'
                '<td class="metric-cell"><div class="metric-label">评估期收益</div>'
                f'<div class="metric-value {return_color}">{_html_escape(self._daily_metric(total_return, "%", 1))}</div></td>'
                '<td class="metric-cell"><div class="metric-label">对主基准超额</div>'
                f'<div class="metric-value">{_html_escape(self._daily_metric(self._daily_get(report, "excess_return"), "%", 1))}</div></td>'
                '</tr><tr><td class="metric-cell"><div class="metric-label">最大回撤</div>'
                f'<div class="metric-value">{_html_escape(self._daily_metric(self._daily_get(report, "max_drawdown"), "%", 1))}</div></td>'
                '<td class="metric-cell"><div class="metric-label">Sharpe / 交易</div>'
                f'<div class="metric-value">{_html_escape(self._daily_metric(self._daily_get(report, "sharpe_ratio"), "", 2))} / '
                f"{_html_escape(self._daily_get(report, 'trade_count', '—'))}</div></td></tr></table>"
            )
            holdout = self._daily_holdout_payload(report)
            if holdout is None:
                parts.append(
                    '<div class="holdout-note">84个月 Holdout：报告尚未形成完整 22/16/2/4 产物，'
                    '请查看完整报告。</div>'
                )
            else:
                summary = holdout["summary"]
                parts.append(
                    '<div class="holdout-title">84个月 Holdout · 窗口等权，回撤取最差</div>'
                    '<div class="holdout-summary">'
                    f'收益 {_html_escape(self._daily_metric(summary["return_pct"], "%", 1))} · '
                    f'超额 {_html_escape(self._daily_metric(summary["excess_return_pct"], "%", 1))} · '
                    f'回撤 {_html_escape(self._daily_metric(summary["max_drawdown_pct"], "%", 1))} · '
                    f'Sharpe {_html_escape(self._daily_metric(summary["sharpe_ratio"], "", 2))}'
                    '</div><div class="holdout-chip-row">'
                )
                for window in holdout["windows"]:
                    parts.append(
                        '<span class="holdout-chip"><strong>'
                        f'H{int(window["role_index"])} </strong>'
                        f'R {_html_escape(self._daily_metric(window["return"], "%", 1))} · '
                        f'E {_html_escape(self._daily_metric(window["excess_return"], "%", 1))} · '
                        f'DD {_html_escape(self._daily_metric(window["max_drawdown"], "%", 1))} · '
                        f'S {_html_escape(self._daily_metric(window["sharpe_ratio"], "", 2))}'
                        '</span>'
                    )
                parts.append("</div>")
            parts.append("</div>")
        parts.append("</section>")
        return "".join(parts)

    def _build_daily_events_section(
        self,
        announcements,
        placements,
        stock_data,
        dividend_events=None,
        *,
        report_as_of=None,
    ) -> str:
        """Render the shared category/market/instrument feed, preserving all source links."""
        events = build_daily_event_rows(
            announcements,
            placements,
            stock_data,
            dividend_events,
            as_of=report_as_of,
            config=self.config,
        )
        if not events:
            return ""
        count = sum(len(item.get("members") or [item]) for item in events)
        months = (
            (self.config.get("report") or {})
            .get("events", {})
            .get("announcement_lookback_months", 2)
        )
        lines = [
            (
                '<section class="daily-section events-section">'
                '<div class="section-heading">事件速览'
                f'<span class="section-count">{count}份事项</span></div>'
            )
        ]
        category = market = None
        for group in group_daily_event_rows(events):
            if group["category"] != category:
                category, market = group["category"], None
                lines.append(
                    f'<div class="event-category">{_html_escape(category)}</div>'
                )
            if group["market"] != market:
                market = group["market"]
                lines.append(f'<div class="event-market">{_html_escape(market)}</div>')
            lines.append(
                f'<div class="event-instrument">{_html_escape(group["code"])} '
                f"{_html_escape(group['name'])}</div>"
            )
            for item in group["rows"]:
                members = item.get("members") or [item]
                if len(members) > 1:
                    links = []
                    for member in members:
                        date = _html_escape(member["date"] or "日期待确认")
                        links.append(
                            f'<a href="{_safe_html_url(member["url"])}" title="{_html_escape(member["title"])}">{date}</a>'
                            if member["url"]
                            else date
                        )
                    lines.append(
                        f'<div class="event-row event-routine"><strong>{_html_escape(item["kind"])} · {len(members)}份</strong>'
                        f'<div class="event-dates">{" / ".join(links)}</div></div>'
                    )
                    continue
                title = _html_escape(item["title"])
                if item["url"]:
                    title = f'<a href="{_safe_html_url(item["url"])}">{title}</a>'
                lines.append(
                    f'<div class="event-row"><span class="event-date">{_html_escape(item["date"] or "日期待确认")}</span>{title}</div>'
                )
        lines.append(
            f'<div class="muted-note">公告展示近{months}个自然月已采集内容；日期待确认项单独标记。待派息分红、未解禁事项继续保留。例行披露合并展示，每个日期均可查看原文。</div>'
            "</section>"
        )
        return "".join(lines)

    @staticmethod
    def _daily_chart_section(chart_png_bytes):
        if not chart_png_bytes:
            return ""
        return (
            '<section class="daily-section"><div class="section-heading">关键价格图表</div>'
            '<div class="chart-card"><img src="cid:chart001" alt="关键价格与锚点图表" style="display:block;width:100%;max-width:100%;height:auto;"></div></section>'
        )

    def _daily_portfolio_chart_section(
        self, portfolio_chart_dict, strategy_return_curves=None
    ):
        if returns_only(self.config) or not technical_signals_enabled(self.config):
            source = (
                "cid:chart002" if (portfolio_chart_dict or {}).get("overview") else None
            )
            return (
                '<section class="daily-section portfolio-overview-section">'
                '<div class="section-heading">Ke策略收益曲线</div><div class="chart-card">'
                + return_curves_html(strategy_return_curves, source, compact=True)
                + "</div></section>"
            )
        if not isinstance(portfolio_chart_dict, dict) or not portfolio_chart_dict.get(
            "overview"
        ):
            return ""
        return (
            '<section class="daily-section portfolio-overview-section">'
            '<div class="section-heading">组合走势总览</div><div class="chart-card">'
            '<img src="cid:chart002" alt="A股、港股、美股归一化组合净值总览" '
            'style="display:block;width:100%;max-width:100%;height:auto;"></div></section>'
        )

    def _build_email_body(
        self,
        alert_stocks,
        stock_data,
        announcements=None,
        chart_png_bytes=None,
        portfolio_chart_dict=None,
        signal_scan=None,
        backtest=None,
        evaluation_reports=None,
        placements=None,
        dividend_events=None,
        strategy_return_curves=None,
        report_as_of=None,
    ):
        template_dir = Path(__file__).parent.parent / "templates"
        template = (template_dir / "daily_email_mobile.html").read_text(
            encoding="utf-8"
        )
        server_info = self._get_server_info()
        deployment_status = (
            '<div class="deployment-strip"><strong>版本 '
            f"{_html_escape(server_info.get('release_label', '未登记发布'))}</strong><br>"
            "<strong>部署状态</strong> · 节点 "
            f"{_html_escape(server_info.get('hostname'))} · IP "
            f"{_html_escape(server_info.get('ip_address'))}"
            f"<span> · {_html_escape(server_info.get('system'))} "
            f"{_html_escape(server_info.get('machine'))} "
            f"{_html_escape(server_info.get('kernel_version'))}</span></div>"
        )
        reports = evaluation_reports or {}
        watchlist_rows = self._daily_watchlist_rows(
            stock_data, alert_stocks, signal_scan
        )
        report_time = pd.Timestamp(report_as_of or datetime.now().astimezone())
        report_time = (
            report_time.tz_localize("Asia/Shanghai")
            if report_time.tzinfo is None
            else report_time.tz_convert("Asia/Shanghai")
        )
        alert_codes, signal_codes = self._daily_alert_code_sets(
            alert_stocks, signal_scan
        )
        return template.format(
            current_time=report_time.strftime("%Y-%m-%d %H:%M:%S"),
            deployment_status=deployment_status,
            summary_section=self._build_daily_summary_section(
                watchlist_rows, alert_stocks, signal_scan
            ),
            signal_section=self._build_daily_signal_section(signal_scan, stock_data),
            decision_section=self._build_daily_strategy_section(
                reports,
                signal_scan,
                backtest,
                alert_codes=alert_codes,
                signal_codes=signal_codes,
            ),
            watchlist_section=self._build_daily_watchlist_section(watchlist_rows),
            chart_section=self._daily_chart_section(chart_png_bytes),
            portfolio_chart_section=self._daily_portfolio_chart_section(
                portfolio_chart_dict, strategy_return_curves
            ),
            events_section=self._build_daily_events_section(
                announcements, placements, stock_data, dividend_events, report_as_of=report_as_of
            ),
        )

    def _get_server_info(self):
        """
        获取服务器信息（IP地址和内核版本）

        Returns:
            dict: 包含服务器信息的字典
        """
        try:
            from ..core.build_info import build_label, get_build_info
        except ImportError:
            from core.build_info import build_label, get_build_info

        build = get_build_info()
        release = {"release_label": build_label(build), "build_info": build}
        try:
            # 获取主机名和IP地址
            hostname = socket.gethostname()
            ip_list = []

            # 方法1: 通过socket.gethostbyname_ex获取所有IP
            try:
                _, _, ip_addresses = socket.gethostbyname_ex(hostname)
                ip_list.extend(ip_addresses)
            except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
                logger.debug(f"gethostbyname_ex 失败: {e}")

            # 方法2: 通过hostname -I命令获取所有IP（Linux）
            try:
                result = subprocess.run(
                    ["hostname", "-I"], capture_output=True, text=True, timeout=5, check=False
                )
                if result.returncode == 0:
                    ips = result.stdout.strip().split()
                    ip_list.extend(ips)
            except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
                logger.debug(f"hostname -I 失败: {e}")

            # 去重并过滤回环地址
            ip_list = list(set(ip_list))
            ip_list = [ip for ip in ip_list if not ip.startswith("127.")]

            if ip_list:
                ip_address = ", ".join(ip_list)
            else:
                ip_address = "无法获取"

            # 获取内核版本（Linux系统）
            kernel_version = "未知"
            try:
                # 尝试通过platform模块获取
                kernel_version = platform.release()
                if not kernel_version or kernel_version == "":
                    # 尝试通过uname命令获取
                    result = subprocess.run(
                        ["uname", "-r"], capture_output=True, text=True, timeout=5, check=False
                    )
                    if result.returncode == 0:
                        kernel_version = result.stdout.strip()
            except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
                logger.debug(f"内核版本获取失败: {e}")
                kernel_version = platform.uname().release

            return {
                **release,
                "hostname": hostname,
                "ip_address": ip_address,
                "kernel_version": kernel_version,
                "system": platform.system(),
                "machine": platform.machine(),
            }
        except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
            logger.warning(f"获取服务器信息失败: {e}")
            return {
                **release,
                "hostname": "未知",
                "ip_address": "无法获取",
                "kernel_version": "未知",
                "system": "未知",
                "machine": "未知",
            }

    # ────────────── 简报方法 ──────────────

    # Thin alias: the implementation moved to report_builders.pick_best_anchor
    # but EmailNotifier._pick_best_anchor is part of the de-facto API
    # (feishu_notifier, telegram_notifier, 11 test call sites).
    _pick_best_anchor = staticmethod(pick_best_anchor)

    # ── 参考持仓 HTML 构建 ────────────────────────────────────

    @staticmethod
    def _build_ref_portfolio_status_note(status) -> str:
        """Explain blocked trading and incomplete valuation without mutating state."""
        notes = []
        reason = str(status.get("_trading_blocked_reason") or "")
        manual_reset = bool(status.get("requires_manual_reset"))
        if manual_reset and not reason:
            reason = "未绑定运行，需手动重置"
        if reason:
            notes.append(f'<p class="muted-note">停单原因：{_html_escape(reason)}</p>')
            if "未绑定运行" in reason:
                recovery = (
                    "旧账户未绑定策略运行。若选择重建，先确认有效策略已激活，"
                    "再通过机器人 /ref_date YYYY-MM-DD 重置；"
                    "这会清空三市场持仓并重设基期。"
                )
            elif "固定运行或执行合同不可恢复" in reason:
                recovery = "请恢复该账户绑定的策略产物及执行合同后重新检查。"
            elif "公司行为数据不就绪" in reason:
                recovery = "请补齐分红、除权等公司行为数据后重新检查。"
            elif "没有完整历史数据" in reason:
                recovery = "请补齐监控标的及持仓的历史行情后重新检查。"
            else:
                recovery = "请核对对应运行日志，修复上述停单原因后重新检查。"
            notes.append(
                f'<p class="muted-note">恢复提示：{_html_escape(recovery)}</p>'
            )

        if status.get("valuation_complete") is False:
            valuation_reason = str(status.get("valuation_reason") or "")
            if not valuation_reason:
                missing = ", ".join(
                    str(code) for code in status.get("missing_price_codes", [])
                )
                valuation_reason = (
                    f"缺少 {missing} 行情" if missing else "持仓行情不完整"
                )
            notes.append(
                '<p class="muted-note">估值不完整：'
                f"{_html_escape(valuation_reason)}</p>"
            )
        ledger = render_in_kind_ledger(
            status.get("in_kind_positions", []) or [],
            status.get("in_kind_cashflows", []) or [],
        )
        if ledger:
            notes.append(ledger)
        return "".join(notes)

    @staticmethod
    def _build_ref_portfolio_html(session, today_date) -> str:
        """构建参考持仓 HTML 片段。三组（A股/港股/美股）各自展示。"""
        all_statuses = getattr(session, "ref_portfolio_status", None)
        if not isinstance(all_statuses, dict) or not all_statuses:
            return ""

        lines = ["<h3>📊 参考持仓</h3>"]
        group_order = ["a_share", "hk", "us"]

        for gk in group_order:
            status = all_statuses.get(gk)
            if not status:
                continue
            label = status.get("_label", gk)
            nav = _fmt(status.get("nav"), fmt_spec=",.0f")
            nav_return = _fmt(status.get("nav_return_pct"), "%", "+.2f")
            lines.append(
                f"<h4>{_html_escape(label)}</h4>"
                f"<p>📅 期初: {_html_escape(status.get('inception_date'))} | "
                f"💰 净值: {nav} | 📈 回报: {nav_return} | "
                f"📆 交易日: {_html_escape(status.get('trading_days'))}</p>"
            )
            lines.append(EmailNotifier._build_ref_portfolio_status_note(status))

            if status["holdings"]:
                lines.append(
                    "<table><tr><th>代码</th><th>持仓</th><th>现价</th>"
                    "<th>市值</th><th>成本</th></tr>"
                )
                for h in status["holdings"]:
                    lines.append(
                        f"<tr>"
                        f"<td>{_html_escape(h['code'])}</td>"
                        f"<td>{_html_escape(h['shares'])}</td>"
                        f"<td>{_fmt(h.get('price'))}</td>"
                        f"<td>{_fmt(h.get('market_value'), fmt_spec=',.0f')}</td>"
                        f"<td>{_fmt(h.get('avg_cost'))}</td>"
                        f"</tr>"
                    )
                lines.append("</table>")
            else:
                lines.append("<p>📭 空仓</p>")

            cash = _fmt(status.get("cash"), fmt_spec=",.2f")
            lines.append(f"<p>💵 现金: {cash}</p>")

        return "\n".join(lines)

    def send_brief_report(self, session, report_config: dict):
        try:
            subject, body = self._prepare_brief_report(session, report_config)
        except Exception as exc:  # noqa: BLE001 -- optional placement metadata  # noqa: BLE001 - classify preparation before SMTP
            logger.error("简报邮件准备失败: %s", type(exc).__name__)
            return {"status": "failed", "reason": "email_preparation_failed"}
        return self._send_email(subject, body)

    def _prepare_brief_report(self, session, report_config: dict):
        """
        发送简报邮件（仅价格+锚点偏离率，无图表/基本面/公告）。

        Args:
            session: SessionContext
            report_config: 简报配置 {"id": "morning_snapshot", "label": "早盘简报", ...}
        """
        from datetime import datetime
        from pathlib import Path

        session = report_view(session, self.config)
        curves_only = returns_only(self.config) or not technical_signals_enabled(
            self.config
        )
        label = report_config.get("label", "简报")
        from .report_builders import get_brief_dataframe

        stock_data = get_brief_dataframe(session)
        today = datetime.now().astimezone()
        today_date = today.date()

        # ── 构建每只股票的行 ──
        rows_data = [] if curves_only else build_brief_entries(stock_data, today)

        # ── 策略信号（直接用 SignalScanner 结果，和日报一致）──
        signal_scan = getattr(session, "signal_scan", None)
        strat_html = ""
        if signal_scan and signal_scan.alerts:
            alerts = signal_scan.alerts
            map_a = _build_signal_label_map("a_share")
            map_hk = _build_signal_label_map("hk") or _build_signal_label_map(
                "non_a_share"
            )
            map_us = _build_signal_label_map("us") or _build_signal_label_map(
                "non_a_share"
            )
            strat_html = (
                "<h3>策略信号</h3>"
                f"<p>共 {len(alerts)} 条策略告警</p>"
                "<table><tr><th>代码</th><th>信号</th><th>当前值</th></tr>"
            )
            for a in alerts[:12]:
                code = _alert_value(a, "stock_code", "?")
                raw = _alert_value(a, "rule_label", "?")
                readable = _readable_signal(code, raw, map_a, map_hk, map_us)
                cv = _alert_value(a, "current_value", "-")
                strat_html += (
                    f"<tr><td>{code}</td><td>{readable}</td><td>{cv}</td></tr>"
                )
            strat_html += "</table><br>"
        elif signal_scan:
            strat_html = "<h3>策略信号</h3><p>当前无活跃信号</p><br>"

        # ── 参考持仓状态 ──
        ref_html = (
            "" if curves_only else self._build_ref_portfolio_html(session, today_date)
        )

        # ── 渲染 HTML ──
        html_rows = []
        for entry in rows_data:
            open_str = (
                f"{entry['open']:.2f}"
                if entry["open"] is not None and not pd.isna(entry["open"])
                else "—"
            )
            close_str = (
                f"{entry['close']:.2f}"
                if entry["close"] is not None and not pd.isna(entry["close"])
                else "—"
            )
            anchor_str = (
                f"{entry['anchor_val']:.2f}" if entry["anchor_val"] is not None else "-"
            )
            dev_color = "#27ae60" if (entry["dev_pct"] or 0) >= 0 else "#c0392b"
            html_row = (
                f"<tr>"
                f"<td>{entry['code']}</td>"
                f"<td>{entry['name']}</td>"
                f"<td>{_fmt(entry['prev_close'])}</td>"
                f"<td>{open_str}</td>"
                f"<td>{close_str}</td>"
                f"<td>{entry['anchor_name']}</td>"
                f"<td>{anchor_str}</td>"
                f'<td style="color:{dev_color}">{entry["dev_str"]}</td>'
                f"</tr>\n"
            )
            html_rows.append(html_row)

        rows = "".join(html_rows)
        active_count = len(rows_data)
        columns = ["代码", "名称", "昨收", "开盘", "现价", "锚点", "锚值", "偏离率"]
        note = (
            "偏离率 = (现价 - 锚点值) / 锚点值 × 100% | "
            "每只标的标注实际回溯交易日最短且落入警报阈值区间的锚点 "
            "(ma60=60天 < wma20≈100天 < wma30≈150天 < wma50≈250天)"
        )
        if curves_only:
            from .report_policy import FUNDAMENTAL_COLUMNS

            columns = [label for _, label in FUNDAMENTAL_COLUMNS]
            entries = fundamental_rows(stock_data)
            rows = "".join(
                "<tr>" + "".join(f"<td>{_html_escape(v)}</td>" for v in row) + "</tr>"
                for row in entries
            )
            active_count = len(entries)
            note = (
                "技术信号已停用；缺失指标显示为 —，行情日期按各市场实际交易日展示。"
                + ROE_ESTIMATE_NOTE
            )
            strat_html = return_curves_html(
                getattr(session, "strategy_return_curves", None)
            )
        template_dir = Path(__file__).parent.parent / "templates"
        template = (template_dir / "brief_email.html").read_text(encoding="utf-8")

        from ..core.build_info import build_label

        body = template.format(
            release_label=_html_escape(build_label()),
            label=label,
            report_date=today.strftime("%Y-%m-%d"),
            current_time=today.strftime("%H:%M"),
            active_count=active_count,
            total_count=active_count,
            brief_rows=rows,
            brief_columns="".join(f"<th>{label}</th>" for label in columns),
            brief_note=note,
            strategy_suggestions=strat_html,
            ref_portfolio=ref_html,
        )

        subject = f"{label} - {today.strftime('%Y-%m-%d')}"
        return subject, body

    # ────────────────────────────────────

    def _delivery_guard(self):
        """Optional authenticated-route guard; ordinary mail needs no binding."""

    def _send_email(
        self,
        subject,
        body,
        chart_png_bytes=None,
        portfolio_chart_dict=None,
        pdf_bytes=None,
        candlestick_png=None,
        attachments=None,
    ):
        """
        发送邮件

        Args:
            subject: 邮件主题
            body: 邮件正文（HTML格式）
            chart_png_bytes: 告警走势图 PNG 字节（可选），CID=chart001
            portfolio_chart_dict: 投资组合走势图 {"a_share": bytes, "non_a_share": bytes}
            pdf_bytes: 日报 PDF 附件 bytes（可选）
            candlestick_png: 周K蜡烛图 bytes（可选），CID=candlestick
        """

        guard = self._delivery_guard()
        if guard:
            return guard

        # 保存邮件副本（无论是否跳过发送）
        copy_path = self._save_email_copy(subject, body)
        if copy_path:
            logger.info("邮件副本保存成功，路径: %s", copy_path)
        else:
            logger.error("邮件副本未保存，目标目录: %s", self.email_archive_dir)

        if not channel_enabled(self.config, "email"):
            logger.info(f"跳过邮件发送（测试模式）: 主题={subject}")
            return {"status": "skipped", "error": "email_channel_disabled"}

        submitted = False
        receipt = None
        recipients = []
        try:
            # 创建 HTML 部分
            # MIMEText encodes the payload and sets its matching transfer header.
            html_part = MIMEText(body, "html", "utf-8")

            has_any_chart = chart_png_bytes or portfolio_chart_dict or candlestick_png

            if has_any_chart:
                # 有任意图表：MIMEMultipart("related") 容器，HTML + 内嵌图片
                inner = MIMEMultipart("related")
                inner.policy = policy.default

                # 内嵌 alternative（HTML）
                alt = MIMEMultipart("alternative")
                alt.attach(html_part)
                inner.attach(alt)

                # 添加告警走势图（CID: chart001）
                if chart_png_bytes:
                    image = MIMEImage(chart_png_bytes, "png")
                    image.add_header("Content-ID", "<chart001>")
                    image.add_header(
                        "Content-Disposition", "inline", filename="chart.png"
                    )
                    inner.attach(image)
                    logger.info("告警走势图以 CID chart001 嵌入邮件")

                # 添加周K蜡烛图（CID: candlestick）
                if candlestick_png:
                    cs_img = MIMEImage(candlestick_png, "png")
                    cs_img.add_header("Content-ID", "<candlestick>")
                    cs_img.add_header(
                        "Content-Disposition", "inline", filename="candlestick.png"
                    )
                    inner.attach(cs_img)
                    logger.info("周K蜡烛图以 CID candlestick 嵌入邮件")

                # 日报优先使用单张归一化三市场总览；兼容旧调用方的分市场图。
                if portfolio_chart_dict:
                    cid_map = {
                        "overview": "chart002",
                        "a_share": "chart002",
                        "hk": "chart003",
                        "us": "chart004",
                        "non_a_share": "chart005",
                    }
                    for group_key, png_bytes in portfolio_chart_dict.items():
                        if png_bytes and group_key in cid_map:
                            cid = cid_map[group_key]
                            img = MIMEImage(png_bytes, "png")
                            img.add_header("Content-ID", f"<{cid}>")
                            img.add_header(
                                "Content-Disposition",
                                "inline",
                                filename=f"portfolio_{group_key}.png",
                            )
                            inner.attach(img)
                            logger.info(f"投资组合走势图以 CID {cid} 嵌入邮件")
            else:
                # 无图表：保持原逻辑
                inner = MIMEMultipart("alternative")
                inner.policy = policy.default
                inner.attach(html_part)

            # Reports can carry a compact portable HTML reader as an attachment.
            if pdf_bytes or attachments:
                from email.mime.application import MIMEApplication

                msg = MIMEMultipart("mixed")
                msg.policy = policy.default
                msg.attach(inner)
                if pdf_bytes:
                    pdf_part = MIMEApplication(pdf_bytes, "pdf")
                    pdf_part.add_header(
                        "Content-Disposition", "attachment", filename="日报.pdf"
                    )
                    msg.attach(pdf_part)
                for attachment in attachments or []:
                    filename = attachment["filename"]
                    content = attachment["content"]
                    if (
                        not isinstance(filename, str)
                        or not filename
                        or any(c in filename for c in ("/", "\\", "\r", "\n"))
                        or not isinstance(content, bytes)
                        or len(content) > 8_000_000
                    ):
                        raise ValueError("invalid_email_attachment")
                    major, minor = attachment.get(
                        "content_type", "application/octet-stream"
                    ).split("/", 1)
                    if (
                        (major, minor) == ("application", "pdf")
                        and not content.startswith(b"%PDF-")
                    ):
                        raise ValueError("pdf_attachment_payload_is_not_pdf")
                    if (major, minor) == ("text", "html"):
                        part = MIMEText(content.decode("utf8"), "html", "utf-8")
                    else:
                        part = MIMEApplication(content, minor)
                    part.add_header(
                        "Content-Disposition", "attachment", filename=filename
                    )
                    msg.attach(part)
                logger.info("邮件附件已添加")
            else:
                msg = inner

            # 邮件主题（使用UTF-8编码策略自动处理）
            msg["Subject"] = subject

            # 编码发件人和收件人
            msg["From"] = self.sender_email
            msg["To"] = self.receiver_email
            from email.utils import formatdate, make_msgid

            msg["Message-ID"] = make_msgid()
            msg["Date"] = formatdate(localtime=False)
            from email.utils import getaddresses

            recipients = list(
                dict.fromkeys(
                    address
                    for _name, address in getaddresses([self.receiver_email])
                    if address
                )
            )
            if not all(
                (self.smtp_server, self.sender_email, self.sender_password, recipients)
            ):
                return {"status": "failed", "reason": "smtp_configuration_incomplete"}

            guard = self._delivery_guard()
            if guard:
                return guard

            # 连接到SMTP服务器并发送邮件
            if self.enable_ssl:
                context = ssl.create_default_context()
                connection = smtplib.SMTP_SSL(
                    self.smtp_server, self.smtp_port, timeout=30, context=context
                )
            else:
                connection = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30)
            with connection as server:
                if self.enable_tls and not self.enable_ssl:
                    server.starttls()
                server.login(self.sender_email, self.sender_password)
                guard = self._delivery_guard()
                if guard:
                    receipt = guard
                    return receipt
                submitted = True
                try:
                    refused = server.send_message(msg)
                except Exception as exc:  # noqa: BLE001 -- optional placement metadata  # noqa: BLE001 - preserve before QUIT
                    receipt = self._smtp_failure_receipt(
                        exc,
                        submitted=True,
                        recipients=recipients,
                        message_id=msg["Message-ID"],
                        archive=copy_path,
                    )
                else:
                    receipt = self._smtp_recipient_receipt(refused, recipients)
                receipt["message_id"] = msg["Message-ID"]
                receipt["archive"] = str(copy_path) if copy_path else None
            return receipt
        except Exception as exc:  # noqa: BLE001 -- optional placement metadata  # noqa: BLE001 - classify transport stage
            logger.error("SMTP阶段异常: %s", type(exc).__name__)
            if receipt is not None:
                return {**receipt, "cleanup_error": type(exc).__name__}
            return self._smtp_failure_receipt(
                exc,
                submitted=submitted,
                recipients=recipients,
                message_id=msg.get("Message-ID") if submitted else None,
                archive=copy_path,
            )

    @staticmethod
    def _smtp_recipient_receipt(refused, recipients: list) -> dict:
        """A returned refusal map implies DATA acceptance only if well formed."""
        valid_map = isinstance(refused, dict) and set(refused) <= set(recipients)
        valid_values = valid_map and all(
            isinstance(value, (tuple, list)) and value and type(value[0]) is int
            for value in refused.values()
        )
        if not valid_values:
            return {
                "status": "unknown",
                "reason": "smtp_acknowledgement_missing",
                "pieces": [
                    {"recipient": address, "status": "unknown"}
                    for address in recipients
                ],
            }
        pieces = []
        for address in recipients:
            rejection = refused.get(address)
            pieces.append(
                {
                    "recipient": address,
                    "status": "failed" if rejection is not None else "sent",
                    "reason": "recipient_rejected"
                    if rejection is not None
                    else "smtp_accepted",
                    **({"smtp_code": rejection[0]} if rejection else {}),
                }
            )
        receipt = combine_receipts(pieces)
        receipt["smtp_accepted"] = any(item["status"] == "sent" for item in pieces)
        receipt["refused_count"] = len(refused)
        return receipt

    @staticmethod
    def _smtp_failure_receipt(
        exc: Exception,
        *,
        submitted: bool,
        recipients: list,
        message_id=None,
        archive=None,
    ) -> dict:
        rejected = isinstance(
            exc,
            (
                smtplib.SMTPSenderRefused,
                smtplib.SMTPRecipientsRefused,
                smtplib.SMTPDataError,
                smtplib.SMTPHeloError,
                smtplib.SMTPNotSupportedError,
            ),
        )
        status = "failed" if not submitted or rejected else "unknown"
        rejected_recipients = (
            exc.recipients if isinstance(exc, smtplib.SMTPRecipientsRefused) else {}
        )
        return {
            "status": status,
            "reason": "smtp_rejected"
            if rejected
            else ("smtp_submission_unknown" if submitted else "smtp_pre_submit_failed"),
            "error": type(exc).__name__,
            "archive": str(archive) if archive else None,
            "message_id": message_id,
            "pieces": [
                {
                    "recipient": address,
                    "status": status,
                    **(
                        {"smtp_code": rejected_recipients[address][0]}
                        if address in rejected_recipients
                        else {}
                    ),
                }
                for address in recipients
            ],
        }

    def send_deployment_notification(
        self, deployment_info=None, version=None, summary=None
    ):
        """
        发送部署通知邮件

        Returns:
            (ok, message): ok 为 True 表示邮件已发出，False 表示失败
        """
        try:
            # 获取服务器信息
            server_info = self._get_server_info()

            # 构建部署邮件主题
            subject = f"部署完成通知 - {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}"

            # 构建部署邮件正文
            body = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>部署完成通知</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: #333; border-bottom: 1px solid #ddd; padding-bottom: 10px; }}
        .info {{ margin: 15px 0; padding: 10px; background-color: #f5f5f5; border-radius: 5px; }}
        .success {{ color: #4caf50; font-weight: bold; }}
    </style>
</head>
<body>
    <h1>部署完成通知</h1>
    <p class="success">✅ 股票量化系统已成功部署到生产服务器</p>

    <div class="info">
        <p><strong>部署时间:</strong> {datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")}</p>
        <p><strong>部署服务器:</strong> {server_info["hostname"]}</p>
        <p><strong>服务器IP:</strong> {server_info["ip_address"]}</p>
        <p><strong>系统信息:</strong> {server_info["system"]} {server_info["machine"]} (内核: {server_info["kernel_version"]})</p>
        {f"<p><strong>部署版本:</strong> {version}</p>" if version else ""}
        {f"<p><strong>部署摘要:</strong> {summary}</p>" if summary else ""}
    </div>

    {f'<div class="info"><p><strong>部署详情:</strong> {deployment_info}</p></div>' if deployment_info else ""}

    <p><em>注：此邮件由股票量化系统自动发送，用于部署验证。</em></p>
</body>
</html>"""

            # 发送邮件
            receipt = normalize_receipt(self._send_email(subject, body))
            logger.info("部署通知邮件结果: %s", receipt["status"])
            return legacy_tuple(receipt)

        except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
            msg = str(e)
            logger.error(f"发送部署通知邮件失败: {msg}")
            return False, msg

    def send_optimizer_notification(
        self, report, group_name: str = "", full_report: dict | None = None
    ) -> None:
        """发送优化结果邮件。含完整回测报告（日回报测+敏感性+波动率）。"""
        body = build_optimizer_summary(report, group_name, full_report)
        subject = optimizer_notification_title(report, group_name)
        candlestick_png = (full_report or {}).get("candlestick_png")
        self._send_email(subject, body, candlestick_png=candlestick_png)

    def _save_email_copy(self, subject, body):
        """
        保存邮件副本到本地文件

        Args:
            subject: 邮件主题
            body: 邮件正文（HTML格式）
        """
        try:
            # 生成文件名：日期_时间_主题前30字符
            current_time = datetime.now().astimezone()
            date_str = current_time.strftime("%Y%m%d")
            time_str = current_time.strftime("%H%M%S")
            # 清理主题中的非法文件名字符
            clean_subject = "".join(
                c if c.isalnum() or c in " _-" else "_" for c in subject
            )
            clean_subject = clean_subject[:50]  # 限制长度

            filename = f"{date_str}_{time_str}_{clean_subject}.html"
            filepath = self.email_archive_dir / filename

            # 直接保存正文（已经是完整 HTML 文档，由统一日报或简报模板渲染）
            # 仅插入元数据注释，不嵌套 <html>
            meta_comment = (
                f"<!-- 主题: {subject} | "
                f"发送时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"收件人: {self.receiver_email} -->\n"
            )
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(meta_comment)
                f.write(body)

            logger.info(f"邮件副本已保存: {filepath}")
            return filepath

        except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
            logger.error(
                "保存邮件副本失败: %s, 目标目录: %s", e, self.email_archive_dir
            )
            return None

    # ── 日报 PDF 生成 ──

    def _chart_deviation_timeline(
        self,
        signal_scan,
        backtest,
        base64=True,
    ) -> str:
        """
        偏离度 30 日折线图: 取偏离绝对值最大的 5 只标的 + 触发信号的标的，
        叠加折线。虚线标注买入阈值。

        Returns:
            base64 PNG 字符串 或 HTML <img> 标签
        """
        import base64
        import io

        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        from ..utils.font_setup import setup_cjk_font

        setup_cjk_font()

        snapshot = getattr(signal_scan, "indicator_snapshot", {}) if signal_scan else {}
        if not snapshot:
            return ""

        # 取偏离度最大的 5 只
        dev_codes = []
        for code, vals in snapshot.items():
            d = vals.get("deviation", 0)
            dev_codes.append((code, abs(d), d))
        dev_codes.sort(key=lambda x: x[1], reverse=True)
        top5 = [c for c, _, _ in dev_codes[:5]]

        # 加触发信号的标的
        strategy_alerts = getattr(signal_scan, "alerts", []) if signal_scan else []
        for a in strategy_alerts:
            code = getattr(a, "stock_code", "")
            if code and code not in top5:
                top5.append(code)
        top5 = top5[:8]  # 最多 8 条线

        # 获取历史数据（需要 session._historical）
        # 这里只能从最近 60 天的历史中提取 deviation
        # 简化: 用 snapshot 做单点标注
        fig, ax = plt.subplots(figsize=(6.5, 2.2), dpi=120)
        colors = [
            "#2d8a56",
            "#c9a84c",
            "#2980b9",
            "#c0392b",
            "#8e44ad",
            "#e67e22",
            "#1abc9c",
            "#34495e",
        ]

        for i, code in enumerate(top5):
            vals = snapshot.get(code, {})
            d = vals.get("deviation", 0) * 100  # → %
            color = colors[i % len(colors)]
            ax.barh(i, d, color=color, height=0.5, alpha=0.85)
            label = f"{code[-4:]} {d:+.1f}%"
            x_pos = d + (0.5 if d >= 0 else -0.5)
            ha = "left" if d >= 0 else "right"
            ax.text(
                x_pos,
                i,
                label,
                va="center",
                ha=ha,
                fontsize=7,
                color=color,
                fontweight="bold",
            )

        # 买入阈值虚线
        ax.axvline(x=-0.5, color="#888", linestyle="--", linewidth=0.6, alpha=0.5)
        ax.text(
            -0.5,
            len(top5) - 0.3,
            " 买入阈值 -0.5%",
            fontsize=6,
            color="#888",
            va="bottom",
        )

        ax.set_yticks(range(len(top5)))
        ax.set_yticklabels([c[-4:] for c in top5], fontsize=7)
        ax.invert_yaxis()
        ax.set_xlabel("偏离度 %", fontsize=7)
        ax.axvline(x=0, color="#ccc", linewidth=0.5)
        ax.grid(axis="x", alpha=0.2)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        plt.tight_layout(pad=0.5)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        buf.seek(0)

        if base64:
            b64 = base64.b64encode(buf.read()).decode()
            return f'<img src="data:image/png;base64,{b64}" style="max-width:100%"/>'
        return buf.read()

    def _generate_daily_pdf(
        self,
        session,
        alert_stocks,
        signal_scan,
        backtest,
        stock_data,
    ) -> bytes | None:
        """Delegate to daily_pdf.generate_daily_pdf, injecting the helpers."""
        return generate_daily_pdf(
            session,
            alert_stocks,
            signal_scan,
            backtest,
            stock_data,
            chart_deviation_timeline=self._chart_deviation_timeline,
            pick_best_anchor=self._pick_best_anchor,
            get_server_info=self._get_server_info,
        )
