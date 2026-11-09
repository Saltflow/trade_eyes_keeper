"""
邮件通知模块
发送股票提醒邮件
"""

from __future__ import annotations

import hashlib
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
from uuid import uuid4

import pandas as pd

from ..markets import _detect_fine_group
from .base import BaseNotifier
from .receipts import combine_receipts, legacy_tuple, normalize_receipt
from .report_builders import (  # noqa: F401
    _fmt,
    _html_escape,
    _safe_html_url,
    build_daily_event_rows,
    build_strategy_text_summary,
    group_daily_event_rows,
)
from .report_policy import (
    PRICE_COLORS,
    ROE_ESTIMATE_NOTE,
    format_price,
    format_roe,
    fundamental_rows,
    generate_return_curve_png,
    implied_ke,
    price_movement,
    quote_presentation,
    quote_status_summary,
    return_curves_html,
)
from .settings import channel_enabled

logger = logging.getLogger(__name__)


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
        return self.send_daily_report_from_session(session)

    def send_daily_report_from_session(self, session):
        """
        从Session读取数据并发送每日报告（新数据流）

        Args:
            session: SessionContext对象
        """
        try:
            # 从Session读取所有数据
            stock_data = session.get_all_dataframe()
            announcements = session.announcements

            # 构建邮件主题
            subject = f"股票日报 - {datetime.now().astimezone().strftime('%Y-%m-%d')}"

            # 日报使用单张三市场 Ke 曲线总览图。
            portfolio_chart_dict = None
            try:
                overview_png = generate_return_curve_png(
                    getattr(session, "strategy_return_curves", None)
                )
                portfolio_chart_dict = (
                    {"overview": overview_png} if overview_png else None
                )
            except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
                logger.error(f"投资组合图表生成失败: {e}")

            # 生成日报 PDF 附件
            pdf_bytes = None
            try:
                pdf_bytes = self._generate_daily_pdf(session, stock_data)
            except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
                logger.warning("日报 PDF 生成失败: %s", e)

            # 构建完整日报正文。
            body = self._build_email_body(
                stock_data,
                announcements,
                portfolio_chart_dict=portfolio_chart_dict,
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

    def _daily_watchlist_rows(self, stock_data):
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
        group_order = {"a_share": 0, "hk": 1, "us": 2, "sg": 3}
        rows = []
        for code, configured_name in symbols:
            source_row = source_rows.get(code)
            source = source_row if source_row is not None else {}
            quote = quote_presentation(source)
            entry = (
                None
                if source_row is None or source.get("quote_status") == "unavailable"
                else {key: source.get(key) for key in ("open", "close", "prev_close")}
            )
            raw_name = (
                source_row.get("stock_name", configured_name or code)
                if source_row is not None
                else configured_name or code
            )
            name = code if raw_name is None or pd.isna(raw_name) else str(raw_name)
            group = _detect_fine_group(code)
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "group": group,
                    "entry": entry,
                    "quote": quote,
                    "pe_ratio": source_row.get("pe_ratio")
                    if source_row is not None
                    else None,
                    "pb_ratio": source_row.get("pb_ratio")
                    if source_row is not None
                    else None,
                    "roe": source_row.get("roe") if source_row is not None else None,
                }
            )
        return sorted(
            rows,
            key=lambda item: (
                group_order.get(item["group"], 99),
                item["code"],
            ),
        )

    def _build_daily_summary_section(self, rows):
        counts = {key: 0 for key in ("realtime", "delayed", "last_close", "unavailable")}
        for item in rows:
            counts[(item.get("quote") or quote_presentation({}))["status"]] += 1
        cells = [
            ("实时报价", counts["realtime"]),
            ("延迟采样", counts["delayed"]),
            ("最近收盘", counts["last_close"]),
            ("行情缺口", counts["unavailable"]),
            ("监控标的", len(rows)),
            ("策略展示", "Ke收益曲线"),
            ("覆盖市场", len({item["group"] for item in rows})),
        ]
        html = [
            (
                '<section class="daily-section summary-section">'
                '<div class="section-heading">今日概览</div>'
                '<table role="presentation" class="summary-grid"><tr>'
            )
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
                "<thead><tr><th>标的</th><th>开盘</th><th>前收</th><th>报价</th>"
                '<th>PE</th><th class="mobile-hide">PB</th><th>隐含 Ke</th>'
                '<th class="mobile-hide">ROE%</th></tr></thead><tbody>'
            )
            for index, row in enumerate(rows):
                entry = row["entry"]
                if entry is None:
                    open_text = prev_close_text = close_text = "—"
                    row_class = " watch-row-stale"
                else:
                    open_text = format_price(entry.get("open"))
                    prev_close_text = format_price(entry.get("prev_close"))
                    close_text = format_price(entry.get("close"))
                    row_class = ""
                pe_text = self._daily_metric(row.get("pe_ratio"), "", 1)
                pb_text = self._daily_metric(row.get("pb_ratio"), "", 2)
                implied_ke_text = self._daily_metric(
                    implied_ke(row.get("pe_ratio")), "%", 1
                )
                status = (
                    '<span class="status-badge">'
                    + _html_escape((row.get("quote") or quote_presentation({}))["text"])
                    + "</span>"
                )
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
            '<div class="muted-note">每行注明行情状态与源时间；延迟采样不代表实时，最近收盘不代表盘中报价。'
            '缺口价格显示为 —，已有基本面独立保留。前收使用行情源前收盘价，'
            "缺失时显示为 —。隐含 Ke = TTM 每股收益 ÷ 收盘价 + 2% "
            "（即 1 ÷ PE + 2%；PE≤0 或缺失时不显示）。ETF 的 PE/PB 按跟踪指数或"
            "公开成分股权重聚合；黄金、REIT 等非企业资产不展示企业 PE/PB。"
            + _html_escape(ROE_ESTIMATE_NOTE)
            + "</div>"
        )
        sections.append("</section>")
        return "".join(sections)

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

    def _daily_portfolio_chart_section(
        self, portfolio_chart_dict, strategy_return_curves=None
    ):
        source = (
            "cid:chart002" if (portfolio_chart_dict or {}).get("overview") else None
        )
        return (
            '<section class="daily-section portfolio-overview-section">'
            '<div class="section-heading">Ke策略收益曲线</div><div class="chart-card">'
            + return_curves_html(strategy_return_curves, source, compact=True)
            + "</div></section>"
        )

    def _build_email_body(
        self,
        stock_data,
        announcements=None,
        portfolio_chart_dict=None,
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
        watchlist_rows = self._daily_watchlist_rows(stock_data)
        report_time = pd.Timestamp(report_as_of or datetime.now().astimezone())
        report_time = (
            report_time.tz_localize("Asia/Shanghai")
            if report_time.tzinfo is None
            else report_time.tz_convert("Asia/Shanghai")
        )
        return template.format(
            current_time=report_time.strftime("%Y-%m-%d %H:%M:%S"),
            deployment_status=deployment_status,
            summary_section=self._build_daily_summary_section(watchlist_rows),
            watchlist_section=self._build_daily_watchlist_section(watchlist_rows),
            portfolio_chart_section=self._daily_portfolio_chart_section(
                portfolio_chart_dict, strategy_return_curves
            ),
            events_section=self._build_daily_events_section(
                announcements,
                placements,
                stock_data,
                dividend_events,
                report_as_of=report_as_of,
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
                    ["hostname", "-I"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
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
                        ["uname", "-r"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
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

    def send_brief_report(self, session, report_config: dict):
        try:
            subject, body = self._prepare_brief_report(session, report_config)
        except Exception as exc:  # noqa: BLE001 -- optional placement metadata  # noqa: BLE001 - classify preparation before SMTP
            logger.error("简报邮件准备失败: %s", type(exc).__name__)
            return {"status": "failed", "reason": "email_preparation_failed"}
        return self._send_email(subject, body)

    def _prepare_brief_report(self, session, report_config: dict):
        """Render current price/fundamentals and validated Ke curves."""
        from ..core.build_info import build_label
        from .report_policy import FUNDAMENTAL_COLUMNS

        label = report_config.get("label", "简报")
        stock_data = session.get_all_dataframe()
        today = datetime.now().astimezone()
        entries = fundamental_rows(stock_data)
        rows = "".join(
            "<tr>" + "".join(f"<td>{_html_escape(v)}</td>" for v in row) + "</tr>"
            for row in entries
        )
        template_dir = Path(__file__).parent.parent / "templates"
        template = (template_dir / "brief_email.html").read_text(encoding="utf-8")
        body = template.format(
            release_label=_html_escape(build_label()),
            label=label,
            report_date=today.strftime("%Y-%m-%d"),
            current_time=today.strftime("%H:%M"),
            active_count=sum(
                quote_presentation(row)["status"] != "unavailable"
                for _, row in stock_data.iterrows()
            ),
            total_count=len(entries),
            brief_rows=rows,
            brief_columns="".join(
                f"<th>{column}</th>" for _, column in FUNDAMENTAL_COLUMNS
            ),
            brief_note=quote_status_summary(stock_data)
            + "。每行按源时间展示行情状态；延迟采样不代表实时，最近收盘不代表盘中报价。"
            + "缺口价格与缺失指标显示为 —。"
            + ROE_ESTIMATE_NOTE,
            strategy_suggestions=return_curves_html(
                getattr(session, "strategy_return_curves", None)
            ),
        )
        return f"{label} - {today:%Y-%m-%d}", body

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
        """

        guard = self._delivery_guard()
        if guard:
            return guard

        # 保存邮件副本（无论是否跳过发送）
        copy_path, copy_sha256 = self._save_email_copy(subject, body) or (None, None)
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

            has_any_chart = chart_png_bytes or portfolio_chart_dict

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
                    if (major, minor) == (
                        "application",
                        "pdf",
                    ) and not content.startswith(b"%PDF-"):
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
                        archive_sha256=copy_sha256,
                    )
                else:
                    receipt = self._smtp_recipient_receipt(refused, recipients)
                receipt["message_id"] = msg["Message-ID"]
                receipt["archive"] = str(copy_path) if copy_path else None
                receipt["archive_sha256"] = copy_sha256
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
                archive_sha256=copy_sha256,
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
        archive_sha256=None,
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
            "archive_sha256": archive_sha256,
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

    def _save_email_copy(self, subject: str, body: str) -> tuple[Path, str | None] | None:
        """
        保存邮件副本到本地文件

        Args:
            subject: 邮件主题
            body: 邮件正文（HTML格式）

        Returns:
            新副本路径和保存时的哈希；哈希不可用时不影响后续SMTP结果。
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

            filename = f"{date_str}_{time_str}_{clean_subject}_{uuid4().hex}.html"
            filepath = self.email_archive_dir / filename

            # 直接保存正文（已经是完整 HTML 文档，由统一日报或简报模板渲染）
            # 仅插入元数据注释，不嵌套 <html>
            meta_comment = (
                f"<!-- 主题: {subject} | "
                f"发送时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"收件人: {self.receiver_email} -->\n"
            )
            content = (meta_comment + body).encode("utf-8")
            with open(filepath, "xb") as f:
                f.write(content)

            # Pin the bytes saved for this message, never certify them during reads.
            # Archival evidence failure cannot change an SMTP acknowledgement.
            try:
                digest = hashlib.sha256(content).hexdigest()
            except Exception as exc:  # noqa: BLE001 - optional archival evidence
                logger.warning("邮件副本哈希不可用: %s", type(exc).__name__)
                digest = None

            logger.info(f"邮件副本已保存: {filepath}")
            return filepath, digest

        except Exception as e:  # noqa: BLE001 -- contain optional report/OS/archive failures
            logger.error(
                "保存邮件副本失败: %s, 目标目录: %s", e, self.email_archive_dir
            )
            return None

    # ── 日报 PDF 生成 ──

    def _generate_daily_pdf(self, session, stock_data):
        return generate_daily_pdf(
            session, stock_data, get_server_info=self._get_server_info
        )
