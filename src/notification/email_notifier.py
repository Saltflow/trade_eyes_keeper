"""
邮件通知模块
发送股票提醒邮件
"""

import logging
import smtplib
import ssl
import pandas as pd
import socket
import platform
import subprocess
from html import escape
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email import policy
from datetime import datetime
from pathlib import Path

from .chart_generator import generate_combined_chart
from .base import BaseNotifier

logger = logging.getLogger(__name__)


def _fmt(v, unit="", fmt_spec=".2f"):
    """安全格式化数值：None/pd.NA/NaN → "—"，数值 → 格式化。

    原则：绝不凭空造零。缺失数据和真实零值必须可区分。
    """
    if v is None:
        return "—"
    try:
        if pd.isna(v):
            return "—"
    except (TypeError, ValueError):
        pass
    if unit:
        return f"{v:{fmt_spec}}{unit}"
    return f"{v:{fmt_spec}}"


from .base import BaseNotifier

# ... (keep all existing imports and code)


def build_brief_entries(stock_data, today) -> list[dict]:
    """从 DataFrame 提取简报行。Email/Feishu/Telegram 三端共享。

    1. 过滤最近 3 天内有交易的标的
    2. 收集 MA60/WMA20/WMA30/WMA50 锚点
    3. 用 _pick_best_anchor 选最优锚点
    4. 不在预警区间 → MA60 兜底
    5. 按偏离率升序排列（跌幅越大越靠前）

    Returns:
        [{code, name, close, open, anchor_name, anchor_val, dev_pct, dev_str}]
        dev_pct 为 None 时排到最后，dev_str 为 "-"
    """
    import pandas as pd
    from datetime import datetime as dt_mod

    today_date = today.date() if hasattr(today, "date") else today
    entries = []

    for _, row in stock_data.iterrows():
        code = str(row.get("stock_code", ""))
        name = str(row.get("stock_name") or code)
        close_price = row.get("close")
        open_price = row.get("open")

        # 3 天日期过滤
        data_date = row.get("date")
        in_trading = False
        if data_date is not None and not pd.isna(data_date):
            try:
                date_str = str(data_date)[:10]
                data_dt = dt_mod.strptime(date_str, "%Y-%m-%d").date()
                days_since = (today_date - data_dt).days
                in_trading = 0 <= days_since <= 3
            except Exception:
                continue
        if not in_trading:
            continue

        # 收集锚点
        anchors = {}
        for an in ("ma60", "wma20", "wma30", "wma50"):
            v = row.get(an)
            if v is not None and not pd.isna(v):
                anchors[an] = float(v)

        # 最优锚点 + MA60 兜底
        dev_pct = None
        anchor_name = "-"
        anchor_val = None
        if close_price is not None and not pd.isna(close_price) and anchors:
            best = EmailNotifier._pick_best_anchor(float(close_price), anchors)
            if best:
                anchor_name, anchor_val, dev_pct = best
            else:
                # MA60 兜底：不在预警区间也显示偏离
                ma60_v = anchors.get("ma60") or row.get("ma60")
                if ma60_v is not None and not pd.isna(ma60_v) and float(ma60_v) > 0:
                    ma60_v = float(ma60_v)
                    dev_pct = (float(close_price) - ma60_v) / ma60_v * 100
                    anchor_name = "ma60"
                    anchor_val = ma60_v

        entries.append({
            "code": code,
            "name": name,
            "close": close_price,
            "open": open_price,
            "anchor_name": anchor_name,
            "anchor_val": anchor_val,
            "dev_pct": dev_pct,
            "dev_str": f"{dev_pct:+.2f}%" if dev_pct is not None else "-",
            "sort_key": dev_pct if dev_pct is not None else float("inf"),
        })

    entries.sort(key=lambda x: x["sort_key"])
    return entries


