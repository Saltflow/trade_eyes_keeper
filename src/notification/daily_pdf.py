"""Shared daily PDF: current fundamentals, validated Ke curves and events."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..markets import _detect_fine_group
from .report_builders import (
    build_daily_event_rows,
    group_daily_event_rows,
    report_data_readiness,
)

logger = logging.getLogger(__name__)


def _converter_probe_preamble() -> str:
    """Cache the real driver probe before a large Unix TeX process can fork."""
    import subprocess

    result = subprocess.run(
        ["extractbb", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    lines = result.stdout.splitlines()
    first = next((line for line in lines if line.strip()), "")
    if (
        result.returncode != 0
        or not re.search(r"\bVersion +[0-9]{8}(?: +|$)", first)
        or len(re.findall(r"\bVersion\b", first)) != 1
        or not first.isascii()
        or any(character in first for character in "\\{}%#$&^_~")
        or any(not 32 <= ord(character) < 127 for character in first)
    ):
        raise ValueError("Unrecognized converter version probe")
    # Only this exact command uses the cached, actual first line. All other
    # commands retain the original public conditional and catcode setup. The
    # backend still parses the version and selects its own color/link behavior.
    return (
        "\\ExplSyntaxOn\n"
        r"\cs_new_eq:NN \tradeeyes_original_shell:nnNTF \sys_get_shell:nnNTF"
        "\n"
        r"\cs_set_protected:Npn \sys_get_shell:nnNTF #1#2#3#4#5"
        "\n{\n"
        r"\str_if_eq:nnTF {#1} {extractbb~--version}"
        "\n{\n"
        r"\cs_gset_eq:NN \sys_get_shell:nnNTF \tradeeyes_original_shell:nnNTF"
        "\n"
        r"\sys_if_shell:TF"
        "\n{\n"
        r"\tl_set_rescan:Nnn #3 {#2} {" + first.replace(" ", "~") + "~}\n#4\n}\n{\n"
        r"\tradeeyes_original_shell:nnNTF {#1} {#2} #3 {#4} {#5}"
        "\n}\n}\n{\n"
        r"\tradeeyes_original_shell:nnNTF {#1} {#2} #3 {#4} {#5}"
        "\n}\n}\n\\ExplSyntaxOff\n"
    )


def _compile_pdf(tex_file: Path) -> bytes | None:
    """Release XeTeX memory before converting the final XDV to PDF."""
    import subprocess

    if os.name != "nt":
        preamble = _converter_probe_preamble()
        if preamble:
            tex_file.write_text(
                preamble + tex_file.read_text(encoding="utf-8"), encoding="utf-8"
            )
    xdv_file = tex_file.with_suffix(".xdv")
    for _ in range(2):  # Preserve both passes for cross references.
        # A failed later pass must not reuse the previous pass's output.
        xdv_file.unlink(missing_ok=True)
        result = subprocess.run(
            [
                "xelatex",
                "-no-pdf",
                "-interaction=nonstopmode",
                "-output-directory",
                str(tex_file.parent),
                str(tex_file),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            log_file = tex_file.with_suffix(".log")
            log_tail = ""
            if log_file.exists():
                lines = log_file.read_text(errors="replace").split("\n")
                for index, line in enumerate(lines):
                    if line.startswith("!"):
                        log_tail = "\\n".join(lines[max(0, index - 1) : index + 5])
                        break
            logger.warning("xelatex 编译问题: %s", log_tail or result.stderr[-200:])

    if not xdv_file.exists():
        logger.error("xelatex 未产出 XDV")
        return None

    pdf_file = tex_file.with_suffix(".pdf")
    pdf_file.unlink(missing_ok=True)
    # These are XeTeX's default converter flags. Running after XeTeX exits
    # avoids its fork/exec briefly duplicating the resident TeX memory on Linux.
    result = subprocess.run(
        ["xdvipdfmx", "-q", "-E", "-o", str(pdf_file), str(xdv_file)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0 or not pdf_file.exists():
        logger.error("XDV 转 PDF 失败: %s", result.stderr[-200:])
        return None
    pdf_bytes = pdf_file.read_bytes()
    if not pdf_bytes.startswith(b"%PDF-"):
        logger.error("XDV 转换结果不是有效的 PDF 文件头")
        return None
    logger.info("日报 PDF 生成成功 (%d bytes)", len(pdf_bytes))
    return pdf_bytes


def generate_daily_pdf(
    session,
    stock_data,
    *,
    get_server_info,
) -> bytes | None:
    """生成日报 PDF（xelatex 编译 LaTeX 模板），返回 bytes"""
    import os
    import tempfile

    from .report_policy import (
        FUNDAMENTAL_COLUMNS,
        PRICE_COLORS,
        ROE_ESTIMATE_NOTE,
        fundamental_rows,
        generate_return_curve_png,
        implied_ke,
        price_movement,
        return_curve_markets,
        return_curve_notes,
    )

    config = getattr(session, "config", {})
    chart_path = None
    try:
        # 1. 图表 PNG
        curves = getattr(session, "strategy_return_curves", None)
        chart_buf = generate_return_curve_png(curves)
        chart_path = None
        if chart_buf and not isinstance(chart_buf, str):
            fd, chart_path = tempfile.mkstemp(suffix=".png", prefix="chart_")
            with open(fd, "wb") as f:
                data = (
                    chart_buf
                    if isinstance(chart_buf, bytes)
                    else chart_buf.getvalue()
                    if hasattr(chart_buf, "getvalue")
                    else chart_buf
                )
                f.write(data)
            chart_section = (
                f"\\includegraphics[width=\\textwidth]{{{Path(chart_path).as_posix()}}}"
            )
        else:
            chart_section = "{\\color{gray}\\small 今日无图表数据}"

        # ── LaTeX 转义函数 (必须在使用前定义) ──
        def _esc(s):
            """LaTeX 转义"""
            escaped = {
                "\\": r"\textbackslash{}",
                "&": r"\&",
                "%": r"\%",
                "#": r"\#",
                "$": r"\$",
                "_": r"\_",
                "{": r"\{",
                "}": r"\}",
                "~": r"\textasciitilde{}",
                "^": r"\textasciicircum{}",
            }
            return "".join(escaped.get(char, char) for char in str(s))

        ta, th, tu = [
            float(item["values"][-1] / item["initial_capital"] * 100 - 100)
            if item["status"] != "unavailable"
            else None
            for item in return_curve_markets(curves)
        ]
        kpi_a = f"{ta:+.1f}\\%" if ta is not None else "—"
        kpi_h = f"{th:+.1f}\\%" if th is not None else "—"
        kpi_u = f"{tu:+.1f}\\%" if tu is not None else "—"
        kpi_color_a = "green" if ta is not None and ta > 0 else "red"
        kpi_color_h = "green" if th is not None and th > 0 else "red"
        kpi_color_u = "green" if tu is not None and tu > 0 else "red"
        kpi_buy = str(len(stock_data))
        kpi_s, kpi_color_s = "历史模拟", "navy"
        readiness = report_data_readiness(session)
        readiness_section = ""
        if readiness["status"] == "not_ready":
            kpi_s, kpi_color_s = "数据未就绪", "red"
            labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
            issues = [
                f"{labels.get(group, group)}：{status}"
                for group, status in readiness["analysis_issues"].items()
            ]
            if readiness["missing_codes"]:
                issues.append("缺少标的：" + "、".join(readiness["missing_codes"]))
            if readiness["error_count"]:
                issues.append(f"采集错误：{readiness['error_count']}项")
            readiness_section = (
                r"{\color{red}\small 数据未就绪：" + _esc("；".join(issues)) + "}"
            )
            for group, key in (("a_share", "kpi_a"), ("hk", "kpi_h"), ("us", "kpi_u")):
                if group in readiness["analysis_issues"]:
                    if key == "kpi_a":
                        kpi_a = "受阻"
                    elif key == "kpi_h":
                        kpi_h = "受阻"
                    else:
                        kpi_u = "受阻"

        events = build_daily_event_rows(
            getattr(session, "announcements", None),
            getattr(session, "placements", None),
            stock_data,
            getattr(session, "dividend_events", None),
            as_of=getattr(session, "created_at", None),
            config=config,
        )
        events_section = ""
        if events:
            event_lines = [
                r"\begingroup",
                r"\section*{\color{navy}事件速览}",
                r"\small\begin{longtable}{p{.14\textwidth}p{.80\textwidth}}",
                r"\toprule 日期 & 详情 \\ \midrule\endhead",
            ]
            heading = None
            for group in group_daily_event_rows(events):
                current = (group["category"], group["market"])
                if current != heading:
                    event_lines.append(
                        r"\rowcolor{navy!8}\multicolumn{2}{l}{\textbf{"
                        + _esc(" · ".join(current))
                        + r"}} \\*"
                    )
                    heading = current
                event_lines.append(
                    r"\multicolumn{2}{l}{\color{navy}\textbf{"
                    + _esc(group["code"] + " " + group["name"])
                    + r"}} \\*"
                )
                for row in group["rows"]:
                    members = row.get("members") or [row]
                    if len(members) > 1:
                        links = [
                            r"\href{"
                            + _esc(member["url"])
                            + "}{"
                            + _esc(member["date"] or "日期待确认")
                            + "}"
                            if member["url"]
                            else _esc(member["date"] or "日期待确认")
                            for member in members
                        ]
                        event_lines.append(
                            "— & "
                            + _esc(f"{row['kind']} · {len(members)}份")
                            + r"\newline "
                            + " / ".join(links)
                            + r" \\"
                        )
                    else:
                        detail = _esc(row["title"])
                        if row["url"]:
                            detail += r"\newline\href{" + _esc(row["url"]) + r"}{原文}"
                        event_lines.append(
                            _esc(row["date"] or "日期待确认") + " & " + detail + r" \\"
                        )
            event_lines.extend([r"\bottomrule\end{longtable}", r"\endgroup"])
            events_section = "\n".join(event_lines)

        header_cols = [
            "标的",
            "今开",
            "昨收",
            "现价",
            "PE",
            "PB",
            "隐含 Ke",
            "ROE%",
        ]
        table_rows = []
        by_code = {str(row["stock_code"]): row for _, row in stock_data.iterrows()}
        fundamental = {
            row[0]: dict(zip((key for key, _ in FUNDAMENTAL_COLUMNS), row))
            for row in fundamental_rows(stock_data)
        }
        from .report_policy import DISPLAY_MARKETS

        for group, label in DISPLAY_MARKETS.items():
            codes = sorted(
                code for code in by_code if _detect_fine_group(code) == group
            )
            if not codes:
                continue
            table_rows.append(
                r"\rowcolor{navy!8}\multicolumn{8}{l}{\textbf{"
                + label
                + f" · {len(codes)}只"
                + r"}} \\*"
            )
            for code in codes:
                row, display = by_code[code], fundamental[code]
                direction, change = price_movement(
                    row.get("close"), row.get("prev_close")
                )
                color = PRICE_COLORS[direction].lstrip("#")
                current = (
                    r"\textcolor[HTML]{"
                    + color
                    + r"}{\shortstack[r]{\textbf{"
                    + _esc(display["close"])
                    + r"}\\{\tiny "
                    + _esc(change)
                    + "}}}"
                )
                ke = implied_ke(row.get("pe_ratio"))
                instrument = (
                    r"\textbf{"
                    + _esc(code)
                    + r"}\newline "
                    + _esc(display["stock_name"])
                )
                cells = [
                    instrument,
                    _esc(display["open"]),
                    _esc(display["prev_close"]),
                    current,
                    _esc(display["pe_ratio"]),
                    _esc(display["pb_ratio"]),
                    _esc(f"{ke:.1f}%" if ke is not None else "—"),
                    _esc(display["roe"]),
                ]
                table_rows.append(" & ".join(cells) + r" \\")
        table_section = (
            r"\scriptsize\renewcommand{\arraystretch}{1.35}\rowcolors{2}{white}{stripe}"
            + "\n"
            + r"\begin{longtable}{p{.25\textwidth}rrrrrrr}"
            + "\n"
            + r"\toprule "
            + " & ".join(_esc(label) for label in header_cols)
            + r" \\ \midrule\endhead"
            + "\n"
            + "\n".join(table_rows)
            + "\n"
            + r"\bottomrule\end{longtable}"
        )

        bt_text = (
            _esc(ROE_ESTIMATE_NOTE)
            + r"\par 价格对应各市场最近行情交易日；红涨绿跌，以现价相比昨收计算。"
        )

        # 7. 渲染模板
        tex_path = Path(__file__).parent.parent / "templates" / "report_daily.tex"
        template = tex_path.read_text(encoding="utf-8").replace("\r\n", "\n")

        info = get_server_info()
        report_time = pd.Timestamp(
            getattr(session, "created_at", None) or datetime.now().astimezone()
        )
        report_time = (
            report_time.tz_localize("Asia/Shanghai")
            if report_time.tzinfo is None
            else report_time.tz_convert("Asia/Shanghai")
        )
        html = template.replace(
            "\\VAR{report_date}", report_time.strftime("%Y-%m-%d %A")
        )
        html = html.replace("\\VAR{server_hostname}", info.get("hostname", ""))
        html = html.replace(
            "\\VAR{release_label}", _esc(info.get("release_label", "未登记发布"))
        )
        html = html.replace("\\VAR{kpi_buy}", kpi_buy)
        html = html.replace("\\VAR{kpi_buy_label}", "监控标的")
        html = html.replace(
            "\\VAR{chart_title}",
            "Ke策略收益曲线",
        )
        html = html.replace("\\VAR{kpi_a}", kpi_a)
        html = html.replace("\\VAR{kpi_h}", kpi_h)
        html = html.replace("\\VAR{kpi_u}", kpi_u)
        html = html.replace("\\VAR{kpi_s}", kpi_s)
        html = html.replace("\\VAR{kpi_color_a}", kpi_color_a)
        html = html.replace("\\VAR{kpi_color_h}", kpi_color_h)
        html = html.replace("\\VAR{kpi_color_u}", kpi_color_u)
        html = html.replace("\\VAR{kpi_color_s}", kpi_color_s)
        html = html.replace("\\VAR{readiness_section}", readiness_section)
        html = html.replace("\\VAR{events_section}", events_section)
        html = html.replace("\\VAR{chart_section}", chart_section)
        html = html.replace(
            "\\VAR{curve_notes}",
            "\\par\n".join(_esc(line) for line in return_curve_notes(curves)),
        )
        html = html.replace("\\VAR{table_section}", table_section)
        html = html.replace("\\VAR{fundamental_note}", bt_text)

        # 8. xelatex 编译
        with tempfile.TemporaryDirectory() as tmpdir:
            tex_file = Path(tmpdir) / "report.tex"
            tex_file.write_text(html, encoding="utf-8")

            return _compile_pdf(tex_file)

    except Exception as e:  # noqa: BLE001 -- classify report generation failure
        logger.error("生成日报 PDF 失败: %s", type(e).__name__)
        return None
    finally:
        if chart_path and os.path.exists(chart_path):
            os.unlink(chart_path)