def build_strategy_suggestions(stock_data, today=None) -> dict | None:
    """从最新优化结果生成策略建议（简报用）。

    1. 读取 data/optimizer/ 下最新的 A 股策略 YAML
    2. 取 Top1 策略的买入规则
    3. 对每只标的评估是否有买入信号触发
    4. 返回结构化建议数据

    Returns:
        None 如果没有优化结果
        {
            "strategy_label": "deep_value + deviation_absolute",
            "active_count": 3,
            "total_count": 10,
            "entries": [{code, name, close, signals: [str]}],
            "html_rows": "HTML 表格行",
            "text_rows": "纯文本行",
        }
    """
    from pathlib import Path
    from datetime import datetime

    today_date = today.date() if hasattr(today, "date") else (today or datetime.now().date())

    # 空数据直接返回
    if stock_data is None or stock_data.empty or "close" not in stock_data.columns:
        return None

    # 找最新 A 股优化结果
    opt_dir = Path("data/optimizer")
    yaml_files = sorted(
        [f for f in opt_dir.glob("*_a_share_strategies.yaml")
         if "non_a_share" not in f.name],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not yaml_files:
        return None
    try:
        with open(yaml_files[0], "r", encoding="utf-8") as f:
            import yaml
            data = yaml.safe_load(f)
        strategies = data.get("strategies", [])
        if not strategies:
            return None
        top = strategies[0]
        params = top.get("params", {})
    except Exception:
        return None

    # 解析买入规则
    buy_rules = []
    for i in range(1, 6):
        signal = params.get(f"buy_{i}_signal", "none")
        if signal == "none" or not signal:
            continue
        t_raw = float(params.get(f"buy_{i}_t", "0.3"))
        frac = float(params.get(f"buy_{i}_frac", "0.15"))
        buy_rules.append((signal, t_raw, frac))

    if not buy_rules:
        return None

    # 构建策略标签
    unique_signals = list(dict.fromkeys(s[0] for s in buy_rules))
    strategy_label = " + ".join(unique_signals[:3])

    # 补算新因子需要的列
    df = stock_data.copy()
    close = df["close"].astype(float)
    if "ma60" in df.columns:
        ma60 = df["ma60"].astype(float)
    else:
        ma60 = close.rolling(window=60, min_periods=1).mean()

    # deep_value needs ma200_dev, ma60_slope
    need_ma200 = any("ma200" in r[0] or r[0] == "deep_value" for r in buy_rules)
    need_slope = any("slope" in r[0] or r[0] == "deep_value" for r in buy_rules)
    need_ath = any("ath" in r[0] or "discount" in r[0] for r in buy_rules)

    if need_ma200 and "ma200_dev" not in df.columns:
        ma200 = close.rolling(window=200, min_periods=1).mean()
        df["ma200_dev"] = (close - ma200) / ma200.replace(0, float("nan"))
    if need_slope and "ma60_slope" not in df.columns:
        df["ma60_slope"] = ma60 / ma60.shift(20).replace(0, float("nan")) - 1.0
    if need_ath and "pct_from_ath" not in df.columns:
        ath = close.rolling(window=504, min_periods=1).max()
        df["pct_from_ath"] = close / ath.replace(0, float("nan")) - 1.0

    # 对每只标的评估信号
    entries = []
    for _, row in df.iterrows():
        code = str(row.get("stock_code", ""))
        name = str(row.get("stock_name") or code)
        c = row.get("close")
        if c is None or pd.isna(c):
            continue
        c = float(c)

        # 3 天日期过滤（同 brief 逻辑）
        data_date = row.get("date")
        if data_date is not None and not pd.isna(data_date):
            try:
                data_dt = datetime.strptime(str(data_date)[:10], "%Y-%m-%d").date()
                days_since = (today_date - data_dt).days
                if days_since < 0 or days_since > 3:
                    continue
            except Exception:
                continue

        active_signals = []
        for signal, t_raw, frac in buy_rules:
            triggered = False
            try:
                if signal == "deviation_cross":
                    dev = float(row.get("deviation", np.nan))
                    t = -0.005 + t_raw * (-0.295)
                    triggered = not np.isnan(dev) and dev <= t
                elif signal == "deviation_absolute":
                    dev = float(row.get("deviation", np.nan))
                    t = t_raw * -0.40
                    triggered = not np.isnan(dev) and dev <= t
                elif signal == "rsi_signal":
                    rsi = float(row.get("rsi", np.nan))
                    t = 10 + (1.0 - t_raw) * 30
                    triggered = not np.isnan(rsi) and rsi < t
                elif signal == "deep_value":
                    dev200 = float(df.loc[row.name, "ma200_dev"]) if "ma200_dev" in df.columns else np.nan
                    slope = float(df.loc[row.name, "ma60_slope"]) if "ma60_slope" in df.columns else np.nan
                    t = -0.05 + t_raw * (-0.35)
                    triggered = (
                        not np.isnan(dev200) and not np.isnan(slope)
                        and dev200 <= t and slope > -0.005
                    )
                elif signal == "absolute_discount":
                    pct_ath = float(df.loc[row.name, "pct_from_ath"]) if "pct_from_ath" in df.columns else np.nan
                    t = -0.10 + t_raw * (-0.60)
                    triggered = not np.isnan(pct_ath) and pct_ath <= t
                elif signal == "trend_follow":
                    adx = float(row.get("adx", np.nan))
                    macd = float(row.get("macd_hist", np.nan))
                    t = 15 + t_raw * 25
                    triggered = not np.isnan(adx) and not np.isnan(macd) and adx > t and macd > 0
                elif signal == "volume_spike":
                    vr = float(row.get("vol_ratio", np.nan))
                    t = 1.2 + t_raw * 2.8
                    triggered = not np.isnan(vr) and vr > t
                elif signal == "bollinger_signal":
                    bb = float(row.get("boll_pct_b", np.nan))
                    t = (1.0 - t_raw) * 0.35
                    triggered = not np.isnan(bb) and bb < t
            except Exception:
                continue

            if triggered:
                active_signals.append(f"{signal}({frac*100:.0f}%)")

        entries.append({
            "code": code,
            "name": name,
            "close": round(c, 2),
            "signals": active_signals,
            "signal_count": len(active_signals),
        })

    # 排序：有信号的排前面
    entries.sort(key=lambda x: (-x["signal_count"], x["code"]))

    # 生成 HTML 行
    html_parts = []
    for e in entries:
        sigs = ", ".join(e["signals"]) if e["signals"] else "—"
        css = "color:#27ae60;font-weight:bold" if e["signals"] else ""
        html_parts.append(
            f"<tr><td>{e['code']}</td><td>{e['name']}</td>"
            f"<td>{e['close']:.2f}</td>"
            f'<td style="{css}">{sigs}</td></tr>'
        )

    # 生成纯文本行（飞书用）
    text_parts = []
    for e in entries:
        sigs = ", ".join(e["signals"]) if e["signals"] else "—"
        text_parts.append(f"{e['code']:<8} {e['name'][:6]:<6} {e['close']:>7.2f}  {sigs}")

    active_count = sum(1 for e in entries if e["signals"])
    return {
        "strategy_label": strategy_label,
        "active_count": active_count,
        "total_count": len(entries),
        "entries": entries,
        "html_rows": "\n".join(html_parts),
        "text_rows": "\n".join(text_parts),
    }


SIGNAL_NAMES = {
    "deviation_cross": "偏离穿越",
    "deviation_absolute": "偏离达标",
    "rsi_signal": "RSI超卖",
    "bollinger_signal": "布林低位",
    "volume_spike": "放量异动",
    "trend_follow": "趋势跟踪",
    "deep_value": "深度价值",
    "absolute_discount": "绝对折价",
    "sell_deviation_cross": "偏离穿越(卖)",
    "sell_deviation_absolute": "偏离达标(卖)",
    "sell_rsi_signal": "RSI超买",
    "sell_bollinger_signal": "布林高位",
    "sell_trend_follow": "趋势反转",
    "sell_overextended": "超涨卖出",
    "none": "无",
}


def _build_signal_label_map(group: str = "a_share") -> dict[str, str]:
    """读指定分组最新优化器 YAML，返回 {buy_1: 偏离穿越, buy_2: RSI超卖, ...}

    Args:
        group: "a_share" 或 "non_a_share" — 不同组信号名不同，必须分开读
    """
    try:
        import yaml
        opt_dir = Path("data/optimizer")
        is_non_a = (group == "non_a_share")
        yaml_files = sorted(
            [f for f in opt_dir.glob(f"*_{group}_strategies.yaml")
             if ("non_a_share" in f.name) == is_non_a],
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not yaml_files:
            return {}
        with open(yaml_files[0], "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        strategies = data.get("strategies", [])
        if not strategies:
            return {}
        params = strategies[0].get("params", {})
        label_map: dict[str, str] = {}
        for k, v in params.items():
            if not k.endswith("_signal"):
                continue
            idx = k.split("_")[1]  # "buy_1_signal" → "1"
            rule_id = (
                f"buy_{idx}" if k.startswith("buy")
                else f"sell_{idx}"
            )
            label_map[rule_id] = SIGNAL_NAMES.get(v, v)
        logger.debug(f"Signal label map: {label_map}")
        return label_map
    except Exception as e:
        logger.warning(f"读取信号标签映射失败: {e}")
        return {}


def _readable_signal(code: str, rule_label: str, map_a: dict, map_hk: dict,
                     map_us: dict = None) -> str:
    """按标的所属细分组把 rule_id(如 buy_1) 翻译成信号名(如 趋势跟踪)。

    A股/港股/美股各用自己的 YAML 映射 — 三组信号名可能都不同。
    map_us 缺省时港美股共用 map_hk（向后兼容非A单组）。
    """
    try:
        from ..analysis.portfolio_strategy import _detect_fine_group
    except (ImportError, ValueError):
        from analysis.portfolio_strategy import _detect_fine_group
    g = _detect_fine_group(str(code))
    if g == "a_share":
        m = map_a
    elif g == "hk":
        m = map_hk
    else:  # us
        m = map_us if map_us is not None else map_hk
    return m.get(rule_label, rule_label)


def build_strategy_text_summary(session, markdown: bool = False) -> str:
    """构建搜参策略 + 今日信号 + 定增的纯文本摘要（Telegram/飞书共享）。

    与邮件日报信息量对齐：搜参3组(A股/港股/美股) + 验证期胜率 + 平均现金仓位
    + 今日信号(可读名) + 未解禁定增。

    Args:
        session: SessionContext（读 portfolio_results/signal_scan/placements/
                 opt_data_a/opt_data_non_a）
        markdown: True=飞书(**粗体**), False=Telegram(纯文本)
    """
    b = "**" if markdown else ""  # 粗体标记
    portfolio_results = getattr(session, "portfolio_results", None)
    signal_scan = getattr(session, "signal_scan", None)
    placements = getattr(session, "placements", None)
    opt_n = getattr(session, "opt_data_non_a", None)
    yaml_by_group = {
        "a_share": getattr(session, "opt_data_a", None),
        "hk": getattr(session, "opt_data_hk", None) or opt_n,
        "us": getattr(session, "opt_data_us", None) or opt_n,
    }
    benchmark = getattr(session, "_historical", {}) or {}

    lines: list[str] = []

    # 策略引擎名（新/旧引擎一目了然）
    first_yaml = yaml_by_group.get("a_share") or yaml_by_group.get("hk") or yaml_by_group.get("us")
    if first_yaml:
        engine_id = (first_yaml.get("strategies") or [{}])[0].get("params", {}).get("_engine", "global")
        engine_names = {"percentile": "分位评分", "global": "全局阈值"}
        engine_label = engine_names.get(engine_id, engine_id)
        ts_raw = first_yaml.get("timestamp", "")[:16].replace("T", " ")
        lines.append(f"{b}搜参引擎{b}: {engine_label} ({engine_id})  {ts_raw}")
        lines.append("")

    # ── 搜参策略结果（三组）──
    if portfolio_results:
        group_labels = {"a_share": "A股组合", "hk": "港股组合", "us": "美股组合"}
        # 每组三基线：(展示名, 价格基准code 或 None=无风险, 无风险年化)
        bench_sets = {
            "a_share": [("510880", "510880", None), ("沪深300", "510300", None),
                        ("无风险", None, 0.02)],
            "hk": [("VOO", "VOO", None), ("BRK.B", "BRK.B", None),
                   ("无风险", None, 0.038)],
            "us": [("VOO", "VOO", None), ("BRK.B", "BRK.B", None),
                   ("无风险", None, 0.038)],
        }
        for gk, gl in group_labels.items():
            gd = portfolio_results.get(gk)
            if not gd:
                continue
            r = gd.get("top1") or gd.get("max_return")
            if not r:
                continue
            # YAML 权威预估收益（各组用自己的 YAML）
            g_yaml = yaml_by_group.get(gk)
            g_top = ((g_yaml or {}).get("strategies") or [{}])[0]
            test_ret = g_top.get("test_return")
            test_dd = g_top.get("test_drawdown")
            g_sharpe = g_top.get("sharpe")
            ts = (g_yaml or {}).get("timestamp", "")[:16].replace("T", " ")

            qh = getattr(r, "quarterly_holdings", None) or []
            cash_pcts = [(100 - q["pos_pct"]) for q in qh if q.get("nav", 0) > 0]
            avg_cash = sum(cash_pcts) / len(cash_pcts) if cash_pcts else None
            comp = getattr(r, "composition", [])

            # 验证期胜率（三基线各算一个）
            wr_parts = []
            for disp, bcode, rf in bench_sets.get(gk, []):
                if bcode is not None:
                    wr, wd, td, _ = EmailNotifier._calc_validation_winrate(
                        r, benchmark.get(bcode), months=9,
                    )
                else:
                    wr, wd, td = EmailNotifier._calc_winrate_vs_riskfree(
                        r, annual_rate=rf, months=9,
                    )
                if wr is not None:
                    wr_parts.append(f"{disp} {wr:.0f}%")

            if test_ret is not None:
                head = (f"{b}{gl}{b} 预估收益(测试期超额,近9月) {test_ret:+.1f}%"
                        f"  回撤 {test_dd:.1f}%  夏普 {g_sharpe:.2f}")
            else:
                tr = getattr(r, "total_return", 0)
                head = f"{b}{gl}{b} 收益 {tr:+.1f}%"
            if avg_cash is not None:
                head += f"  平均现金仓位 {avg_cash:.0f}%"
            lines.append(head)
            if wr_parts:
                lines.append(
                    "  验证期胜率(任意日买入持有到期跑赢): "
                    + " | ".join(wr_parts)
                )
            if comp:
                lines.append(f"  成分: {', '.join(comp)}")
            if ts:
                lines.append(f"  搜参时间 {ts}")

            # 买卖规则明细（从 YAML params 翻译）
            params = g_top.get("params", {})
            buy_rules, sell_rules = [], []
            for k, v in sorted(params.items()):
                if not k.endswith("_signal"):
                    continue
                idx = k.split("_")[1]
                name = SIGNAL_NAMES.get(str(v), str(v))
                if str(v) == "none":
                    continue
                t = params.get(k.replace("_signal", "_t"))
                frac = params.get(k.replace("_signal", "_frac"))
                extra = ""
                if t is not None:
                    extra += f" 阈值{float(t):.2f}"
                if frac is not None:
                    extra += f" 仓位{float(frac)*100:.0f}%"
                if k.startswith("buy"):
                    buy_rules.append(f"买{idx}:{name}{extra}")
                else:
                    sell_rules.append(f"卖{idx}:{name}{extra}")
            if buy_rules:
                lines.append("  买入: " + " | ".join(buy_rules))
            if sell_rules:
                lines.append("  卖出: " + " | ".join(sell_rules))

            # 季末持仓（最后一个季度快照）
            if qh:
                last_q = qh[-1]
                qpos = last_q.get("positions", [])
                if qpos:
                    pos_str = ", ".join(
                        f"{p['code']} {p['shares']:.0f}股@{p['price']:.2f}"
                        f"({p.get('pnl_pct', 0):+.0f}%)"
                        for p in qpos[:8]
                    )
                    lines.append(f"  期末持仓(Q{last_q.get('quarter','?')}): {pos_str}")
            lines.append("")

    # ── 今日信号 ──
    alerts = getattr(signal_scan, "alerts", None) or [] if signal_scan else []
    if alerts:
        map_a = _build_signal_label_map("a_share")
        map_hk = _build_signal_label_map("hk") or _build_signal_label_map("non_a_share")
        map_us = _build_signal_label_map("us") or _build_signal_label_map("non_a_share")
        codes = set()
        for a in alerts:
            codes.add(getattr(a, "stock_code", "?"))
        lines.append(f"{b}今日信号{b} ({len(alerts)}条 / {len(codes)}只)")
        for a in alerts[:30]:
            code = getattr(a, "stock_code", "?")
            raw = getattr(a, "rule_label", "?")
            readable = _readable_signal(code, raw, map_a, map_hk, map_us)
            cv = getattr(a, "current_value", "-")
            lines.append(f"  {code} {readable}  {cv}")
        lines.append("")
    elif signal_scan is not None:
        lines.append(f"{b}今日信号{b}: 无触发")
        lines.append("")

    # ── 未解禁定增 ──
    if placements:
        name_map = {}
        try:
            df = session.get_all_dataframe()
            for _, row in df.iterrows():
                name_map[str(row.get("stock_code", ""))] = row.get("stock_name", "")
        except Exception:
            pass
        lines.append(f"{b}未解禁定增{b}")
        for code, p in sorted(placements.items()):
            name = name_map.get(code, "")
            num = p.get("issue_num")
            num_str = f"{num / 1e8:.2f}亿股" if num else "—"
            price = p.get("issue_price")
            price_str = f"{price:.2f}元" if price else "—"
            pct = p.get("pct_of_total")
            pct_str = f"{pct:.2f}%" if pct is not None else "—"
            unlock = p.get("unlock_date") or "—"
            lines.append(
                f"  {code} {name}  {num_str}  占{pct_str}  {price_str}  解禁{unlock}"
            )
        lines.append("")

    # ── 公告 / 股息 ──
    announcements = getattr(session, "announcements", None) or {}
    ann_lines = []
    for code, items in announcements.items():
        if not items:
            continue
        for a in items[:2]:  # 每只最多2条
            title = a.get("title", "")
            date = str(a.get("date", ""))[:10]
            # 优先展示 LLM 提取的分红
            div = a.get("llm_extracted_dividend") or {}
            cash = div.get("cash_dividend_per_share") or div.get("dividend_per_share")
            if cash:
                ann_lines.append(f"  {code} {date} 分红{cash:.3f}元/股")
            elif title:
                ann_lines.append(f"  {code} {date} {title[:24]}")
    if ann_lines:
        lines.append(f"{b}公告/股息{b}")
        lines.extend(ann_lines[:15])
        lines.append("")

    return "\n".join(lines).rstrip()


def build_optimizer_summary(report, group_name: str = "") -> str:
    """将 OptimizationReport 格式化为人话摘要。"""
    lines = ["<b>策略优化完成</b>"]
    if group_name:
        label = {"a_share": "A股", "hk": "港股", "us": "美股",
                 "non_a_share": "非A股"}.get(group_name, group_name)
        lines.append(f"分组: {label}")
    lines.append(
        f"耗时: {report.elapsed_seconds:.0f}s  |  "
        f"评估: {report.iterations} 策略"
    )

    if not report.top_strategies:
        return "\n".join(lines) + "\n(无有效策略)"

    # 买入卖出信号中文映射
    SIGNAL_NAMES = {
        "deviation_cross": "偏离穿越",
        "deviation_absolute": "偏离达标",
        "rsi_signal": "RSI超卖",
        "bollinger_signal": "布林低位",
        "volume_spike": "放量异动",
        "trend_follow": "趋势跟踪",
        "sell_deviation_cross": "偏离穿越(卖)",
        "sell_deviation_absolute": "偏离达标(卖)",
        "sell_rsi_signal": "RSI超买",
        "sell_bollinger_signal": "布林高位",
        "sell_trend_follow": "趋势反转",
        "none": "无",
    }

    lines.append("")
    for i, t in enumerate(report.top_strategies[:3]):
        lines.append(f"<b>策略 #{i+1}</b>")
        lines.append(
            f"  收益 {t.test_return:+.1f}%  回撤 {t.test_drawdown:.1f}%  "
            f"夏普 {t.sharpe:.2f}  交易 {t.trade_count}笔"
        )

        # 翻译参数为人话
        buy_rules = []
        sell_rules = []
        for k, v in t.params.items():
            if k.startswith("_"):
                continue
            if k.startswith("buy_"):
                idx = k.split("_")[1]
                if k.endswith("_signal"):
                    name = SIGNAL_NAMES.get(str(v), str(v))
                    buy_rules.append(f"买{idx}: {name}")
                elif k.endswith("_t"):
                    buy_rules.append(f"买{idx}阈值: {float(v):.3f}")
                elif k.endswith("_frac"):
                    buy_rules.append(f"买{idx}仓位: {float(v)*100:.0f}%")
            elif k.startswith("sell_"):
                idx = k.split("_")[1]
                if k.endswith("_signal"):
                    name = SIGNAL_NAMES.get(str(v), str(v))
                    sell_rules.append(f"卖{idx}: {name}")
                elif k.endswith("_t"):
                    sell_rules.append(f"卖{idx}阈值: {float(v):.3f}")
                elif k.endswith("_frac"):
                    sell_rules.append(f"卖{idx}仓位: {float(v)*100:.0f}%")

        if buy_rules:
            lines.append(f"  买入: {'  '.join(buy_rules)}")
        if sell_rules:
            lines.append(f"  卖出: {'  '.join(sell_rules)}")

        # 策略人话描述
        desc = getattr(t, "strategy_description", "")
        if desc:
            lines.append(f"  <pre>{desc}</pre>")

        # 季末持仓
        qh = getattr(t, "quarterly_holdings", None) or []
        if qh:
            lines.append("  <b>季末持仓明细:</b>")
            lines.append("  <table style='font-size:12px;border-collapse:collapse'>"
                         "<tr><th>Q</th><th>代码</th><th>持股</th>"
                         "<th>成本</th><th>现价</th><th>市值</th>"
                         "<th>盈亏</th><th>盈亏%</th></tr>")
            for q in qh:
                qn = q["quarter"]
                qd = q["day"]
                qp = q["pos_pct"]
                qnv = q["nav"]
                qcs = q["cash"]
                qpos = q.get("positions", [])
                if not qpos:
                    lines.append(f"<tr><td>Q{qn}</td><td colspan=7>空仓 (nav={qnv:.0f})</td></tr>")
                for pos in qpos:
                    code = pos["code"]
                    sh = pos["shares"]
                    cb = pos["cost"]
                    px = pos["price"]
                    vl = pos["value"]
                    pn = pos["pnl"]
                    pp = pos["pnl_pct"]
                    color = "#27ae60" if pn >= 0 else "#c0392b"
                    lines.append(
                        f"<tr><td>Q{qn}</td><td>{code}</td><td>{sh:.0f}股</td>"
                        f"<td>{cb:.2f}</td><td>{px:.2f}</td><td>{vl:.0f}</td>"
                        f"<td style='color:{color}'>{pn:+.0f}</td><td style='color:{color}'>{pp:+.1f}%</td></tr>"
                    )
                if qpos:
                    lines.append(f"<tr><td>Q{qn}</td><td colspan=4>现金: {qcs:.0f}</td>"
                                 f"<td colspan=3>仓位: {qp:.0f}%</td></tr>")
            lines.append("</table>")

        lines.append("")

    rid = getattr(report, "report_id", "")
    lines.append(f"完整结果: data/optimizer/{rid}_strategies.yaml")
    return "\n".join(lines)


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

        # 报告 token 超时配置
        try:
            timeout = config.get("health_server", {}).get(
                "report_token_timeout_minutes", 30
            )
            from src.health_server.core.global_instances import set_report_token_timeout
            set_report_token_timeout(timeout)
        except Exception as e:
            logger.debug(f"设置 token 超时失败: {e}")

    def send_from_session(self, session):
        """
        从Session读取数据并发送邮件（新数据流）

        Args:
            session: SessionContext对象
        """
        try:
            # 从Session读取所有数据
            alert_stocks = session.get_alerts_as_dicts()
            stock_data = session.get_all_dataframe()
            announcements = session.announcements
            # 历史数据（由 data_fetcher 暂存，供图表使用）
            historical_data = getattr(session, "_historical", {})

            # 生成走势图表（PNG bytes + CID 内嵌，兼容 Gmail）
            _, chart_png_bytes = generate_combined_chart(
                historical_data=historical_data,
                alerts=alert_stocks,
                stock_data=stock_data,
                trading_days=60,
            )

            # 构建邮件主题
            subject = f"股票提醒 - {datetime.now().strftime('%Y-%m-%d')}"

            # 获取投资组合策略结果
            portfolio_results = getattr(session, "portfolio_results", None)

            # 优化器 YAML（main.py 已存入 session，A/非A 各一份）
            opt_data = getattr(session, "opt_data_a", None)
            opt_data_map = {
                "a_share": getattr(session, "opt_data_a", None),
                "hk": getattr(session, "opt_data_hk", None),
                "us": getattr(session, "opt_data_us", None),
                "non_a_share": getattr(session, "opt_data_non_a", None),
            }

            # 生成投资组合走势图（两张: A股 / 非A股）
            portfolio_chart_dict = None
            if portfolio_results:
                try:
                    from src.analysis.portfolio_strategy import generate_portfolio_chart
                    portfolio_chart_dict = generate_portfolio_chart(
                        portfolio_results,
                        benchmark_data=historical_data,
                    )
                    n_charts = len(portfolio_chart_dict) if portfolio_chart_dict else 0
                    logger.info(f"投资组合图表生成: {n_charts}张" if n_charts else "投资组合图表跳过")
                except Exception as e:
                    logger.error(f"投资组合图表生成失败: {e}")

            # 获取策略信号扫描结果
            signal_scan = getattr(session, "signal_scan", None)
            backtest = getattr(session, "backtest", None)

            # 生成日报 PDF 附件
            pdf_bytes = None
            try:
                pdf_bytes = self._generate_daily_pdf(
                    session, alert_stocks, signal_scan, backtest, stock_data,
                )
                if pdf_bytes:
                    logger.info("日报 PDF 生成成功 (%d bytes)", len(pdf_bytes))
            except Exception as e:
                logger.warning("日报 PDF 生成失败: %s", e)

            # 构建邮件内容（精简正文）
            body = self._build_email_body(
                alert_stocks,
                stock_data,
                announcements,
                historical_data=historical_data,
                chart_png_bytes=chart_png_bytes,
                portfolio_results=portfolio_results,
                portfolio_chart_dict=portfolio_chart_dict,
                signal_scan=signal_scan,
                backtest=backtest,
                opt_data=opt_data,
                daily_mode=True,
                opt_data_map=opt_data_map,
                placements=getattr(session, "placements", None),
            )

            # 发送邮件（PDF 作为附件）
            self._send_email(subject, body, chart_png_bytes=chart_png_bytes, portfolio_chart_dict=portfolio_chart_dict, pdf_bytes=pdf_bytes)

            logger.info(
                f"邮件任务完成 ({self.receiver_email}) "
                f"(来自Session: {len(alert_stocks)}个警报)"
            )

        except Exception as e:
            logger.error(f"从Session发送邮件失败: {e}")

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
            # 历史数据（由 data_fetcher 暂存，供图表使用）
            historical_data = getattr(session, "_historical", {})

            # 构建邮件主题
            subject = f"股票日报 - {datetime.now().strftime('%Y-%m-%d')}"

            # 获取投资组合策略结果
            portfolio_results = getattr(session, "portfolio_results", None)

            # 优化器 YAML（main.py 已存入 session，A/非A 各一份）
            opt_data = getattr(session, "opt_data_a", None)
            opt_data_map = {
                "a_share": getattr(session, "opt_data_a", None),
                "hk": getattr(session, "opt_data_hk", None),
                "us": getattr(session, "opt_data_us", None),
                "non_a_share": getattr(session, "opt_data_non_a", None),
            }

            # 生成投资组合走势图（两张）
            portfolio_chart_dict = None
            if portfolio_results:
                try:
                    from src.analysis.portfolio_strategy import generate_portfolio_chart
                    portfolio_chart_dict = generate_portfolio_chart(
                        portfolio_results,
                        benchmark_data=historical_data,
                    )
                except Exception as e:
                    logger.error(f"投资组合图表生成失败: {e}")

            # 获取策略信号扫描结果
            signal_scan = getattr(session, "signal_scan", None)
            backtest = getattr(session, "backtest", None)

            # 生成日报 PDF 附件
            pdf_bytes = None
            try:
                pdf_bytes = self._generate_daily_pdf(
                    session, [], signal_scan, backtest, stock_data,
                )
            except Exception as e:
                logger.warning("日报 PDF 生成失败: %s", e)

            # 构建邮件内容（使用空警报列表）
            body = self._build_email_body(
                [],
                stock_data,
                announcements,
                historical_data=historical_data,
                portfolio_results=portfolio_results,
                portfolio_chart_dict=portfolio_chart_dict,
                signal_scan=signal_scan,
                backtest=backtest,
                opt_data=opt_data,
                daily_mode=True,
                opt_data_map=opt_data_map,
                placements=getattr(session, "placements", None),
            )

            # 发送邮件
            self._send_email(subject, body, portfolio_chart_dict=portfolio_chart_dict, pdf_bytes=pdf_bytes)

            logger.info(f"每日报告邮件任务完成 ({self.receiver_email}) (来自Session)")

        except Exception as e:
            logger.error(f"从Session发送每日报告邮件失败: {e}")

    def _build_strategy_alert_section(
        self, signal_scan, alert_stocks, stock_data
    ) -> str:
        """构建策略信号报警 + 共识指标快照"""
        if not signal_scan:
            return ""

        consensus = getattr(signal_scan, "consensus", None)
        alerts = getattr(signal_scan, "alerts", None) or []
        snapshot = getattr(signal_scan, "indicator_snapshot", None) or {}
        warnings = getattr(signal_scan, "divergence_warnings", None) or []

        # 区分边界报警和策略报警
        boundary_codes = set()
        if alert_stocks:
            for a in alert_stocks:
                if isinstance(a, dict) and a.get("type") != "strategy":
                    boundary_codes.add(a.get("stock_code", ""))
                elif not isinstance(a, dict):
                    boundary_codes.add(getattr(a, "stock_code", ""))

        strategy_codes = set()
        for a in alerts:
            code = a.stock_code if hasattr(a, "stock_code") else a.get("stock_code", "")
            strategy_codes.add(code)

        html = "<h3>策略信号扫描</h3>\n"

        # 报警部分
        if alerts:
            html += f"<p><strong>策略报警 ({len(alerts)} 条 / {len(strategy_codes)} 只标的)</strong></p>\n"
            html += '<table style="border-collapse:collapse;width:100%;margin:10px 0;font-size:12px" cellpadding="6" cellspacing="0" border="0">\n'
            html += '<tr style="background:#34495e;color:#fff"><th>标的</th><th>规则</th><th>条件</th><th>当前值</th><th>来源</th></tr>\n'
            for a in alerts[:12]:
                code = a.stock_code if hasattr(a, "stock_code") else a.get("stock_code", "?")
                label = a.rule_label if hasattr(a, "rule_label") else a.get("rule_label", "?")
                cond = a.condition_str if hasattr(a, "condition_str") else a.get("condition", "?")
                cv = a.current_value if hasattr(a, "current_value") else a.get("current_value", "-")
                rank = a.strategy_rank if hasattr(a, "strategy_rank") else a.get("strategy_rank", "?")
                html += (
                    f'<tr style="background:#e8f6f3">'
                    f'<td>[策略] {code}</td>'
                    f'<td>{label}</td>'
                    f'<td style="font-size:11px">{cond[:60]}</td>'
                    f'<td>{cv}</td>'
                    f'<td>Rank {rank}</td>'
                    f'</tr>\n'
                )
            html += "</table>\n"
        else:
            html += "<p>策略信号: 无触发</p>\n"

        # 共识指标快照
        if consensus and consensus.consensus_indicators and snapshot:
            ind_cols = consensus.consensus_indicators
            html += f"<p><strong>共识指标快照 ({len(snapshot)} 只)</strong></p>\n"
            html += '<table style="border-collapse:collapse;width:auto;margin:10px 0;font-size:12px" cellpadding="6" cellspacing="0" border="0">\n'
            header = '<tr style="background:#34495e;color:#fff"><th>标的</th>'
            for ind in ind_cols:
                label = {"rsi": "RSI", "vol_ratio": "量比", "boll_pct_b": "布林%B",
                         "adx": "ADX", "macd_hist": "MACD柱", "deviation": "偏差%",
                         "atr": "ATR"}.get(ind, ind)
                header += f"<th>{label}</th>"
            header += "</tr>\n"
            html += header

            # 按标的在报警池内优先排序
            consensus_stocks = set(consensus.consensus_stocks or [])
            sorted_codes = sorted(snapshot.keys(),
                                  key=lambda c: (0 if c in strategy_codes else
                                                 1 if c in consensus_stocks else 2))

            for code in sorted_codes[:20]:
                vals = snapshot.get(code, {})
                html += f"<tr><td>{code}</td>"
                for ind in ind_cols:
                    v = vals.get(ind)
                    if v is not None:
                        if ind == "deviation":
                            html += f"<td>{v*100:.1f}%</td>"
                        else:
                            html += f"<td>{v:.2f}</td>"
                    else:
                        html += "<td>-</td>"
                html += "</tr>\n"
            html += "</table>\n"

        # 背离警告
        if warnings:
            html += "<p style='color:#c44e52;font-size:12px'><strong>⚠ 背离警告:</strong><br>"
            html += "<br>".join(warnings)
            html += "<br><em>建议以共识信号为准，不盲从单一名次</em></p>\n"

        return html

    def _build_backtest_section(self, backtest) -> str:
        """构建回测结果 HTML"""
        if not backtest:
            return (
                "<h3>历史回测</h3>\n"
                '<p style="color:#888;font-size:13px">优化策略未生成（每日 02:00 自动运行 <code>main.py --optimize</code>）。'
                "首次部署后可手动触发: <code>python main.py --optimize</code></p>\n"
            )

        html = "<h3>历史回测</h3>\n"
        group_labels = {"a_share": "A股", "non_a_share": "境外"}

        for group, bt in backtest.items():
            if not bt:
                continue
            label = group_labels.get(group, group)
            html += f"<p><strong>{label} — 基于最新优化策略 (Rank {bt.get('strategy_rank','?')})</strong></p>\n"
            html += '<table style="border-collapse:collapse;width:100%;margin:8px 0;font-size:12px" cellpadding="6" cellspacing="0" border="0">\n'
            html += '<tr style="background:#34495e;color:#fff"><th>指标</th><th>全期</th><th>观察0-6m</th><th>部署6-12m</th><th>验证12-24m</th></tr>\n'

            phases = bt.get("phase_metrics", {})
            # 全期总收益含资金注入，无直接可比超额 → 各阶段超额见分列
            _excess_all = "—"
            total_excess_col = _excess_all
            dd = f"{bt.get('max_drawdown', 0):.1f}%"
            sp = f"{bt.get('sharpe', 0):.3f}"
            trades = str(bt.get("trade_count", 0))

            def _pval(p_obj, key):
                if p_obj is None:
                    return "-"
                v = getattr(p_obj, key, 0)
                if key in ("total_return", "excess_return"):
                    return f"{v:+.1f}%"
                if key == "max_drawdown":
                    return f"{v:.1f}%"
                if key == "sharpe_ratio":
                    return f"{v:.3f}"
                return str(v)

            html += (f"<tr><td>超额收益</td><td>{total_excess_col}</td>"
                     f"<td>{_pval(phases.get('observe'), 'excess_return')}</td>"
                     f"<td>{_pval(phases.get('deploy'), 'excess_return')}</td>"
                     f"<td>{_pval(phases.get('test'), 'excess_return')}</td></tr>\n")
            html += (f"<tr><td>最大回撤</td><td>{dd}</td>"
                     f"<td>{_pval(phases.get('observe'), 'max_drawdown')}</td>"
                     f"<td>{_pval(phases.get('deploy'), 'max_drawdown')}</td>"
                     f"<td>{_pval(phases.get('test'), 'max_drawdown')}</td></tr>\n")
            html += (f"<tr><td>Sharpe</td><td>{sp}</td>"
                     f"<td>{_pval(phases.get('observe'), 'sharpe_ratio')}</td>"
                     f"<td>{_pval(phases.get('deploy'), 'sharpe_ratio')}</td>"
                     f"<td>{_pval(phases.get('test'), 'sharpe_ratio')}</td></tr>\n")
            html += (f"<tr><td>交易次数</td><td>{trades}</td>"
                     f"<td>{getattr(phases.get('observe'), 'trade_count', '-')}</td>"
                     f"<td>{getattr(phases.get('deploy'), 'trade_count', '-')}</td>"
                     f"<td>{getattr(phases.get('test'), 'trade_count', '-')}</td></tr>\n")

            # 基准对比
            bm = bt.get("benchmarks", {})
            if bm:
                test_excess = getattr(phases.get("test"), "excess_return", 0)
                html += "<tr><td>vs基准</td><td colspan='4'>"
                parts = []
                for name, val in bm.items():
                    beat = "✓" if test_excess > val else "✗"
                    parts.append(f"{name}: {val:+.1f}% {beat}")
                html += " | ".join(parts)
                html += "</td></tr>\n"

            html += "</table>\n"

            stocks = bt.get("stocks", [])
            if stocks:
                html += (f"<p style='font-size:12px;color:#888'>入选标的: "
                         f"{', '.join(stocks[:8])}"
                         f"{' +' + str(len(stocks)-8) if len(stocks)>8 else ''}</p>\n")

        return html

    @staticmethod
    def _calc_validation_winrate(result, bench_df, months=9):
        """验证期胜率：最近 N 月内任意一天买入持有到期，跑赢主基准的概率。

        现场用策略 nav_series vs 基准价格逐日算 forward return，不依赖 YAML。

        Returns:
            (win_rate%, win_days, total_days, v_excess%) 或全 None
        """
        nav = getattr(result, "nav_series", None) or []
        dates = getattr(result, "nav_dates", None) or []
        if len(nav) < 20 or bench_df is None or len(dates) != len(nav):
            return None, None, None, None
        try:
            import pandas as pd
            # 验证期 = 最近 months 月 ≈ months*21 交易日
            v_len = min(len(nav), months * 21)
            cut = len(nav) - v_len
            v_nav = nav[cut:]
            v_dates = dates[cut:]

            # 基准对齐到验证期日期
            bdf = bench_df.copy()
            bdf["date"] = pd.to_datetime(bdf["date"]).dt.strftime("%Y-%m-%d")
            bmap = dict(zip(bdf["date"], bdf["close"]))
            v_bench = [bmap.get(d) for d in v_dates]

            # 剔除基准缺失日，保持对齐
            pairs = [(n, b) for n, b in zip(v_nav, v_bench) if b is not None and b > 0]
            if len(pairs) < 5:
                return None, None, None, None
            s_nav = [p[0] for p in pairs]
            b_nav = [p[1] for p in pairs]

            s_final = s_nav[-1]
            b_final = b_nav[-1]
            wins = 0
            total = 0
            for i in range(len(s_nav) - 1):
                if s_nav[i] <= 0 or b_nav[i] <= 0:
                    continue
                s_fwd = s_final / s_nav[i] - 1
                b_fwd = b_final / b_nav[i] - 1
                if s_fwd > b_fwd:
                    wins += 1
                total += 1
            if total == 0:
                return None, None, None, None
            win_rate = wins / total * 100
            # 验证期整体超额
            s_ret = (s_nav[-1] / s_nav[0] - 1) * 100 if s_nav[0] > 0 else 0
            b_ret = (b_nav[-1] / b_nav[0] - 1) * 100 if b_nav[0] > 0 else 0
            v_excess = s_ret - b_ret
            return win_rate, wins, total, v_excess
        except Exception as e:
            logger.debug(f"验证期胜率计算失败: {e}")
            return None, None, None, None

    @staticmethod
    def _calc_winrate_vs_riskfree(result, annual_rate=0.02, months=9):
        """验证期胜率 vs 无风险基准（固定年化，日复利）。

        Returns: (win_rate%, win_days, total_days) 或全 None
        """
        nav = getattr(result, "nav_series", None) or []
        if len(nav) < 20:
            return None, None, None
        try:
            v_len = min(len(nav), months * 21)
            v_nav = nav[len(nav) - v_len:]
            daily_rf = (1 + annual_rate) ** (1 / 252) - 1
            s_final = v_nav[-1]
            wins = total = 0
            n = len(v_nav)
            for i in range(n - 1):
                if v_nav[i] <= 0:
                    continue
                s_fwd = s_final / v_nav[i] - 1
                # 无风险从 i 持有到期末的收益（(n-1-i) 个交易日）
                rf_fwd = (1 + daily_rf) ** (n - 1 - i) - 1
                if s_fwd > rf_fwd:
                    wins += 1
                total += 1
            if total == 0:
                return None, None, None
            return wins / total * 100, wins, total
        except Exception as e:
            logger.debug(f"无风险胜率计算失败: {e}")
            return None, None, None

    @staticmethod
    def _build_placement_section(placements, stock_data):
        """构建未解禁定增表 HTML。

        列：标的编号 | 名称 | 未解禁定增数额 | 占总股本 | 定增价格 | 解禁时间
        """
        if not placements:
            return ""

        # 从 stock_data 取名称
        name_map = {}
        try:
            if stock_data is not None and hasattr(stock_data, "iterrows"):
                for _, row in stock_data.iterrows():
                    name_map[str(row.get("stock_code", ""))] = row.get(
                        "stock_name", ""
                    )
        except Exception:
            pass

        rows = ""
        for code, p in sorted(placements.items()):
            name = name_map.get(code, "")
            issue_num = p.get("issue_num")
            issue_price = p.get("issue_price")
            pct = p.get("pct_of_total")
            unlock = p.get("unlock_date") or "—"
            # 数额格式化：亿股
            if issue_num:
                num_str = f"{issue_num / 1e8:.2f}亿股"
            else:
                num_str = "—"
            price_str = f"{issue_price:.2f}元" if issue_price else "—"
            pct_str = f"{pct:.2f}%" if pct is not None else "—"
            rows += (
                f'<tr>'
                f'<td style="padding:8px">{code}</td>'
                f'<td style="padding:8px">{name}</td>'
                f'<td style="text-align:right;padding:8px">{num_str}</td>'
                f'<td style="text-align:right;padding:8px">{pct_str}</td>'
                f'<td style="text-align:right;padding:8px">{price_str}</td>'
                f'<td style="text-align:right;padding:8px">{unlock}</td>'
                f'</tr>\n'
            )

        return (
            '<tr><td style="padding:16px 24px 4px;border-bottom:2px solid #ecf0f1">'
            '<div style="font-size:15px;font-weight:600;color:#2c3e50">'
            '未解禁定增</div></td></tr>\n'
            '<tr><td style="padding:8px 24px 16px">'
            '<table role="presentation" style="width:100%;border-collapse:collapse;'
            'font-size:12px;table-layout:fixed;word-break:break-all" cellpadding="6" cellspacing="0" border="0">\n'
            '<thead><tr style="background:#34495e;color:#fff">'
            '<th style="text-align:left;padding:8px">代码</th>'
            '<th style="text-align:left;padding:8px">名称</th>'
            '<th style="text-align:right;padding:8px">定增数额</th>'
            '<th style="text-align:right;padding:8px">占总股本</th>'
            '<th style="text-align:right;padding:8px">定增价格</th>'
            '<th style="text-align:right;padding:8px">解禁时间</th>'
            '</tr></thead>\n<tbody>\n'
            f'{rows}'
            '</tbody></table></td></tr>\n'
        )

    def _build_strategy_results_section(
        self, portfolio_results, opt_data=None, signal_scan=None,
        opt_data_map=None, benchmark_data=None,
    ) -> str:
        """构建搜参策略结果段（策略规则 + 今日信号 + 回测指标 + 季末持仓）。

        Args:
            portfolio_results: PortfolioOptimizer.run_fixed() 返回值
            opt_data: A股优化器 YAML (含 params/rules)，向后兼容
            signal_scan: SignalScanner.scan() 结果 (今日触发的策略信号)
            opt_data_map: {"a_share": yaml, "non_a_share": yaml} 各组 YAML
                          用于展示 YAML 权威预估收益 (test_return)
            benchmark_data: {code: DataFrame} 基准价格 (算验证期胜率)
        """
        if not portfolio_results:
            return ""
        if opt_data_map is None:
            opt_data_map = {"a_share": opt_data, "non_a_share": None}
        benchmark_data = benchmark_data or {}

        SIGNAL_NAMES = {
            "deviation_cross": "偏离穿越",
            "deviation_absolute": "偏离达标",
            "rsi_signal": "RSI超卖",
            "bollinger_signal": "布林低位",
            "volume_spike": "放量异动",
            "trend_follow": "趋势跟踪",
            "deep_value": "深度价值",
            "absolute_discount": "绝对折价",
            "sell_deviation_cross": "偏离穿越(卖)",
            "sell_deviation_absolute": "偏离达标(卖)",
            "sell_rsi_signal": "RSI超买",
            "sell_bollinger_signal": "布林高位",
            "sell_trend_follow": "趋势反转",
            "none": "无",
        }

        lines: list[str] = []
        lines.append('<div style="margin-top:30px;border-top:2px solid #2c3e50;padding-top:16px">')

        # 策略引擎名 + 搜参日期（新/旧引擎一目了然）
        engine_label = ""
        ts_display = ""
        if opt_data:
            top_engine = (opt_data.get("strategies") or [{}])[0]
            engine_id = (top_engine.get("params") or {}).get("_engine", "global")
            engine_names = {"percentile": "分位评分", "global": "全局阈值"}
            engine_label = engine_names.get(engine_id, engine_id)
            raw_ts = opt_data.get("timestamp", "")
            if raw_ts:
                ts_display = raw_ts[:16].replace("T", " ")

        header = "搜参策略结果"
        if engine_label:
            header += f" — {engine_label} ({engine_id})"
        if ts_display:
            header += f"  {ts_display}"
        lines.append(f'<h3 style="color:#2c3e50">{header}</h3>')

        # 策略参数摘要 (从 optimizer YAML)
        top_strategy = None
        if opt_data:
            top_strategy = (opt_data.get("strategies") or [None])[0]
        if top_strategy:
            params = top_strategy.get("params", {})
            buy_rules: list[str] = []
            sell_rules: list[str] = []
            for k, v in sorted(params.items()):
                if not k.endswith("_signal"):
                    continue
                if str(v) == "none":
                    continue
                idx = k.split("_")[1]
                name = SIGNAL_NAMES.get(str(v), str(v))
                t = params.get(k.replace("_signal", "_t"))
                frac = params.get(k.replace("_signal", "_frac"))
                extra = ""
                if t is not None:
                    extra += f" 阈值{float(t):.2f}"
                if frac is not None:
                    extra += f" 仓位{float(frac)*100:.0f}%"
                if k.startswith("buy"):
                    buy_rules.append(f"买{idx}:{name}{extra}")
                else:
                    sell_rules.append(f"卖{idx}:{name}{extra}")

            strategy_label = top_strategy.get(
                "strategy_description",
                top_strategy.get("_mode", "搜索最优"),
            )
            mode = params.get("_mode", "?")
            lines.append('<div style="background:#f0f4ff;border:1px solid #c8d6ff;'
                         'border-radius:6px;padding:12px;margin:10px 0">')
            lines.append(
                '<p style="margin:0;font-weight:600;color:#1565c0">'
                f'Top1 策略 ({mode} 模式)</p>'
            )
            if buy_rules:
                lines.append('<p style="margin:6px 0 2px"><b>买入:</b> '
                             f'{" | ".join(buy_rules)}</p>')
            if sell_rules:
                lines.append('<p style="margin:2px 0 0"><b>卖出:</b> '
                             f'{" | ".join(sell_rules)}</p>')
            lines.append("</div>")
        else:
            lines.append(
                '<p style="color:#888;margin:10px 0">'
                '（未找到优化器策略，使用 confg 默认均线规则）</p>'
            )

        # 今日信号（从 SignalScanner 结果取，和日报/简报同一套数据）
        alerts = getattr(signal_scan, "alerts", None) or [] if signal_scan else []
        if alerts:
            lines.append(
                '<div style="margin:10px 0;padding:10px;'
                'border:1px solid #d4e6f1;border-radius:5px;background:#ebf5fb">'
            )
            signal_codes = set()
            for a in alerts:
                code = getattr(a, "stock_code", None) or (
                    a.get("stock_code", "?") if isinstance(a, dict) else "?"
                )
                signal_codes.add(code)
            lines.append(
                f'<p style="margin:0 0 6px;font-weight:600;color:#1a5276">'
                f'今日信号 ({len(alerts)} 条 / {len(signal_codes)} 只标的)</p>'
            )
            lines.append(
                '<table style="font-size:12px;border-collapse:collapse;width:100%;table-layout:fixed;word-break:break-all">'
                '<tr style="background:#2c3e50;color:#fff">'
                '<th style="width:22%">标的</th><th style="width:33%">规则</th>'
                '<th style="width:45%">当前值</th></tr>'
            )
            map_a = _build_signal_label_map("a_share")
            map_hk = _build_signal_label_map("hk") or _build_signal_label_map("non_a_share")
            map_us = _build_signal_label_map("us") or _build_signal_label_map("non_a_share")
            for a in alerts[:30]:
                code = getattr(a, "stock_code", None) or (
                    a.get("stock_code", "?") if isinstance(a, dict) else "?"
                )
                raw = getattr(a, "rule_label", None) or (
                    a.get("rule_label", "?") if isinstance(a, dict) else "?"
                )
                readable = _readable_signal(code, raw, map_a, map_hk, map_us)
                cv = getattr(a, "current_value", None) or (
                    a.get("current_value", "-") if isinstance(a, dict) else "-"
                )
                lines.append(
                    f'<tr><td>{code}</td><td>{readable}</td><td>{cv}</td></tr>'
                )
            lines.append("</table></div>")
        else:
            lines.append(
                '<p style="color:#888;margin:8px 0">今日信号: 无触发</p>'
            )

        # 组合结果 (PortfolioEvaluator 实盘评估) — 只展示 Top1 (max_return)
        group_labels = {
            "a_share": "A股组合", "hk": "港股组合", "us": "美股组合",
            "non_a_share": "非A股组合",
        }
        for group_key, group_label in group_labels.items():
            group_data = portfolio_results.get(group_key)
            if not group_data:
                continue
            # run_fixed 返回 "top1"，向后兼容旧 "max_return"
            r = group_data.get("top1") or group_data.get("max_return")
            if not r:
                continue
            lines.append(
                f'<h4 style="color:#333;border-left:4px solid #2196f3;'
                f'padding-left:10px;margin:16px 0 8px">{group_label}</h4>'
            )

            # ── YAML 权威预估收益（Top1 测试期，近9个月样本外）──
            # 各组用自己的 YAML（hk/us 独立搜参），回退 non_a_share
            g_yaml = (opt_data_map.get(group_key)
                      or opt_data_map.get("non_a_share") or {})
            g_top = (g_yaml.get("strategies") or [{}])[0]
            test_ret = g_top.get("test_return")
            test_dd = g_top.get("test_drawdown")
            g_sharpe = g_top.get("sharpe")
            ts = g_yaml.get("timestamp", "")[:16].replace("T", " ")
            comp = getattr(r, "composition", [])
            qh = getattr(r, "quarterly_holdings", None) or []
            # 平均现金仓位（从季末持仓算 avg(cash/nav)）
            cash_pcts = [
                (100 - q["pos_pct"]) for q in qh if q.get("nav", 0) > 0
            ]
            avg_cash = sum(cash_pcts) / len(cash_pcts) if cash_pcts else None

            # ── 验证期胜率（现场用 nav_series vs 三基线算，不依赖 YAML）──
            # 每组三基线：(展示名, 价格基准code 或 None=无风险, 无风险年化)
            bench_sets = {
                "a_share": [("510880", "510880", None), ("沪深300", "510300", None),
                            ("无风险", None, 0.02)],
                "hk": [("VOO", "VOO", None), ("BRK.B", "BRK.B", None),
                       ("无风险", None, 0.038)],
                "us": [("VOO", "VOO", None), ("BRK.B", "BRK.B", None),
                       ("无风险", None, 0.038)],
            }
            wr_parts = []
            for disp, bcode, rf in bench_sets.get(group_key, []):
                if bcode is not None:
                    wr, _, _, _ = self._calc_validation_winrate(
                        r, benchmark_data.get(bcode), months=9,
                    )
                else:
                    wr, _, _ = self._calc_winrate_vs_riskfree(
                        r, annual_rate=rf, months=9,
                    )
                if wr is not None:
                    wc = "#27ae60" if wr >= 50 else "#c0392b"
                    wr_parts.append(
                        f'{disp} <span style="color:{wc}">{wr:.0f}%</span>')

            if test_ret is not None:
                rc = "#27ae60" if test_ret >= 0 else "#c0392b"
                summary = (
                    '<div style="border:1px solid #ddd;padding:10px;'
                    'margin:6px 0;border-radius:5px;background:#f0f7ff">'
                    f'<b>预估收益(测试期超额, 近9月, 不参与排序)</b> '
                    f'<span style="color:{rc}">{test_ret:+.1f}%</span> &nbsp; '
                    f'回撤 {test_dd:.1f}% &nbsp; '
                    f'夏普 {g_sharpe:.2f} &nbsp; '
                )
                if avg_cash is not None:
                    summary += f'平均现金仓位 {avg_cash:.0f}% &nbsp; '
                # 验证期胜率（三基线）
                if wr_parts:
                    summary += (
                        f'<br><b>验证期胜率</b>(任意一天买入持有到期跑赢): '
                        + " | ".join(wr_parts)
                    )
                summary += f'<br><span style="color:#888;font-size:11px">'
                summary += f'搜参时间 {ts} · 成分: '
                summary += f'{", ".join(comp) if comp else "—"}</span>'
                lines.append(summary)
            else:
                # YAML 无测试收益时回退展示评估值
                tr = getattr(r, "total_return", 0)
                dd = getattr(r, "max_drawdown", 0)
                rc = "#27ae60" if tr >= 0 else "#c0392b"
                lines.append(
                    '<div style="border:1px solid #ddd;padding:10px;'
                    'margin:6px 0;border-radius:5px;background:#fafafa">'
                    f'收益 <span style="color:{rc}">{tr:+.1f}%</span> &nbsp; '
                    f'回撤 {dd:.1f}% &nbsp; 成分: '
                    f'{", ".join(comp) if comp else "—"}'
                )

            # 季末持仓明细
            if qh:
                lines.append(
                    '<table style="font-size:11px;border-collapse:collapse;'
                    'width:100%;margin-top:6px;table-layout:fixed;word-break:break-all">'
                    '<tr style="background:#34495e;color:#fff">'
                    '<th>Q</th><th>代码</th><th>持股</th>'
                    '<th>成本</th><th>现价</th><th>市值</th>'
                    '<th>盈亏</th><th>盈亏%</th></tr>'
                )
                for q in qh:
                    qn = q["quarter"]
                    qcs = q["cash"]
                    qp = q["pos_pct"]
                    qnv = q["nav"]
                    qpos = q.get("positions", [])
                    if not qpos:
                        lines.append(
                            f'<tr><td>Q{qn}</td>'
                            f'<td colspan=7>空仓 (nav={qnv:.0f})</td></tr>'
                        )
                    for pos in qpos:
                        code = pos["code"]
                        sh = pos["shares"]
                        cb = pos["cost"]
                        px = pos["price"]
                        vl = pos["value"]
                        pn = pos["pnl"]
                        pp = pos["pnl_pct"]
                        color = "#27ae60" if pn >= 0 else "#c0392b"
                        lines.append(
                            f'<tr><td>Q{qn}</td><td>{code}</td>'
                            f'<td>{sh:.0f}股</td>'
                            f'<td>{cb:.2f}</td><td>{px:.2f}</td>'
                            f'<td>{vl:.0f}</td>'
                            f'<td style="color:{color}">{pn:+.0f}</td>'
                            f'<td style="color:{color}">{pp:+.1f}%</td></tr>'
                        )
                    if qpos:
                        lines.append(
                            f'<tr><td>Q{qn}</td>'
                            f'<td colspan=4>现金: {qcs:.0f}</td>'
                            f'<td colspan=3>仓位: {qp:.0f}%</td></tr>'
                        )
                lines.append("</table>")
            lines.append("</div>")  # close card

        lines.append("</div>")  # close section
        return "\n".join(lines)

    def _build_portfolio_section(self, portfolio_results, portfolio_chart_dict=None):
        """
        构建投资组合策略分析部分HTML

        Args:
            portfolio_results: PortfolioOptimizer.run()返回的结果字典
                {
                    "a_share": {
                        "max_return": PortfolioResult,
                        "min_drawdown": PortfolioResult,
                        "max_sharpe": PortfolioResult
                    },
                    "non_a_share": { ... }
                }

        Returns:
            str: HTML格式的投资组合策略分析部分
        """
        if not portfolio_results:
            return ""

        logger.info("构建投资组合策略分析部分")

        group_labels = {
            "a_share": "A股组合",
            "non_a_share": "非A股组合（港股/美股/新加坡）",
        }
        metric_labels = {
            "max_return": "最高收益",
            "min_drawdown": "最小回撤",
            "max_sharpe": "最优夏普",
        }

        html = """
        <div style="margin-top: 40px; border-top: 1px solid #ddd; padding-top: 20px;">
            <h3>投资组合预期回报</h3>
            <p style="color: #666; font-size: 14px;">基于MA60锚点择时策略，搜索最优投资组合（月度买入/卖出各限15000元）</p>
        """

        for group_key, group_label in group_labels.items():
            group_data = portfolio_results.get(group_key)
            if not group_data:
                continue

            html += f"""
            <div style="margin-top: 25px;">
                <h4 style="color: #333; border-left: 4px solid #2196f3; padding-left: 10px;">{group_label}</h4>
            """

            for metric_key, metric_label in metric_labels.items():
                result = group_data.get(metric_key)
                if not result:
                    continue

                # PortfolioResult is a dataclass, use attribute access
                total_return = (
                    result.total_return if hasattr(result, "total_return") else 0
                )
                max_drawdown = (
                    result.max_drawdown if hasattr(result, "max_drawdown") else 0
                )
                sharpe_ratio = (
                    result.sharpe_ratio if hasattr(result, "sharpe_ratio") else 0
                )
                expected_position = (
                    result.expected_position
                    if hasattr(result, "expected_position")
                    else 0
                )
                trade_count = (
                    result.trade_count if hasattr(result, "trade_count") else 0
                )
                composition = (
                    result.composition if hasattr(result, "composition") else []
                )
                details = (
                    result.stock_details if hasattr(result, "stock_details") else []
                )

                # 构造组合成分字符串
                composition_str = ", ".join(composition) if composition else "无"
                # 各股详情（简要展示前5只）
                details_html = ""
                if details:
                    details_html = "<ul style='margin: 5px 0; padding-left: 20px; font-size: 12px;'>"
                    for d in details[:5]:
                        d_return = (
                            d.get("total_return", 0)
                            if isinstance(d, dict)
                            else getattr(d, "total_return", 0)
                        )
                        d_sharpe = (
                            d.get("sharpe_ratio", 0)
                            if isinstance(d, dict)
                            else getattr(d, "sharpe_ratio", 0)
                        )
                        d_trades = (
                            d.get("trades", 0)
                            if isinstance(d, dict)
                            else getattr(d, "total_trades", 0)
                        )
                        d_code = (
                            d.get("stock_code", "")
                            if isinstance(d, dict)
                            else getattr(d, "stock_code", "")
                        )
                        ret_color = "green" if d_return >= 0 else "red"
                        details_html += (
                            f"<li>{d_code}: "
                            f"收益率 <span style='color:{ret_color};'>{d_return:+.2f}%</span>, "
                            f"夏普 {d_sharpe:.2f}, "
                            f"交易 {d_trades}次"
                            f"</li>"
                        )
                    details_html += "</ul>"

                return_color = "green" if total_return >= 0 else "red"
                dd_color = "red" if max_drawdown < 0 else "green"

                html += f"""
                <div style="border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 6px; background-color: #fafafa;">
                    <h5 style="margin: 0 0 10px 0; color: #1565c0;">{metric_label}</h5>
                    <table style="border-collapse: collapse; width: 100%; font-size: 13px;">
                        <tr>
                            <td style="padding: 4px 8px; width: 25%;"><strong>组合收益率</strong></td>
                            <td style="padding: 4px 8px; color: {return_color};">{total_return:+.2f}%</td>
                            <td style="padding: 4px 8px; width: 25%;"><strong>最大回撤</strong></td>
                            <td style="padding: 4px 8px; color: {dd_color};">{max_drawdown:+.2f}%</td>
                        </tr>
                        <tr>
                            <td style="padding: 4px 8px;"><strong>夏普比率</strong></td>
                            <td style="padding: 4px 8px;">{sharpe_ratio:.2f}</td>
                            <td style="padding: 4px 8px;"><strong>期末持仓市值</strong></td>
                            <td style="padding: 4px 8px;">{expected_position:,.2f}元</td>
                        </tr>
                        <tr>
                            <td style="padding: 4px 8px;"><strong>交易次数</strong></td>
                            <td style="padding: 4px 8px;">{trade_count}</td>
                            <td style="padding: 4px 8px;"><strong>成分股数</strong></td>
                            <td style="padding: 4px 8px;">{len(composition)}</td>
                        </tr>
                        <tr>
                            <td style="padding: 4px 8px; vertical-align: top;"><strong>成分股</strong></td>
                            <td style="padding: 4px 8px;" colspan="3">{composition_str}</td>
                        </tr>
                    </table>
                    {details_html}
                </div>
                """

            html += "</div>"

        html += """
            <p style="color: #888; font-size: 12px; margin-top: 15px;">
                <strong>策略说明：</strong>以MA60为锚点，价格跌破-5%/-10%时分批买入（每笔≤5000元），
                突破+5%/+10%/+15%时分批卖出1/4持仓（每笔≤10000元，低于2500元清仓）。
                 月度买入/卖出各限15000元（组合级）。A股无风险利率2%，非A股4.5%。
                 初始资金每组10万元（组合内标的共享）。
            </p>
        </div>
        """

        return html

    def _build_email_body(
        self,
        alert_stocks,
        stock_data,
        announcements=None,
        historical_data=None,
        chart_png_bytes=None,
        portfolio_results=None,
        portfolio_chart_dict=None,
        signal_scan=None,
        backtest=None,
        opt_data=None,
        daily_mode=False,
        opt_data_map=None,
        placements=None,
    ):
        """
        构建邮件正文（完整版：表格 + 公告 + 图表）

        Args:
            alert_stocks: 满足条件的股票列表
            stock_data: 完整的股票数据DataFrame
            announcements: 公告数据字典（可选）
            historical_data: 完整历史DataFrame字典 stock_code → DataFrame（可选，供图表使用）
            chart_png_bytes: 图表 PNG 原始字节（可选），有值时用 cid:chart001 嵌入

        Returns:
            str: 邮件正文（HTML格式）
        """
        from datetime import datetime
        from pathlib import Path

        # 1. 加载模板
        template_dir = Path(__file__).parent.parent / "templates"
        email_template = (template_dir / "email_template.html").read_text(
            encoding="utf-8"
        )
        alert_section_template = (template_dir / "alert_section.html").read_text(
            encoding="utf-8"
        )

        # 2. 构建满足条件的股票行（拆分为技术指标和基本面指标）
        alert_rows_technical = ""
        alert_rows_fundamental = ""
        seen_fundamental = set()  # 基本面去重：每个股票只加一次
        for alert in alert_stocks:
            if alert.get("type") == "strategy":
                continue  # 策略告警在独立 section 渲染
            if self._is_multi_alert_format(alert):
                # 多层级警报格式
                technical_row, fundamental_row = self._build_alert_rows_multi(
                    alert, stock_data
                )
                alert_rows_technical += technical_row
                # 基本面去重：同一股票只加一次
                multi_code = alert.get("stock_code", "")
                if multi_code and multi_code not in seen_fundamental:
                    alert_rows_fundamental += fundamental_row
                    seen_fundamental.add(multi_code)
            else:
                # 单锚点警报格式（向后兼容）
                stock_code = alert.get("stock_code", "")
                low_price = alert.get("low_price")
                ma60 = alert.get("ma60")
                low_ma60_diff = alert.get("price_difference", 0)  # 最低价与MA60差值
                low_ma60_pct = alert.get(
                    "percentage_difference", 0
                )  # 最低价与MA60百分比差值

                # 从stock_data中查找股票名称、收盘价和其他数据
                stock_row = stock_data[stock_data["stock_code"] == stock_code]
                stock_name = stock_code
                close_price = 0
                if not stock_row.empty:
                    stock_name = stock_row.iloc[0].get("stock_name", stock_code)
                    close_price = stock_row.iloc[0].get("close", 0)

                # 计算收盘价与MA60差值（安全处理None值）
                close_ma60_diff = None
                close_ma60_pct = None
                if (
                    close_price is not None
                    and ma60 is not None
                    and not pd.isna(close_price)
                    and not pd.isna(ma60)
                ):
                    close_ma60_diff = close_price - ma60
                    close_ma60_pct = (close_ma60_diff / ma60 * 100) if ma60 != 0 else 0

                # 获取基本面数据
                dividend_per_share = None
                dividend_yield = None
                pe_ratio = None
                pb_ratio = None
                roe = None

                if not stock_row.empty:
                    dividend_per_share = stock_row.iloc[0].get("dividend_per_share")
                    dividend_yield = stock_row.iloc[0].get("dividend_yield")
                    pe_ratio = stock_row.iloc[0].get("pe_ratio")
                    pb_ratio = stock_row.iloc[0].get("pb_ratio")
                    roe = stock_row.iloc[0].get("roe")

                # 格式化基本面数据
                dividend_per_share_str = (
                    f"{dividend_per_share:.3f}"
                    if dividend_per_share is not None
                    and not pd.isna(dividend_per_share)
                    else "—"
                )
                dividend_yield_str = (
                    f"{dividend_yield:.2f}%"
                    if dividend_yield is not None and not pd.isna(dividend_yield)
                    else "—"
                )
                pe_ratio_str = (
                    f"{pe_ratio:.2f}"
                    if pe_ratio is not None and not pd.isna(pe_ratio)
                    else "—"
                )
                pb_ratio_str = (
                    f"{pb_ratio:.2f}"
                    if pb_ratio is not None and not pd.isna(pb_ratio)
                    else "—"
                )
                roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "—"

                # 确定颜色样式（inline for email clients）
                pos_color = "color:#27ae60"
                neg_color = "color:#c0392b"
                close_diff_style = (
                    pos_color
                    if close_ma60_diff is not None and close_ma60_diff >= 0
                    else neg_color
                    if close_ma60_diff is not None
                    else ""
                )
                close_pct_style = (
                    pos_color
                    if close_ma60_pct is not None and close_ma60_pct >= 0
                    else neg_color
                    if close_ma60_pct is not None
                    else ""
                )
                low_price_str = (
                    f"{low_price:.2f}"
                    if low_price is not None and not pd.isna(low_price)
                    else "—"
                )
                ma60_str = (
                    f"{ma60:.2f}" if ma60 is not None and not pd.isna(ma60) else "—"
                )
                close_price_str = (
                    f"{close_price:.2f}"
                    if close_price is not None and not pd.isna(close_price)
                    else "—"
                )
                close_ma60_diff_str = (
                    f"{close_ma60_diff:+.2f}" if close_ma60_diff is not None else "—"
                )
                close_ma60_pct_str = (
                    f"{close_ma60_pct:+.2f}%" if close_ma60_pct is not None else "—"
                )
                low_ma60_diff_str = (
                    f"{low_ma60_diff:.2f}" if low_ma60_diff is not None else "—"
                )
                low_ma60_pct_str = (
                    f"{low_ma60_pct:.2f}%" if low_ma60_pct is not None else "—"
                )

                # 技术指标行
                alert_rows_technical += f"""
                    <tr style="background:#fef9e7">
                        <td>{stock_code}</td>
                        <td>{stock_name}</td>
                        <td>{low_price_str}</td>
                        <td>{ma60_str}</td>
                        <td>{close_price_str}</td>
                        <td style="{close_diff_style}">{close_ma60_diff_str}</td>
                        <td style="{close_pct_style}">{close_ma60_pct_str}</td>
                        <td style="{neg_color}">{low_ma60_diff_str}</td>
                        <td style="{neg_color}">{low_ma60_pct_str}</td>
                        <td>[MA60] 最低价 &lt; MA60</td>
                    </tr>
                """

                # 基本面指标行（去重：同一股票只加一次）
                if stock_code and stock_code not in seen_fundamental:
                    alert_rows_fundamental += f"""
                    <tr style="background:#fef9e7">>
                        <td>{stock_code}</td>
                        <td>{stock_name}</td>
                        <td>{dividend_per_share_str}</td>
                        <td>{dividend_yield_str}</td>
                        <td>{pe_ratio_str}</td>
                        <td>{pb_ratio_str}</td>
                        <td>{roe_str}</td>
                    </tr>
                """
                    seen_fundamental.add(stock_code)

        # 3. 构建所有监控股票行（拆分为价格技术指标和基本面指标）
        all_rows_price = ""
        all_rows_fundamental = ""

        # 价格表表头（日报模式精简：去掉 MA60/偏离/状态 列）
        if daily_mode:
            price_table_header = (
                '<th style="text-align:left;padding:8px">代码</th>\n'
                '        <th style="text-align:right;padding:8px">开盘</th>\n'
                '        <th style="text-align:right;padding:8px">收盘</th>\n'
                '        <th style="text-align:right;padding:8px">最高</th>\n'
                '        <th style="text-align:right;padding:8px">最低</th>'
            )
        else:
            price_table_header = (
                '<th style="text-align:left;padding:8px">代码</th>\n'
                '        <th style="text-align:right;padding:8px">开盘</th>\n'
                '        <th style="text-align:right;padding:8px">收盘</th>\n'
                '        <th style="text-align:right;padding:8px">最高</th>\n'
                '        <th style="text-align:right;padding:8px">最低</th>\n'
                '        <th style="text-align:right;padding:8px">MA60</th>\n'
                '        <th style="text-align:right;padding:8px">偏离</th>\n'
                '        <th style="text-align:right;padding:8px">偏离%</th>\n'
                '        <th style="text-align:left;padding:8px">状态</th>'
            )
        for _, row in stock_data.iterrows():
            stock_code = row.get("stock_code", "")
            stock_name = row.get("stock_name", stock_code)
            open_price = row.get("open", 0)
            close_price = row.get("close", 0)
            high_price = row.get("high")
            low_price = row.get("low")
            ma60 = row.get("ma60")

            # 计算收盘价与MA60差值（仅在数据有效时计算）
            if (
                close_price is not None
                and ma60 is not None
                and not pd.isna(close_price)
                and not pd.isna(ma60)
            ):
                close_ma60_diff = close_price - ma60
                close_ma60_pct = (close_ma60_diff / ma60 * 100) if ma60 != 0 else 0
                diff_style = ""
                pct_style = ""
            else:
                close_ma60_diff = None
                close_ma60_pct = None
                diff_style = ""
                pct_style = ""

            # 获取基本面数据
            dividend_per_share = row.get("dividend_per_share")
            dividend_yield = row.get("dividend_yield")
            pe_ratio = row.get("pe_ratio")
            pb_ratio = row.get("pb_ratio")
            roe = row.get("roe")

            # 格式化基本面数据
            dividend_per_share_str = (
                f"{dividend_per_share:.3f}"
                if dividend_per_share is not None and not pd.isna(dividend_per_share)
                else "—"
            )
            dividend_yield_str = (
                f"{dividend_yield:.2f}%"
                if dividend_yield is not None and not pd.isna(dividend_yield)
                else "—"
            )
            pe_ratio_str = (
                f"{pe_ratio:.2f}"
                if pe_ratio is not None and not pd.isna(pe_ratio)
                else "—"
            )
            pb_ratio_str = (
                f"{pb_ratio:.2f}"
                if pb_ratio is not None and not pd.isna(pb_ratio)
                else "—"
            )
            roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "—"

            # 检查是否满足条件（最低价 < MA60）- 安全处理None值
            status = "正常"
            if (
                low_price is not None
                and ma60 is not None
                and not pd.isna(low_price)
                and not pd.isna(ma60)
                and low_price < ma60
            ):
                status = "<span style='color: #f44336;'>提醒</span>"

            # 格式化价格数据（安全处理None值）
            open_price_str = (
                f"{open_price:.2f}"
                if open_price is not None and not pd.isna(open_price)
                else "—"
            )
            close_price_str = (
                f"{close_price:.2f}"
                if close_price is not None and not pd.isna(close_price)
                else "—"
            )
            high_price_str = (
                f"{high_price:.2f}"
                if high_price is not None and not pd.isna(high_price)
                else "—"
            )
            low_price_str = (
                f"{low_price:.2f}"
                if low_price is not None and not pd.isna(low_price)
                else "—"
            )
            ma60_str = f"{ma60:.2f}" if ma60 is not None and not pd.isna(ma60) else "—"
            close_ma60_diff_str = (
                f"{close_ma60_diff:+.2f}" if close_ma60_diff is not None else "—"
            )
            close_ma60_pct_str = (
                f"{close_ma60_pct:+.2f}%" if close_ma60_pct is not None else "—"
            )

            # 价格技术指标行 (inline styles for email clients)
            pos = "color:#27ae60;text-align:right"
            neg = "color:#c0392b;text-align:right"
            neut = "text-align:right"
            diff_style = pos if close_ma60_diff is not None and close_ma60_diff >= 0 else neg if close_ma60_diff is not None else neut
            pct_style = pos if close_ma60_pct is not None and close_ma60_pct >= 0 else neg if close_ma60_pct is not None else neut
            if daily_mode:
                # 日报模式：精简价格行（去掉 MA60/偏离/状态）
                all_rows_price += (
                    f'<tr>'
                    f'<td>{stock_code}</td>'
                    f'<td style="{neut}">{open_price_str}</td>'
                    f'<td style="{neut}">{close_price_str}</td>'
                    f'<td style="{neut}">{high_price_str}</td>'
                    f'<td style="{neut}">{low_price_str}</td>'
                    f'</tr>'
                )
            else:
                all_rows_price += (
                    f'<tr>'
                    f'<td>{stock_code}</td>'
                    f'<td style="{neut}">{open_price_str}</td>'
                    f'<td style="{neut}">{close_price_str}</td>'
                    f'<td style="{neut}">{high_price_str}</td>'
                    f'<td style="{neut}">{low_price_str}</td>'
                    f'<td style="{neut}">{ma60_str}</td>'
                    f'<td style="{diff_style}">{close_ma60_diff_str}</td>'
                    f'<td style="{pct_style}">{close_ma60_pct_str}</td>'
                    f'<td>{status}</td>'
                    f'</tr>'
                )

            # 基本面指标行
            all_rows_fundamental += (
                f'<tr>'
                f'<td>{stock_code}</td>'
                f'<td style="{neut}">{dividend_per_share_str}</td>'
                f'<td style="{neut}">{dividend_yield_str}</td>'
                f'<td style="{neut}">{pe_ratio_str}</td>'
                f'<td style="{neut}">{pb_ratio_str}</td>'
                f'<td style="{neut}">{roe_str}</td>'
                f'</tr>'
            )

        # 4. 构建公告部分
        announcements_section = ""
        if announcements and len(announcements) > 0:
            announcements_section = """
            <h3>近期重要公告</h3>
            <p>以下为监控股票近期发布的重要公告：</p>
            """
            for stock_code, announcement_list in announcements.items():
                if not announcement_list:
                    continue
                announcements_section += f"""
                <div style="border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px;">
                    <h4>股票 {stock_code}</h4>
                """
                for i, announcement in enumerate(announcement_list[:5]):
                    title = announcement.get("title", "")
                    date = announcement.get("date", "")
                    url = announcement.get("url", "")
                    exchange = announcement.get("exchange", "").upper()
                    link = (
                        f'<a href="{url}" target="_blank">{title}</a>' if url else title
                    )
                    announcements_section += f"""
                    <div style="margin-bottom: 8px;">
                        <strong>{i + 1}. [{exchange}] {date}</strong><br/>
                        {link}
                    """

                    # 检查是否有官方分红记录
                    dividend_details = announcement.get("dividend_details")
                    if dividend_details and len(dividend_details) > 0:
                        official_info = []
                        for detail in dividend_details:
                            announcement_date = detail.get("announcement_date", "未知")
                            cash_dividend = detail.get("cash_dividend")
                            dividend_per_share = detail.get("dividend_per_share")
                            # 格式化分红值，处理None情况
                            if cash_dividend is not None and not pd.isna(cash_dividend):
                                info = (
                                    f"{announcement_date}: 分红{cash_dividend:.2f}元/股"
                                )
                                official_info.append(info)
                            elif dividend_per_share is not None and not pd.isna(
                                dividend_per_share
                            ):
                                info = f"{announcement_date}: 分红{dividend_per_share:.2f}元/股"
                                official_info.append(info)
                        if official_info:
                            announcements_section += f"""
                            <div style="margin-left: 20px; margin-top: 5px; padding: 5px; background-color: #e8f4fd; border-left: 3px solid #2196f3; font-size: 0.9em;">
                                <strong>官方分红记录:</strong><br/>
                                {", ".join(official_info)}
                            </div>
                            """

                    # 检查是否有LLM提取的分红详情
                    llm_dividend = announcement.get("llm_extracted_dividend")
                    if llm_dividend and llm_dividend.get("success", False):
                        dividend_info = []
                        cash_dividend = llm_dividend.get("cash_dividend_per_share")
                        if cash_dividend is not None and not pd.isna(cash_dividend):
                            dividend_info.append(f"现金分红: {cash_dividend:.3f}元/股")
                        dividend_per_share = llm_dividend.get("dividend_per_share")
                        if dividend_per_share is not None and not pd.isna(
                            dividend_per_share
                        ):
                            dividend_info.append(
                                f"总分红: {dividend_per_share:.3f}元/股"
                            )
                        dividend_date = llm_dividend.get("dividend_date")
                        if dividend_date:
                            dividend_info.append(f"分红日期: {dividend_date}")
                        confidence = llm_dividend.get("confidence")
                        confidence_pct = (
                            f"{confidence * 100:.0f}%"
                            if confidence is not None and not pd.isna(confidence)
                            else "N/A"
                        )

                        if dividend_info:
                            announcements_section += f"""
                            <div style="margin-left: 20px; margin-top: 5px; padding: 5px; background-color: #f8f9fa; border-left: 3px solid #4caf50; font-size: 0.9em;">
                                <strong>LLM提取分红详情（置信度: {confidence_pct}）:</strong><br/>
                                {", ".join(dividend_info)}
                            </div>
                            """
                    announcements_section += """
                    </div>
                    """
                announcements_section += """
                </div>
                """
            announcements_section += """
             <p><em>注：公告信息仅供参考，请以交易所官方公告为准。</em></p>
            """

        # 6b. 构建搜参策略结果段（含今日信号）
        strategy_results_section = ""
        if portfolio_results:
            strategy_results_section = self._build_strategy_results_section(
                portfolio_results, opt_data, signal_scan=signal_scan,
                opt_data_map=opt_data_map, benchmark_data=historical_data,
            )

        # 6c. 构建投资组合策略分析部分（旧版，日报模式跳过避免与搜参段重复）
        portfolio_section = ""
        if portfolio_results and not daily_mode:
            portfolio_section = self._build_portfolio_section(portfolio_results, portfolio_chart_dict)

        # 7. 走势图表（由调用方生成，通过 chart_png_bytes 传入，使用 CID 内嵌）
        chart_section = ""
        if chart_png_bytes:
            chart_section = """
            <h3>价格走势图</h3>
            <p>近2个月收盘价走势：</p>
            <div style="text-align: center; margin: 20px 0;">
                <img src="cid:chart001"
                     alt="价格走势图"
                     style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 4px;" />
            </div>
            """

        # 7b. 投资组合走势图（chart002=A股, chart003=港股, chart004=美股, chart005=非A兼容）
        portfolio_chart_section = ""
        if portfolio_chart_dict:
            cid_map = {"a_share": "chart002", "hk": "chart003",
                       "us": "chart004", "non_a_share": "chart005"}
            group_titles = {
                "a_share": "A股投资组合净值走势", "hk": "港股投资组合净值走势",
                "us": "美股投资组合净值走势", "non_a_share": "非A股投资组合净值走势",
            }
            for group_key in ("a_share", "hk", "us", "non_a_share"):
                if group_key in portfolio_chart_dict:
                    cid = cid_map[group_key]
                    title = group_titles.get(group_key, group_key)
                    portfolio_chart_section += f"""
            <h3>{title}</h3>
            <div style="text-align: center; margin: 10px 0 20px 0;">
                <img src="cid:{cid}"
                     alt="{title}"
                     style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 4px;" />
            </div>
            """

        # 7b. 策略信号报警 — 日报模式已合并到搜参策略结果段，不单独渲染
        strategy_alert_section = ""
        if signal_scan and not daily_mode:
            strategy_alert_section = self._build_strategy_alert_section(
                signal_scan, alert_stocks, stock_data
            )

        # 7c. 回测分析 — 日报模式跳过
        backtest_section = ""
        if not daily_mode and backtest:
            backtest_section = self._build_backtest_section(backtest)

        # 8. 获取服务器信息
        server_info = self._get_server_info()

        # 7. 构建报警股票部分 — 日报模式跳过
        alert_section = ""
        is_multi_format = False  # 保存格式标志供后续使用
        if alert_stocks and not daily_mode:
            # 确定警报格式（检查第一个警报）
            if alert_stocks and len(alert_stocks) > 0:
                is_multi_format = self._is_multi_alert_format(alert_stocks[0])

            if is_multi_format:
                # 多层级警报格式
                alert_section = f"""
                <h3>满足条件的股票 ({len(alert_stocks)} 只)</h3>
                
                <h4>多层级警报技术指标</h4>
                <table>
                    <tr>
                        <th>股票代码</th>
                        <th>股票名称</th>
                        <th>价格</th>
                        <th>锚点值</th>
                        <th>价格差值</th>
                        <th>百分比(%)</th>
                        <th>锚点名称</th>
                        <th>区间标签</th>
                        <th>连续天数</th>
                        <th>条件</th>
                    </tr>
                    {alert_rows_technical}
                </table>
                
                <h4>基本面指标</h4>
                <table>
                    <tr>
                        <th>股票代码</th>
                        <th>股票名称</th>
                        <th>每股分红(元)</th>
                        <th>股息率(%)</th>
                        <th>PE</th>
                        <th>PB</th>
                        <th>ROE(%)</th>
                        <th>负债率(%)</th>
                    </tr>
                    {alert_rows_fundamental}
                </table>
                """
            else:
                # 单锚点警报格式（使用模板）
                alert_section = alert_section_template.format(
                    alert_count=len(alert_stocks),
                    alert_rows_technical=alert_rows_technical,
                    alert_rows_fundamental=alert_rows_fundamental,
                )

        # 8. 根据警报格式更新模板标题
        if is_multi_format:
            # 替换为多层级警报标题
            email_template = email_template.replace(
                "系统检测到以下股票满足条件：<strong>当天最低价 &lt; MA60（前复权）</strong>",
                "系统检测到以下股票满足多层级警报条件：<strong>多锚点阈值区间突破</strong>",
            )
        else:
            # 确保是单锚点标题（默认）
            email_template = email_template.replace(
                "系统检测到以下股票满足条件：<strong>多锚点阈值区间突破</strong>",
                "系统检测到以下股票满足条件：<strong>当天最低价 &lt; MA60（前复权）</strong>",
            )

        # 8.5. 报告链接（A股 + 境外各一份，30 分钟后过期）
        report_link = ""
        try:
            optimizer_dir = Path("data/optimizer")
            if optimizer_dir.exists():
                a_r = sorted(
                    optimizer_dir.glob("*_a_share_report.html"),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                nona_r = sorted(
                    optimizer_dir.glob("*_non_a_share_report.html"),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                if a_r or nona_r:
                    from src.health_server.core.global_instances import register_report_token

                    hc = self.config.get("health_server", {})
                    server_ip = hc.get("public_ip", "")
                    port = hc.get("port", 1933)
                    use_ssl = hc.get("ssl", False)

                    if not server_ip:
                        try:
                            import urllib.request
                            ip_url = hc.get(
                                "ip_detect_url", "https://ifconfig.me"
                            )
                            server_ip = (
                                urllib.request.urlopen(ip_url, timeout=5)
                                .read().decode("utf-8").strip()
                            )
                        except Exception as e:
                            logger.debug(f"IP检测服务失败: {e}")
                            fi = self._get_server_info().get("ip_address", "localhost")
                            for p in fi.replace("(优先)","").replace("(","").replace(")","").split(","):
                                s = p.strip().split()[0] if p.strip() else ""
                                if s and not s.startswith(("172.","10.","192.168.","127.")):
                                    server_ip = s; break
                            if server_ip == "localhost":
                                server_ip = fi.split(",")[0].strip().split()[0]

                    proto = "https" if use_ssl else "http"
                    links_html = ""
                    for label, report_list in [("A股", a_r), ("境外", nona_r)]:
                        if not report_list:
                            continue
                        token = register_report_token(str(report_list[0]))
                        links_html += (
                            f'<a href="{proto}://{server_ip}:{port}/report/{token}" '
                            f'style="color:#2980b9;text-decoration:none">'
                            f'{label}</a> &nbsp;'
                        )
                    if links_html:
                        report_link = (
                            f'<tr><td style="padding:8px 16px;color:#7f8c8d;font-size:13px">'
                            f'交互报告: {links_html}'
                            f'<span style="font-size:11px">(30分钟)</span></td></tr>'
                        )
        except Exception as e:
            logger.debug(f"交互报告链接生成失败: {e}")

        # 9. 替换主模板变量
        placement_section = self._build_placement_section(placements, stock_data)
        html_content = email_template.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            alert_section=alert_section,
            all_rows_price=all_rows_price,
            all_rows_fundamental=all_rows_fundamental,
            price_table_header=price_table_header,
            announcements_section=announcements_section,
            placement_section=placement_section,
            chart_section=chart_section,
            portfolio_chart_section=portfolio_chart_section,
            strategy_alert_section=strategy_alert_section,
            backtest_section=backtest_section,
            report_link=report_link,
            portfolio_section=portfolio_section,
            strategy_results_section=strategy_results_section,
            server_hostname=server_info["hostname"],
            server_ip=server_info["ip_address"],
        )

        return html_content

    def _is_multi_alert_format(self, alert):
        """
        判断警报是否为多层级格式

        Args:
            alert: 警报字典

        Returns:
            bool: 如果是多层级格式返回True，否则返回False
        """
        # 多层级警报包含anchor_name字段，单锚点警报包含ma60字段
        return "anchor_name" in alert and "interval_label" in alert

    def _build_alert_rows_multi(self, alert, stock_data):
        """
        构建多层级警报的行HTML

        Args:
            alert: 多层级警报字典
            stock_data: 股票数据DataFrame

        Returns:
            tuple: (technical_row, fundamental_row) HTML字符串
        """
        stock_code = alert.get("stock_code", "")
        # 从stock_data管道获取股票名称
        stock_name = stock_code
        stock_row_lookup = stock_data[stock_data["stock_code"] == stock_code]
        if not stock_row_lookup.empty:
            stock_name = stock_row_lookup.iloc[0].get("stock_name", stock_code)
        anchor_name = alert.get("anchor_name", "")
        anchor_value = alert.get("anchor_value")
        interval_label = alert.get("interval_label", "")
        percentage = alert.get("percentage")
        consecutive_days = alert.get("consecutive_days", 1)
        price = alert.get("low_price")
        price_difference = alert.get("price_difference")

        # 从stock_data中查找基本面数据
        stock_row = stock_data[stock_data["stock_code"] == stock_code]

        # 获取基本面数据
        dividend_per_share = None
        dividend_yield = None
        pe_ratio = None
        pb_ratio = None
        roe = None

        if not stock_row.empty:
            dividend_per_share = stock_row.iloc[0].get("dividend_per_share")
            dividend_yield = stock_row.iloc[0].get("dividend_yield")
            pe_ratio = stock_row.iloc[0].get("pe_ratio")
            pb_ratio = stock_row.iloc[0].get("pb_ratio")
            roe = stock_row.iloc[0].get("roe")

        # 格式化基本面数据
        dividend_per_share_str = (
            f"{dividend_per_share:.3f}"
            if dividend_per_share is not None and not pd.isna(dividend_per_share)
            else "—"
        )
        dividend_yield_str = (
            f"{dividend_yield:.2f}%"
            if dividend_yield is not None and not pd.isna(dividend_yield)
            else "—"
        )
        pe_ratio_str = (
            f"{pe_ratio:.2f}" if pe_ratio is not None and not pd.isna(pe_ratio) else "—"
        )
        pb_ratio_str = (
            f"{pb_ratio:.2f}" if pb_ratio is not None and not pd.isna(pb_ratio) else "—"
        )
        roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "—"

        # 构建技术指标行
        condition = f"{anchor_name} 区间 {interval_label} (连续{consecutive_days}天)"
        price_str = f"{price:.2f}" if price is not None and not pd.isna(price) else "—"
        anchor_value_str = (
            f"{anchor_value:.2f}"
            if anchor_value is not None and not pd.isna(anchor_value)
            else "—"
        )
        price_diff_str = (
            f"{price_difference:+.2f}"
            if price_difference is not None and not pd.isna(price_difference)
            else "—"
        )
        pct_str = (
            f"{percentage:+.2f}%"
            if percentage is not None and not pd.isna(percentage)
            else "—"
        )

        technical_row = f"""
            <tr style="background:#fef9e7">
                <td>{stock_code}</td>
                <td>{stock_name}</td>
                <td>{price_str}</td>
                <td>{anchor_value_str}</td>
                <td>{price_diff_str}</td>
                <td>{pct_str}</td>
                <td>{anchor_name}</td>
                <td>{interval_label}</td>
                <td>{consecutive_days}天</td>
                <td>{condition}</td>
            </tr>
        """

        # 构建基本面指标行
        fundamental_row = f"""
            <tr style="background:#fef9e7">
                <td>{stock_code}</td>
                <td>{stock_name}</td>
                <td>{dividend_per_share_str}</td>
                <td>{dividend_yield_str}</td>
                <td>{pe_ratio_str}</td>
                <td>{pb_ratio_str}</td>
                <td>{roe_str}</td>
            </tr>
        """

        return technical_row, fundamental_row

    def _markdown_to_html(self, text):
        """
        将Markdown文本转换为HTML

        Args:
            text: Markdown格式文本

        Returns:
            str: HTML格式文本
        """
        if not text:
            return ""

        try:
            # 尝试使用markdown库
            import markdown

            # 基本扩展，支持粗体、列表等
            html = markdown.markdown(text, extensions=["extra", "nl2br"])
            return html
        except ImportError:
            # 如果markdown库不可用，进行简单转换
            # 替换粗体语法：**text** -> <strong>text</strong>
            import re

            html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)
            # 替换斜体语法：*text* -> <em>text</em>
            html = re.sub(r"\*(.*?)\*", r"<em>\1</em>", html)
            # 替换无序列表：* item -> <li>item</li>
            lines = html.split("\n")
            in_list = False
            result_lines = []
            for line in lines:
                if line.strip().startswith("* ") or line.strip().startswith("- "):
                    if not in_list:
                        result_lines.append("<ul>")
                        in_list = True
                    content = line.strip()[2:].strip()
                    result_lines.append(f"<li>{content}</li>")
                else:
                    if in_list:
                        result_lines.append("</ul>")
                        in_list = False
                    result_lines.append(line)
            if in_list:
                result_lines.append("</ul>")
            html = "\n".join(result_lines)
            return html

    def _get_server_info(self):
        """
        获取服务器信息（IP地址和内核版本）

        Returns:
            dict: 包含服务器信息的字典
        """
        try:
            # 获取主机名和IP地址
            hostname = socket.gethostname()
            ip_list = []

            # 方法1: 通过socket.gethostbyname_ex获取所有IP
            try:
                _, _, ip_addresses = socket.gethostbyname_ex(hostname)
                ip_list.extend(ip_addresses)
            except Exception as e:
                logger.debug(f"gethostbyname_ex 失败: {e}")

            # 方法2: 通过hostname -I命令获取所有IP（Linux）
            try:
                result = subprocess.run(
                    ["hostname", "-I"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    ips = result.stdout.strip().split()
                    ip_list.extend(ips)
            except Exception as e:
                logger.debug(f"hostname -I 失败: {e}")

            # 方法3: 获取公网IP（可选）
            try:
                import urllib.request

                ip_url = self.config.get("health_server", {}).get(
                    "ip_detect_url", "https://ifconfig.me"
                )
                public_ip = (
                    urllib.request.urlopen(ip_url, timeout=10)
                    .read()
                    .decode("utf-8")
                    .strip()
                )
                if public_ip and public_ip not in ip_list:
                    ip_list.append(f"{public_ip} (公网)")
            except Exception as e:
                logger.debug(f"公网IP检测失败: {e}")

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
                        ["uname", "-r"], capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        kernel_version = result.stdout.strip()
            except Exception as e:
                logger.debug(f"内核版本获取失败: {e}")
                kernel_version = platform.uname().release

            return {
                "hostname": hostname,
                "ip_address": ip_address,
                "kernel_version": kernel_version,
                "system": platform.system(),
                "machine": platform.machine(),
            }
        except Exception as e:
            logger.warning(f"获取服务器信息失败: {e}")
            return {
                "hostname": "未知",
                "ip_address": "无法获取",
                "kernel_version": "未知",
                "system": "未知",
                "machine": "未知",
            }

    # ────────────── 简报方法 ──────────────

    @staticmethod
    def _pick_best_anchor(
        close: float,
        anchors: dict[str, float | None],
    ) -> tuple[str, float, float] | None:
        """
        选择最优锚点：实际回溯最短 + 偏离率落入警报阈值区间。

        锚点优先级（回溯交易日短→长，越小越优先）:
            ma60(60天) > wma20(~100天) > wma30(~150天) > wma50(~250天)

        警报阈值区间 (来自 alerts.yaml thresholds):
            ≤ -10%, (-10%, -5%], (-5%, 0), [5%, 10%), [10%, 15%), ≥ 15%

        Args:
            close: 现价
            anchors: {"ma60": 5.91, "wma20": 5.78, ...}

        Returns:
            (anchor_name, anchor_value, deviation_pct) 或 None
        """
        # 窗口优先级（按实际交易日排序，数字越小越优先）
        #   ma60: 日线60个交易日 → 最短
        #   wma20: 周线≈100交易日 (20×5)
        #   wma30: 周线≈150交易日 (30×5)
        #   wma50: 周线≈250交易日 (50×5)
        WINDOW_PRIORITY = {
            "ma60": 60,
            "wma20": 100,
            "wma30": 150,
            "wma50": 250,
        }

        def _in_alert_range(dev: float) -> bool:
            if dev <= -10.0 or dev >= 15.0:
                return True
            if -10.0 < dev <= -5.0:
                return True
            if -5.0 < dev < 0.0:
                return True
            if 5.0 <= dev < 10.0:
                return True
            if 10.0 <= dev < 15.0:
                return True
            return False

        candidates = []
        for name, value in anchors.items():
            if value is None or pd.isna(value) or value <= 0:
                continue
            dev = (close - value) / value * 100.0
            if _in_alert_range(dev):
                candidates.append(
                    (name, round(float(value), 2), round(dev, 2),
                     WINDOW_PRIORITY.get(name, 999))
                )

        if not candidates:
            return None

        # 按窗口升序 → 偏离绝对值升序
        candidates.sort(key=lambda x: (x[3], abs(x[2])))
        best = candidates[0]
        return (best[0], best[1], best[2])

    def send_brief_report(self, session, report_config: dict):
        """
        发送简报邮件（仅价格+锚点偏离率，无图表/基本面/公告）。

        Args:
            session: SessionContext
            report_config: 简报配置 {"id": "morning_snapshot", "label": "早盘简报", ...}
        """
        from datetime import datetime
        from pathlib import Path

        label = report_config.get("label", "简报")
        stock_data = session.get_all_dataframe()
        today = datetime.now()
        today_date = today.date()

        # ── 构建每只股票的行 ──
        rows_data = build_brief_entries(stock_data, today)

        # ── 策略信号（直接用 SignalScanner 结果，和日报一致）──
        signal_scan = getattr(session, "signal_scan", None)
        strat_html = ""
        if signal_scan and signal_scan.alerts:
            alerts = signal_scan.alerts
            map_a = _build_signal_label_map("a_share")
            map_hk = _build_signal_label_map("hk") or _build_signal_label_map("non_a_share")
            map_us = _build_signal_label_map("us") or _build_signal_label_map("non_a_share")
            strat_html = (
                "<h3>策略信号</h3>"
                f"<p>共 {len(alerts)} 条策略告警</p>"
                "<table><tr><th>代码</th><th>信号</th><th>当前值</th></tr>"
            )
            for a in alerts[:12]:
                code = getattr(a, "stock_code", "?")
                raw = getattr(a, "rule_label", "?")
                readable = _readable_signal(code, raw, map_a, map_hk, map_us)
                cv = getattr(a, "current_value", "-")
                strat_html += (
                    f"<tr><td>{code}</td><td>{readable}</td><td>{cv}</td></tr>"
                )
            strat_html += "</table><br>"
        elif signal_scan:
            strat_html = "<h3>策略信号</h3><p>当前无活跃信号</p><br>"

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
                f"{entry['anchor_val']:.2f}"
                if entry["anchor_val"] is not None
                else "-"
            )
            dev_color = "#27ae60" if (entry["dev_pct"] or 0) >= 0 else "#c0392b"
            html_row = (
                f"<tr>"
                f"<td>{entry['code']}</td>"
                f"<td>{entry['name']}</td>"
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
        template_dir = Path(__file__).parent.parent / "templates"
        template = (template_dir / "brief_email.html").read_text(encoding="utf-8")

        body = template.format(
            label=label,
            report_date=today.strftime("%Y-%m-%d"),
            current_time=today.strftime("%H:%M"),
            active_count=active_count,
            total_count=active_count,
            brief_rows=rows,
            strategy_suggestions=strat_html,
        )

        subject = f"{label} - {today.strftime('%Y-%m-%d')}"
        self._send_email(subject, body)

    # ────────────────────────────────────

    def _send_email(self, subject, body, chart_png_bytes=None, portfolio_chart_dict=None, pdf_bytes=None):
        """
        发送邮件

        Args:
            subject: 邮件主题
            body: 邮件正文（HTML格式）
            chart_png_bytes: 告警走势图 PNG 字节（可选），CID=chart001
            portfolio_chart_dict: 投资组合走势图 {"a_share": bytes, "non_a_share": bytes}
            pdf_bytes: 日报 PDF 附件 bytes（可选）
        """
        import os

        # 保存邮件副本（无论是否跳过发送）
        copy_path = self._save_email_copy(subject, body)
        if copy_path:
            logger.info("邮件副本保存成功，路径: %s", copy_path)
        else:
            logger.error("邮件副本未保存，目标目录: %s", self.email_archive_dir)

        if os.environ.get("SKIP_EMAIL") == "true":
            logger.info(f"跳过邮件发送（测试模式）: 主题={subject}")
            return

        try:
            # 创建 HTML 部分
            html_part = MIMEText(body, "html", "utf-8")
            html_part.set_charset("utf-8")
            html_part["Content-Transfer-Encoding"] = "quoted-printable"

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
                    image.add_header("Content-Disposition", "inline", filename="chart.png")
                    inner.attach(image)
                    logger.info("告警走势图以 CID chart001 嵌入邮件")

                # 添加投资组合走势图（CID: chart002=A股, chart003=非A股）
                if portfolio_chart_dict:
                    cid_map = {"a_share": "chart002", "hk": "chart003",
                               "us": "chart004", "non_a_share": "chart005"}
                    for group_key, png_bytes in portfolio_chart_dict.items():
                        if png_bytes and group_key in cid_map:
                            cid = cid_map[group_key]
                            img = MIMEImage(png_bytes, "png")
                            img.add_header("Content-ID", f"<{cid}>")
                            img.add_header("Content-Disposition", "inline",
                                           filename=f"portfolio_{group_key}.png")
                            inner.attach(img)
                            logger.info(f"投资组合走势图以 CID {cid} 嵌入邮件")
            else:
                # 无图表：保持原逻辑
                inner = MIMEMultipart("alternative")
                inner.policy = policy.default
                inner.attach(html_part)

            # 如有 PDF 附件，外层包 MIMEMultipart("mixed")
            if pdf_bytes:
                from email.mime.application import MIMEApplication
                msg = MIMEMultipart("mixed")
                msg.policy = policy.default
                msg.attach(inner)
                pdf_part = MIMEApplication(pdf_bytes, "pdf")
                pdf_part.add_header("Content-Disposition", "attachment",
                                    filename="日报.pdf")
                msg.attach(pdf_part)
                logger.info("日报 PDF 已附加到邮件")
            else:
                msg = inner

            # 邮件主题（使用UTF-8编码策略自动处理）
            msg["Subject"] = subject

            # 编码发件人和收件人
            msg["From"] = self.sender_email
            msg["To"] = self.receiver_email

            # 连接到SMTP服务器并发送邮件
            if self.enable_ssl:
                # 使用SSL连接
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(
                    self.smtp_server, self.smtp_port, timeout=30, context=context
                ) as server:
                    # 登录邮箱
                    server.login(self.sender_email, self.sender_password)

                    # 发送邮件
                    server.send_message(msg)
            else:
                # 使用普通SMTP连接
                with smtplib.SMTP(
                    self.smtp_server, self.smtp_port, timeout=30
                ) as server:
                    if self.enable_tls:
                        server.starttls()  # 启用TLS加密

                    # 登录邮箱
                    server.login(self.sender_email, self.sender_password)

                    # 发送邮件
                    server.send_message(msg)

            logger.debug(f"邮件发送成功: {subject}")

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP认证失败: {e}")
            raise
        except smtplib.SMTPException as e:
            logger.error(f"SMTP错误: {e}")
            raise
        except Exception as e:
            logger.error(f"发送邮件时发生未知错误: {e}", exc_info=True)
            raise

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
            subject = f"部署完成通知 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

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
        <p><strong>部署时间:</strong> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
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
            self._send_email(subject, body)
            logger.info(f"部署通知邮件发送成功: {subject} (version={version})")
            return True, "sent"

        except Exception as e:
            msg = str(e)
            logger.error(f"发送部署通知邮件失败: {msg}")
            return False, msg

    def send_optimizer_notification(self, report, group_name: str = "") -> None:
        """发送优化结果邮件。"""
        body = build_optimizer_summary(report, group_name)
        subject = f"策略优化完成 · {group_name}" if group_name else "策略优化完成"
        self._send_email(subject, f"<pre>{body}</pre>")

    def _save_email_copy(self, subject, body):
        """
        保存邮件副本到本地文件

        Args:
            subject: 邮件主题
            body: 邮件正文（HTML格式）
        """
        try:
            # 生成文件名：日期_时间_主题前30字符
            current_time = datetime.now()
            date_str = current_time.strftime("%Y%m%d")
            time_str = current_time.strftime("%H%M%S")
            # 清理主题中的非法文件名字符
            clean_subject = "".join(
                c if c.isalnum() or c in " _-" else "_" for c in subject
            )
            clean_subject = clean_subject[:50]  # 限制长度

            filename = f"{date_str}_{time_str}_{clean_subject}.html"
            filepath = self.email_archive_dir / filename

            # 直接保存正文（已经是完整 HTML 文档，由 email_template.html 渲染）
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

        except Exception as e:
            logger.error(
                "保存邮件副本失败: %s, 目标目录: %s", e, self.email_archive_dir
            )
            return None

    # ── 日报 PDF 生成 ──

    def _chart_deviation_timeline(
        self, signal_scan, backtest, base64=True,
    ) -> str:
        """
        偏离度 30 日折线图: 取偏离绝对值最大的 5 只标的 + 触发信号的标的，
        叠加折线。虚线标注买入阈值。

        Returns:
            base64 PNG 字符串 或 HTML <img> 标签
        """
        import io, base64
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ..utils.font_setup import setup_cjk_font

        setup_cjk_font()

        snapshot = (getattr(signal_scan, "indicator_snapshot", {})
                    if signal_scan else {})
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
        colors = ["#2d8a56", "#c9a84c", "#2980b9", "#c0392b",
                  "#8e44ad", "#e67e22", "#1abc9c", "#34495e"]

        for i, code in enumerate(top5):
            vals = snapshot.get(code, {})
            d = vals.get("deviation", 0) * 100  # → %
            color = colors[i % len(colors)]
            ax.barh(i, d, color=color, height=0.5, alpha=0.85)
            label = f"{code[-4:]} {d:+.1f}%"
            x_pos = d + (0.5 if d >= 0 else -0.5)
            ha = "left" if d >= 0 else "right"
            ax.text(x_pos, i, label, va="center", ha=ha, fontsize=7,
                    color=color, fontweight="bold")

        # 买入阈值虚线
        ax.axvline(x=-0.5, color="#888", linestyle="--", linewidth=0.6, alpha=0.5)
        ax.text(-0.5, len(top5)-0.3, " 买入阈值 -0.5%", fontsize=6,
                color="#888", va="bottom")

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
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                    facecolor="white")
        plt.close(fig)
        buf.seek(0)

        if base64:
            b64 = base64.b64encode(buf.read()).decode()
            return f'<img src="data:image/png;base64,{b64}" style="max-width:100%"/>'
        return buf.read()

    def _generate_daily_pdf(
        self, session, alert_stocks, signal_scan, backtest, stock_data,
    ) -> bytes | None:
        """生成日报 PDF（xelatex 编译 LaTeX 模板），返回 bytes"""
        import tempfile, subprocess, os, io, re

        try:
            # 1. 图表 PNG
            chart_buf = self._chart_deviation_timeline(signal_scan, backtest, base64=False)
            chart_path = None
            if chart_buf and not isinstance(chart_buf, str):
                fd, chart_path = tempfile.mkstemp(suffix=".png", prefix="chart_")
                with open(fd, "wb") as f:
                    data = chart_buf if isinstance(chart_buf, bytes) else chart_buf.getvalue() if hasattr(chart_buf, 'getvalue') else chart_buf
                    f.write(data)
                chart_section = f"\\includegraphics[width=\\textwidth]{{{chart_path}}}"
            else:
                chart_section = "{\\color{gray}\\small 今日无图表数据}"

            # 2. KPI
            sa = getattr(signal_scan, "alerts", None) or [] if signal_scan else []

            # ── LaTeX 转义函数 (必须在使用前定义) ──
            def _esc(s):
                """LaTeX 转义"""
                return str(s).replace("&", "\\&").replace("%", "\\%").replace("#", "\\#").replace("$", "\\$").replace("_", "\\_")

            buy_count = len(sa)
            bt_a = backtest.get("a_share", {}) if backtest else {}
            bt_n = backtest.get("non_a_share", {}) if backtest else {}

            ta = bt_a.get("total_return")  # None = no backtest data
            tn = bt_n.get("total_return")
            has_backtest = ta is not None or tn is not None
            kpi_buy = str(buy_count)
            kpi_a = f"{ta:+.1f}\\%" if ta is not None else "—"
            kpi_n = f"{tn:+.1f}\\%" if tn is not None else "—"
            if not has_backtest:
                kpi_s = "⚙ 优化未运行"
            elif (ta if ta is not None else 0) > 0 or (tn if tn is not None else 0) > 0:
                kpi_s = "✓ 策略有效"
            else:
                kpi_s = "✗ 策略无效"
            kpi_color_a = "green" if ta is not None and ta > 0 else "red"
            kpi_color_n = "green" if tn is not None and tn > 0 else "red"
            kpi_color_s = "green" if buy_count > 0 else "red"

            # 3. 触发信号
            trigger_lines = []
            for a in sa:
                code = getattr(a, "stock_code", "?")
                label = _esc(getattr(a, "rule_label", "?"))
                cv = _esc(str(getattr(a, "current_value", "—")))
                trigger_lines.append(
                    f"\\textbf{{{_esc(code)}}} & {label} & {cv} \\\\"
                )
            trigger_section = (
                "\\begin{tabular}{lll}\n" +
                "\\textbf{标的} & \\textbf{信号规则} & \\textbf{当前值}\\\\\n" +
                "\n".join(trigger_lines) +
                "\n\\end{tabular}"
                if trigger_lines else
                "{\\color{gray}\\small 今日无触发信号}"
            )

            # 4. 表: 合并 indicator_snapshot + fundamentals
            snapshot = (getattr(signal_scan, "indicator_snapshot", {})
                        if signal_scan else {})
            consensus = (getattr(signal_scan, "consensus", None)
                         if signal_scan else None)
            cons_inds = consensus.consensus_indicators if consensus else ["deviation", "rsi"]
            fundamentals = {}
            if stock_data is not None and hasattr(stock_data, "iterrows"):
                for _, row in stock_data.iterrows():
                    code = str(row.get("stock_code", ""))
                    if not code:
                        continue
                    pe = row.get("pe_ratio")
                    pb = row.get("pb_ratio")
                    dy = row.get("dividend_yield")
                    fundamentals[code] = {
                        "pe": f"{pe:.1f}" if pe is not None and not pd.isna(pe) else "—",
                        "pb": f"{pb:.2f}" if pb is not None and not pd.isna(pb) else "—",
                        "dy": f"{dy:.2f}" if dy is not None and not pd.isna(dy) else "—",
                    }

            header_cols = ["标的"] + cons_inds + ["息\\%", "PE", "PB", "信号"]
            # 列格式: l for 标的, c for signal, r for numbers
            col_fmt = "l" + "r" * len(cons_inds) + "r" * 3 + "c"
            table_rows = ""
            alert_codes = set(getattr(a, "stock_code", "") for a in sa)

            a_codes = sorted(
                [c for c in snapshot if (c.isdigit() and len(c) == 6) or c.replace(".", "").isdigit()],
                key=lambda c: (abs(snapshot[c].get("deviation", 0) or 0)),
                reverse=True,
            )
            for code in a_codes:
                vals = snapshot.get(code, {})
                sig = "●" if code in alert_codes else ""
                cells = [_esc(code)]
                for ind in cons_inds:
                    v = vals.get(ind)  # None = 缺失, 0 = 真实零
                    if v is None:
                        cells.append("—")
                    elif ind == "deviation":
                        cells.append(f"{v*100:+.1f}\\%")
                    else:
                        cells.append(f"{v:.2f}")
                fund = fundamentals.get(code, {})
                cells.append(fund.get("dy", "—"))
                cells.append(fund.get("pe", "—"))
                cells.append(fund.get("pb", "—"))
                cells.append(sig)
                row_color = "\\rowcolor{bg!30}" if sig else ""
                table_rows += f"{row_color}{' & '.join(cells)} \\\\\n"

            nona_codes = sorted(
                [c for c in snapshot if c not in a_codes],
                key=lambda c: (abs(snapshot[c].get("deviation", 0) or 0)),
                reverse=True,
            )
            if nona_codes:
                table_rows += (
                    f"\\multicolumn{{{len(header_cols)}}}{{l}}{{\\color{{navy}}\\textbf{{境外 · {len(nona_codes)} 只}}}}\\\\\n"
                )
            for code in nona_codes:
                vals = snapshot.get(code, {})
                sig = "●" if code in alert_codes else ""
                cells = [_esc(code)]
                for ind in cons_inds:
                    v = vals.get(ind)  # None = 缺失, 0 = 真实零
                    if v is None:
                        cells.append("—")
                    elif ind == "deviation":
                        cells.append(f"{v*100:+.1f}\\%")
                    else:
                        cells.append(f"{v:.2f}")
                fund = fundamentals.get(code, {})
                cells.append(fund.get("dy", "—"))
                cells.append(fund.get("pe", "—"))
                cells.append(fund.get("pb", "—"))
                cells.append(sig)
                row_color = "\\rowcolor{bg!30}" if sig else ""
                table_rows += f"{row_color}{' & '.join(cells)} \\\\\n"

            table_section = (
                "\\small\n"
                "\\rowcolors{2}{white}{stripe}\n"
                f"\\begin{{tabular}}{{{col_fmt}}}\n"
                "\\toprule\n"
                + " & ".join(header_cols) + " \\\\\n"
                "\\midrule\n"
                + table_rows +
                "\\bottomrule\n"
                "\\end{tabular}"
            )

            # 5. 脚注
            buy_sigs = consensus.buy_signal_counts if consensus else {}
            strat_note = " · ".join(list(buy_sigs.keys())[:4]) if buy_sigs else "—"
            bt_text = f"A股策略超额 {ta:+.1f}\\%" if ta is not None else "A股策略未运行"
            if bt_a.get("benchmarks"):
                for bn, bv in bt_a["benchmarks"].items():
                    beat = "✓" if (ta is not None and ta > bv) else "✗"
                    bt_text += f"\\quad vs {bn} {bv:+.1f}\\% {beat}"

            # 6. 附录
            md_path = (
                Path(__file__).parent.parent / "templates" / "appendix_methodology.md"
            )
            appendix_section = ""
            if md_path.exists():
                md_text = md_path.read_text(encoding="utf-8")
                # 简单的 MD → LaTeX 转换
                latex_lines = []
                in_list = False
                in_table = False
                for line in md_text.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        if in_list:
                            latex_lines.append("\\end{itemize}")
                            in_list = False
                        if in_table:
                            latex_lines.append("\\end{tabular}")
                            in_table = False
                        latex_lines.append(f"\\section*{{{stripped[2:]}}}")
                    elif stripped.startswith("## "):
                        if in_list:
                            latex_lines.append("\\end{itemize}")
                            in_list = False
                        latex_lines.append(f"\\subsection*{{{stripped[3:]}}}")
                    elif stripped.startswith("- "):
                        if not in_list:
                            latex_lines.append("\\begin{itemize}")
                            in_list = True
                        item = stripped[2:]
                        item = re.sub(r'\*\*(.+?)\*\*', r'\\textbf{\1}', item)
                        latex_lines.append(f"  \\item {item}")
                    elif stripped.startswith("---"):
                        latex_lines.append("\\vspace{4pt}\\hrule\\vspace{4pt}")
                    elif stripped.startswith("$$"):
                        formula = stripped.strip("$").strip()
                        latex_lines.append(f"\\[{formula}\\]")
                    elif stripped.startswith("|"):
                        if not in_table:
                            cols = stripped.count("|") - 1
                            latex_lines.append(f"\\begin{{tabular}}{{{'l'*cols}}}")
                            latex_lines.append("\\toprule")
                            in_table = True
                        else:
                            cells = [c.strip() for c in stripped.split("|")[1:-1]]
                            latex_lines.append(" & ".join(cells) + " \\\\")
                    elif in_table and not stripped.startswith("|"):
                        latex_lines.append("\\bottomrule")
                        latex_lines.append("\\end{tabular}")
                        in_table = False
                    elif stripped:
                        item = re.sub(r'\*\*(.+?)\*\*', r'\\textbf{\1}', stripped)
                        item = re.sub(r'\$(.+?)\$', r'$\1$', item)
                        latex_lines.append(f"{item}\n")
                if in_list:
                    latex_lines.append("\\end{itemize}")
                if in_table:
                    latex_lines.append("\\end{tabular}")
                appendix_section = "\n".join(latex_lines)

            # 7. 渲染模板
            tex_path = Path(__file__).parent.parent / "templates" / "report_daily.tex"
            template = tex_path.read_text(encoding="utf-8").replace("\r\n", "\n")

            info = self._get_server_info()
            html = template.replace("\\VAR{report_date}", datetime.now().strftime("%Y-%m-%d %A"))
            html = html.replace("\\VAR{server_hostname}", info.get("hostname", ""))
            html = html.replace("\\VAR{kpi_buy}", kpi_buy)
            html = html.replace("\\VAR{kpi_a}", kpi_a)
            html = html.replace("\\VAR{kpi_n}", kpi_n)
            html = html.replace("\\VAR{kpi_s}", kpi_s)
            html = html.replace("\\VAR{kpi_color_a}", kpi_color_a)
            html = html.replace("\\VAR{kpi_color_n}", kpi_color_n)
            html = html.replace("\\VAR{kpi_color_s}", kpi_color_s)
            html = html.replace("\\VAR{chart_section}", chart_section)
            html = html.replace("\\VAR{trigger_section}", trigger_section)
            html = html.replace("\\VAR{table_section}", table_section)
            html = html.replace("\\VAR{strategy_note}", strat_note)
            html = html.replace("\\VAR{backtest_note}", bt_text)
            html = html.replace("\\VAR{appendix_section}", appendix_section)

            # 8. xelatex 编译
            with tempfile.TemporaryDirectory() as tmpdir:
                tex_file = Path(tmpdir) / "report.tex"
                tex_file.write_text(html, encoding="utf-8")

                for _ in range(2):  # 两次编译（交叉引用）
                    result = subprocess.run(
                        ["xelatex", "-interaction=nonstopmode", "-output-directory",
                         tmpdir, str(tex_file)],
                        capture_output=True, text=True, timeout=60,
                    )
                    if result.returncode != 0:
                        log_file = Path(tmpdir) / "report.log"
                        log_tail = ""
                        if log_file.exists():
                            lines = log_file.read_text(errors="replace").split("\n")
                            # 找第一个 "!" 错误行
                            for i, l in enumerate(lines):
                                if l.startswith("!"):
                                    log_tail = "\\n".join(lines[max(0,i-1):i+5])
                                    break
                        logger.warning("xelatex 编译问题: %s", log_tail or result.stderr[-200:])

                pdf_file = Path(tmpdir) / "report.pdf"
                if pdf_file.exists():
                    pdf_bytes = pdf_file.read_bytes()
                    logger.info("日报 PDF 生成成功 (%d bytes)", len(pdf_bytes))
                    return pdf_bytes
                else:
                    log_file = Path(tmpdir) / "report.log"
                    if log_file.exists():
                        logger.error("xelatex 日志: %s", log_file.read_text(errors="replace")[-500:])
                    logger.error("xelatex 未产出 PDF")
                    return None

        except Exception as e:
            logger.error("生成日报 PDF 失败: %s", e)
            return None
        finally:
            if chart_path and os.path.exists(chart_path):
                os.unlink(chart_path)
