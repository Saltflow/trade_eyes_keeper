"""Report builders shared by the email / Feishu / Telegram notifiers.

Extracted from email_notifier.py, which had grown to 4,743 lines: 15
module-level builders plus a 3,690-line class. These builders are not
email-specific -- feishu_notifier.py and telegram_notifier.py both import them
-- so they belong in their own module.

email_notifier.py re-exports every name here, so existing importers are
unaffected.
"""

import html as html_lib
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def pick_best_anchor(
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
                (
                    name,
                    round(float(value), 2),
                    round(dev, 2),
                    WINDOW_PRIORITY.get(name, 999),
                )
            )

    if not candidates:
        return None

    # 按窗口升序 → 偏离绝对值升序
    candidates.sort(key=lambda x: (x[3], abs(x[2])))
    best = candidates[0]
    return (best[0], best[1], best[2])


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


def _signed_text_metric(value, unit="%", precision=2) -> str:
    """Format optional report metrics without turning missing data into zero."""
    if value is None:
        return "—"
    try:
        if pd.isna(value):
            return "—"
        return f"{float(value):+.{precision}f}{unit}"
    except (TypeError, ValueError):
        return "—"


def _html_escape(value, default="—") -> str:
    """Escape notification data without collapsing real zeroes to a dash."""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    return html_lib.escape(str(value), quote=True)


def _safe_html_url(value) -> str:
    """Return an escaped http(s) URL or an empty string for unsafe input."""
    if value is None:
        return ""
    raw = str(value).strip()
    if raw.lower().startswith(("http://", "https://")):
        return html_lib.escape(raw, quote=True)
    return ""


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
            best = pick_best_anchor(float(close_price), anchors)
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

        entries.append(
            {
                "code": code,
                "name": name,
                "close": close_price,
                "open": open_price,
                "anchor_name": anchor_name,
                "anchor_val": anchor_val,
                "dev_pct": dev_pct,
                "dev_str": f"{dev_pct:+.2f}%" if dev_pct is not None else "-",
                "sort_key": dev_pct if dev_pct is not None else float("inf"),
            }
        )

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
    today_date = (
        today.date() if hasattr(today, "date") else (today or datetime.now().date())
    )

    # 空数据直接返回
    if stock_data is None or stock_data.empty or "close" not in stock_data.columns:
        return None

    # 找最新 A 股优化结果
    opt_dir = Path("data/optimizer")
    yaml_files = sorted(
        [
            f
            for f in opt_dir.glob("*_a_share_strategies.yaml")
            if "non_a_share" not in f.name
        ],
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
                    dev200 = (
                        float(df.loc[row.name, "ma200_dev"])
                        if "ma200_dev" in df.columns
                        else np.nan
                    )
                    slope = (
                        float(df.loc[row.name, "ma60_slope"])
                        if "ma60_slope" in df.columns
                        else np.nan
                    )
                    t = -0.05 + t_raw * (-0.35)
                    triggered = (
                        not np.isnan(dev200)
                        and not np.isnan(slope)
                        and dev200 <= t
                        and slope > -0.005
                    )
                elif signal == "absolute_discount":
                    pct_ath = (
                        float(df.loc[row.name, "pct_from_ath"])
                        if "pct_from_ath" in df.columns
                        else np.nan
                    )
                    t = -0.10 + t_raw * (-0.60)
                    triggered = not np.isnan(pct_ath) and pct_ath <= t
                elif signal == "trend_follow":
                    adx = float(row.get("adx", np.nan))
                    macd = float(row.get("macd_hist", np.nan))
                    t = 15 + t_raw * 25
                    triggered = (
                        not np.isnan(adx)
                        and not np.isnan(macd)
                        and adx > t
                        and macd > 0
                    )
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
                active_signals.append(f"{signal}({frac * 100:.0f}%)")

        entries.append(
            {
                "code": code,
                "name": name,
                "close": round(c, 2),
                "signals": active_signals,
                "signal_count": len(active_signals),
            }
        )

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
        text_parts.append(
            f"{e['code']:<8} {e['name'][:6]:<6} {e['close']:>7.2f}  {sigs}"
        )

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
        is_non_a = group == "non_a_share"
        yaml_files = sorted(
            [
                f
                for f in opt_dir.glob(f"*_{group}_strategies.yaml")
                if ("non_a_share" in f.name) == is_non_a
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
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
            rule_id = f"buy_{idx}" if k.startswith("buy") else f"sell_{idx}"
            label_map[rule_id] = SIGNAL_NAMES.get(v, v)
        logger.debug(f"Signal label map: {label_map}")
        return label_map
    except Exception as e:
        logger.warning(f"读取信号标签映射失败: {e}")
        return {}


def _alert_value(alert, name: str, default: str = "?"):
    """Read an alert field whether the alert is a dict or an object.

    `_scan_group` 产出 dict alert，旧扫描器产出对象 alert；通知渲染必须
    同时兼容两者，避免裸 getattr 在 dict 上恒返回默认值。
    """
    if isinstance(alert, dict):
        value = alert.get(name)
    else:
        value = getattr(alert, name, None)
    return value if value not in (None, "") else default


def _readable_signal(
    code: str, rule_label: str, map_a: dict, map_hk: dict, map_us: dict = None
) -> str:
    """按标的所属细分组把 rule_id(如 buy_1) 翻译成信号名(如 趋势跟踪)。

    A股/港股/美股各用自己的 YAML 映射 — 三组信号名可能都不同。
    map_us 缺省时港美股共用 map_hk（向后兼容非A单组）。
    """
    try:
        from ..markets import _detect_fine_group
    except (ImportError, ValueError):
        from src.markets import _detect_fine_group
    g = _detect_fine_group(str(code))
    if g == "a_share":
        m = map_a
    elif g == "hk":
        m = map_hk
    else:  # us
        m = map_us if map_us is not None else map_hk
    return m.get(rule_label, rule_label)


def _benchmark_label(code: str) -> str:
    """Return the human label used consistently in every notification channel."""
    return "无风险" if code == "risk_free" else code


def _format_benchmark_comparison(report) -> tuple[list[str], list[str]]:
    """Render cumulative benchmark returns and forward win rates separately."""
    returns = getattr(report, "benchmark_returns", None) or {}
    win_rates = getattr(report, "benchmark_win_rates", None) or {}
    details = getattr(report, "benchmark_details", None) or {}
    primary = getattr(report, "primary_benchmark", "") or ""
    return_parts = []
    win_parts = []
    for code, benchmark_return in returns.items():
        try:
            excess = float(report.total_return) - float(benchmark_return)
            marker = "★" if code == primary else ""
            return_parts.append(
                f"{marker}{_benchmark_label(code)} {float(benchmark_return):+.1f}% / "
                f"超额{excess:+.1f}%"
            )
        except (TypeError, ValueError):
            continue
    for code, win_rate in win_rates.items():
        try:
            detail = details.get(code, {}) or {}
            wins = detail.get("win_days")
            effective = detail.get("comparison_days", detail.get("effective_days"))
            count_text = (
                f" ({int(wins)}/{int(effective)})"
                if wins is not None and effective is not None
                else ""
            )
            marker = "★" if code == primary else ""
            win_parts.append(
                f"{marker}{_benchmark_label(code)} {float(win_rate):.0f}%{count_text}"
            )
        except (TypeError, ValueError):
            continue
    return return_parts, win_parts


def build_strategy_text_summary(session, markdown: bool = False) -> str:
    """构建搜参策略 + 今日信号 + 定增的纯文本摘要（Telegram/飞书共享）。

    数据源统一为 session.evaluation_reports（EvaluationReport dict）。

    Args:
        session: SessionContext（读 evaluation_reports/signal_scan/placements）
        markdown: True=飞书(**粗体**), False=Telegram(纯文本)
    """
    b = "**" if markdown else ""
    reports = getattr(session, "evaluation_reports", {}) or {}
    signal_scan = getattr(session, "signal_scan", None)
    placements = getattr(session, "placements", None)
    lines: list[str] = []

    # 策略引擎名
    first_r = next(iter(reports.values()), None)
    if first_r:
        lines.append(
            f"{b}搜参引擎{b}: {first_r.strategy_label} "
            f"({first_r.engine_name})  {first_r.timestamp[:16].replace('T', ' ')}"
        )
        lines.append("")

    # 搜参策略结果
    group_labels = {"a_share": "A股组合", "hk": "港股组合", "us": "美股组合"}
    for gk, gl in group_labels.items():
        r = reports.get(gk)
        if r is None:
            continue

        qh = r.quarterly_holdings or []
        cash_pcts = [(100 - q.get("pos_pct", 0)) for q in qh if q.get("nav", 0) > 0]
        avg_cash = sum(cash_pcts) / len(cash_pcts) if cash_pcts else None

        benchmark_parts, win_rate_parts = _format_benchmark_comparison(r)

        head = (
            f"{b}{gl}{b} 评估期收益 {r.total_return:+.1f}% "
            f"(超额{r.excess_return:+.1f}%)"
            f"  最大回撤 {r.max_drawdown:.1f}%  夏普 {r.sharpe_ratio:.2f}"
            f"  交易 {int(r.trade_count)}笔"
        )
        if avg_cash is not None:
            head += f"  平均现金仓位 {avg_cash:.0f}%"
        lines.append(head)
        if benchmark_parts:
            lines.append(
                "  三基线收益 / 策略超额: " + " | ".join(benchmark_parts)
            )
        if win_rate_parts:
            lines.append(
                "  验证期胜率(任意日买入持有到期跑赢): "
                + " | ".join(win_rate_parts)
            )
        selection = getattr(r, "selection_diagnostics", None) or {}
        ranking = selection.get("ranking_diagnostics", {})
        sensitivity = selection.get("sensitivity", {})
        if ranking:
            lines.append(
                "  Ranking 窗口筛选: "
                f"加权绝对收益 {float(ranking.get('weighted_strategy_return', 0.0)):+.2f}% | "
                f"正收益窗口 {int(ranking.get('positive_return_windows', 0))}/"
                f"{int(ranking.get('ranking_window_count', 0))} | "
                f"基础 WF {float(selection.get('wf_score') or 0.0):+.3f}"
            )
        if sensitivity:
            final_score = selection.get("selection_score")
            if final_score is None:
                final_score = sensitivity.get("selection_score")
            lines.append(
                "  稳健性: "
                f"最差分 {float(sensitivity.get('worst_score', 0.0)):+.3f} | "
                f"降幅 {float(sensitivity.get('drop', 0.0)):+.3f} | "
                f"最终选择分 {float(final_score or 0.0):+.3f}"
            )
        if r.composition:
            lines.append(f"  成分: {', '.join(r.composition)}")
        holdout = selection.get("holdout_summary", {})
        if isinstance(holdout, dict) and holdout:
            lines.append(
                "  Holdout整体（窗口等权；最大回撤取最差）: "
                f"收益 {_signed_text_metric(holdout.get('return_pct'))} | "
                f"超额 {_signed_text_metric(holdout.get('excess_return_pct'))} | "
                f"最大回撤 {_signed_text_metric(holdout.get('max_drawdown_pct'))} | "
                f"Sharpe {_signed_text_metric(holdout.get('sharpe_ratio'), '', 3)}"
            )
        lines.append("  持仓明细和周 NAV OHLC 已移至 HTML/PDF 详情。")
        warming = getattr(r, "warming_codes", None) or []
        if warming:
            lines.append(f"  预热中（暂不交易）: {', '.join(warming)}")
        ts = r.timestamp[:16].replace("T", " ")
        lines.append(f"  评估时间 {ts}")
        lines.append("")

    # 今日信号
    if signal_scan and signal_scan.alerts:
        lines.append(f"{b}今日策略信号{b}:")
        for a in signal_scan.alerts[:10]:
            code = _alert_value(a, "stock_code", "?")
            raw = _alert_value(a, "rule_label", "?")
            cv = _alert_value(a, "current_value", "")
            lines.append(f"  {code} {raw} {cv}")
    elif signal_scan is not None:
        lines.append(f"{b}今日信号{b}: 无触发")
    lines.append("")

    # 定增
    if placements:
        lines.append(f"{b}未解禁定增{b}:")
        for code, p in placements.items():
            num = p.get("issue_num")
            num_str = f"{num / 1e8:.2f}亿股" if num else "—"
            price_str = f"{p.get('issue_price', '-')}元"
            lines.append(
                f"  {code} {num_str} 发行价{price_str} "
                f"解禁{p.get('unlock_date','-')}"
            )

    return "\n".join(lines).strip()


def optimizer_notification_title(report, group_name: str = "") -> str:
    """Return one consistent success/failure title for every channel."""

    failed = str(getattr(report, "status", "completed")) in {
        "failed",
        "interrupted",
    }
    title = (
        "\u7b56\u7565\u4f18\u5316\u5931\u8d25"
        if failed
        else "\u7b56\u7565\u4f18\u5316\u5b8c\u6210"
    )
    return f"{title} \u00b7 {group_name}" if group_name else title


def build_optimizer_summary(
    report,
    group_name: str = "",
    full_report: dict | None = None,
    include_charts: bool = True,
) -> str:
    """将 OptimizationReport + 完整回测报告格式化为 HTML 摘要。

    full_report 非空时追加：日回报测指标 / 季末持仓 / 敏感性 / 波动率。
    include_charts=False 时跳过图片（飞书 markdown 不支持 <img>）。
    """
    if hasattr(report, "strategy_name") and isinstance(
        getattr(report, "groups", None), dict
    ):
        return _build_optimizer_run_summary(report)

    lines = ["<b>策略优化完成</b>"]
    if group_name:
        label = {
            "a_share": "A股",
            "hk": "港股",
            "us": "美股",
            "non_a_share": "非A股",
        }.get(group_name, group_name)
        lines.append(f"分组: {label}")
    lines.append(
        f"耗时: {report.elapsed_seconds:.0f}s  |  评估: {report.iterations} 策略"
    )

    if not report.top_strategies and not full_report:
        return "\n".join(lines) + "\n(无有效策略)"

    if report.top_strategies:
        lines.append("")
        for i, t in enumerate(report.top_strategies[:3]):
            lines.append(f"<b>策略 #{i + 1}</b>")
            lines.append(
                f"  收益 {t.test_return:+.1f}%  回撤 {t.test_drawdown:.1f}%  "
                f"夏普 {t.sharpe:.2f}  交易 {t.trade_count}笔"
            )
            lines.append(f"  <pre>{t.strategy_description}</pre>")
        lines.append(f"完整结果: data/optimizer/{report.report_id}.yaml")

    if full_report:
        lines.append("")
        lines.append("<b>━━━ 日回报测 (近9月验证期) ━━━</b>")
        rc = "#27ae60" if full_report["total_return"] >= 0 else "#c0392b"
        lines.append(
            f'收益 <span style="color:{rc}">{full_report["total_return"]:+.1f}%</span>'
            f" (超额 {full_report.get('excess_return') or full_report['total_return']:+.1f}%)"
            f"  回撤 {full_report['dd']:.1f}%"
            f"  夏普 {full_report['sharpe']:.2f}"
            f"  交易 {full_report['trades']}笔"
            f"  仓位 {full_report['position']:.0f}%"
        )

        # 参数摘要
        params = full_report.get("params", {})
        if params:
            pi = []
            for lbl in (
                "adx_pct",
                "rsi_pct",
                "deviation_pct",
                "vol_ratio_pct",
                "ma200_dev_pct",
            ):
                tau = params.get(f"{lbl}_tau")
                w = params.get(f"{lbl}_w")
                if tau is not None and w is not None:
                    pi.append(f"τ={tau:.2f} w={w:.2f}")
            lines.append(f"  信号: {' | '.join(pi)}")
            lines.append(
                f"  买阈 τ_buy={params.get('tau_buy', '?'):.2f}"
                f"  卖阈 τ_sell={params.get('tau_sell', '?'):.2f}"
            )
            execution = full_report.get("execution", {}) or {}
            if execution:
                lines.append(
                    "  单笔现金上限 "
                    f"买入 {float(execution.get('buy_cash_limit', 0)):.0f} / "
                    f"卖出 {float(execution.get('sell_cash_limit', 0)):.0f} 元"
                )

        # 季末持仓
        qh = full_report.get("quarterly", [])
        if qh:
            last_q = qh[-1]
            qpos = last_q.get("positions", [])
            if qpos:
                pos_str = ", ".join(
                    f"{p['code']} {p['shares']:.0f}股@{p['price']:.2f}"
                    f"({p.get('pnl_pct', 0):+.0f}%)"
                    for p in qpos[:8]
                )
                lines.append(
                    f"  期末持仓(Q{last_q.get('quarter', '?')} "
                    f"仓位{last_q.get('pos_pct', 0):.0f}%): {pos_str}"
                )

        # 参数敏感性
        sens = full_report.get("sensitivity")
        if sens:
            lines.append("")
            lines.append("<b>━━━ 参数敏感性 (10版随机扰动) ━━━</b>")
            lines.append(
                f"  最优版 {sens['base_ret']:+.1f}%"
                f" → 最差版 {sens['worst_ret']:+.1f}%"
                f" (降幅 {sens['drop_pct']:.1f}pp)"
            )
            lines.append(
                f"  10版范围 [{sens['ret_range'][0]:+.1f}%, "
                f"{sens['ret_range'][1]:+.1f}%]"
            )

        # 跨天波动率
        vol = full_report.get("volatility")
        if vol:
            lines.append("")
            lines.append("<b>━━━ 跨天波动率 (近5交易日) ━━━</b>")
            lines.append(
                f"  最低 {vol['min']:+.1f}% / 最高 {vol['max']:+.1f}%"
                f"  (波幅 {vol['range']:.1f}pp)"
            )

        # 周K 蜡烛图（邮件用 CID，飞书跳过）
        ohlc = full_report.get("weekly_ohlc")
        if include_charts and full_report.get("candlestick_png"):
            lines.append("<br><b>━━━ 周K NAV 蜡烛图 ━━━</b>")
            lines.append(
                '<img src="cid:candlestick" style="max-width:100%;'
                'border:1px solid #444;border-radius:4px;margin:8px 0">'
            )
        elif ohlc:
            # 飞书/无图时：贴周K收益摘要
            labels = ohlc.get("labels", [])
            closes = ohlc.get("close", [])
            if labels and closes:
                n = len(labels)
                lines.append("<br><b>━━━ 周K收益 ━━━</b>")
                summary_parts = []
                for k in range(max(0, n - 8), n):
                    summary_parts.append(f"{labels[k]}: {closes[k]:.0f}")
                lines.append("  " + " | ".join(summary_parts))

    return "<br>".join(lines)


def _build_optimizer_run_summary(report) -> str:
    """Render a channel-neutral optimizer summary covering all three markets."""
    labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
    status_labels = {
        "completed": "完成",
        "no_symbols": "无可搜参标的",
        "no_data": "无可用数据",
        "no_candidates": "未找到有效候选",
        "failed": "\u6267\u884c\u5931\u8d25",
        "interrupted": "\u5f02\u5e38\u4e2d\u6b62",
        "not_run": "未执行",
    }
    lines = [f"<b>{optimizer_notification_title(report)}</b>"]
    lines.append(
        "每个市场独立搜索、独立验收、独立激活；"
        "未激活市场不会读取其他市场策略。"
    )
    failure_reason = str(getattr(report, "failure_reason", "") or "")
    if failure_reason:
        lines.append(f"<b>\u5931\u8d25\u539f\u56e0:</b> {_html_escape(failure_reason)}")
    if report.elapsed_seconds > 0:
        lines.append(f"开始时间: {report.timestamp} | 耗时: {report.elapsed_seconds:.0f}s")
    else:
        lines.append(f"活动策略版本: {report.timestamp}（补发重建）")
    if report.activated:
        lines.append("<b>已发布:</b> 此运行已成为日报、简报和回测的当前告警策略。")
    elif getattr(report, "candidate", False):
        run_id = getattr(report, "run_id", "")
        completed_groups = [
            group
            for group, item in report.groups.items()
            if getattr(item, "status", "") == "completed"
        ]
        activation_scope = (
            f" --group {completed_groups[0]}"
            if len(completed_groups) == 1
            else ""
        )
        lines.append(
            "<b>候选已保存:</b> 当前活动策略未切换。"
            + (
                f"人工确认后运行 python main.py --activate-run {run_id}"
                f"{activation_scope}。"
                if run_id
                else "通过验收后可人工激活。"
            )
        )
    else:
        lines.append("<b>未切换:</b> 本次候选未激活；当前生产指针保持不变。")

    for group in ("a_share", "hk", "us"):
        item = report.groups.get(group)
        if item is None:
            lines.append(f"<br><b>{labels[group]}</b>: 未执行")
            continue
        status = status_labels.get(item.status, item.status)
        lines.append(f"<br><b>{labels[group]}</b>: {status}")
        item_strategy = getattr(item, "strategy_name", "")
        if item_strategy:
            lines.append(f"候选策略: <code>{item_strategy}</code>")
        solver_id = str(getattr(item, "solver_id", "") or "")
        gate_profile = str(getattr(item, "gate_profile", "") or "")
        config_hash = str(getattr(item, "market_config_hash", "") or "")
        item_run_id = str(
            getattr(item, "run_id", "")
            or (getattr(report, "run_ids_by_group", {}) or {}).get(group, "")
        )
        if solver_id:
            lines.append(f"Solver: <code>{solver_id}</code>")
        if gate_profile:
            lines.append(f"Gate: <code>{gate_profile}</code>")
        for field_name, label in (
            ("walk_forward_profile", "Walk-Forward"),
            ("execution_profile", "Execution"),
            ("benchmark_profile", "Benchmark"),
        ):
            profile = str(getattr(item, field_name, "") or "")
            if profile:
                lines.append(f"{label}: <code>{profile}</code>")
        if config_hash:
            lines.append(f"配置指纹: <code>{config_hash}</code>")
        if item_run_id:
            lines.append(f"独立 run_id: <code>{item_run_id}</code>")
        if item.status != "completed":
            evaluated = int(getattr(item, "evaluated_count", 0) or 0)
            if evaluated:
                prefix = "搜索已完成" if item.status == "no_candidates" else "中止前已完成"
                lines.append(f"{prefix} {evaluated:,} 次候选评估。")
            diagnostics = getattr(item, "ranking_diagnostics", {}) or {}
            error = diagnostics.get("error") or diagnostics.get("configuration_error")
            if error:
                lines.append(f"原因: {_html_escape(error)}")
            failures = diagnostics.get("hard_gate_failure_counts", {})
            if failures:
                lines.append("硬 Gate 淘汰次数（同一候选可触发多项）:")
                lines.extend(
                    f"{_html_escape(rule)}: {int(count):,}"
                    for rule, count in failures.items()
                )
            continue
        evaluated = getattr(item, "evaluated_count", 0)
        survivors = getattr(item, "survivor_count", 0) or item.candidate_count
        ranking_windows = getattr(item, "ranking_window_count", 0)
        validation_windows = getattr(item, "validation_window_count", 0)
        purged_windows = getattr(item, "purged_window_count", 0)
        if evaluated:
            lines.append(
                f"配置搜索评估 {evaluated:,} 次 | 最终入围 {survivors:,} 个 | "
                f"最优 WF 分数 {item.wf_score:+.3f}"
            )
        else:
            lines.append(
                f"最终入围 {survivors:,} 个 | 最优 WF 分数 {item.wf_score:+.3f}"
            )
        if ranking_windows:
            lines.append(
                f"WF 排名仅使用 {ranking_windows} 个历史窗口；"
                f"{validation_windows} 个日报验证窗口未参与排序、约束或敏感性。"
            )
        if purged_windows:
            lines.append(
                f"严格验证隔离额外剔除了 {purged_windows} 个与验证期重叠的 WF 窗口。"
            )
        ranking_diagnostics = getattr(item, "ranking_diagnostics", None) or {}
        if ranking_diagnostics:
            lines.append(
                "Ranking absolute return: "
                f"weighted {float(ranking_diagnostics.get('weighted_strategy_return', 0.0)):+.2f}% | "
                f"positive windows {int(ranking_diagnostics.get('positive_return_windows', 0))}/"
                f"{int(ranking_diagnostics.get('ranking_window_count', 0))}"
            )
        sensitivity = getattr(item, "sensitivity", None) or {}
        if sensitivity:
            lines.append(
                "历史窗口参数敏感性 "
                f"({sensitivity.get('sample_count', 0)} 版扰动): "
                f"基准 {sensitivity.get('base_score', 0.0):+.3f} | "
                f"最差 {sensitivity.get('worst_score', 0.0):+.3f} | "
                f"降幅 {sensitivity.get('drop', 0.0):+.3f}"
            )
        if sensitivity and "selection_score" in sensitivity:
            lines.append(
                "Robust selection score: "
                f"{float(sensitivity['selection_score']):+.3f}"
            )
        if item.params:
            params = ", ".join(f"{key}={value}" for key, value in item.params.items())
            lines.append(f"<code>{params}</code>")
        execution = getattr(item, "execution", None) or {}
        if execution:
            lines.append(
                "单笔现金上限: "
                f"买入 {float(execution.get('buy_cash_limit', 0)):.0f} / "
                f"卖出 {float(execution.get('sell_cash_limit', 0)):.0f} 元"
            )
        validation = getattr(item, "validation", None) or {}
        if validation:
            lines.append("<b>验证期回测</b>")
            lines.append(
                f"收益 {validation.get('total_return', 0.0):+.1f}% | "
                f"超额 {validation.get('excess_return', 0.0):+.1f}% | "
                f"回撤 {validation.get('max_drawdown', 0.0):.1f}% | "
                f"夏普 {validation.get('sharpe_ratio', 0.0):.2f} | "
                f"交易 {validation.get('trade_count', 0)} 笔 | "
                f"平均现金 {validation.get('avg_cash_pct', 0.0):.0f}%"
            )
            lines.append(
                "现金分红（税前 / 税额 / 税后）: "
                f"{validation.get('gross_dividend_cash', 0.0):,.2f} / "
                f"{validation.get('dividend_tax_cost', 0.0):,.2f} / "
                f"{validation.get('net_dividend_cash', 0.0):,.2f}"
            )
            benchmark_returns = validation.get("benchmark_returns", {}) or {}
            if benchmark_returns:
                baselines = " | ".join(
                    f"{_benchmark_label(code)} {value:+.1f}%"
                    for code, value in benchmark_returns.items()
                )
                lines.append(f"三基线收益 / 策略超额: {baselines}")
            win_rates = validation.get("benchmark_win_rates", {}) or {}
            if win_rates:
                wins = " | ".join(
                    f"{_benchmark_label(code)} {value:.1f}%"
                    for code, value in win_rates.items()
                )
                lines.append(f"验证期胜率: {wins}")
            positions = validation.get("final_holdings", []) or []
            if positions:
                holding_text = ", ".join(
                    f"{pos.get('code', '?')} {pos.get('shares', 0):.0f}股"
                    for pos in positions
                )
                lines.append(
                    f"期末持仓 (资产 {validation.get('final_asset', 0):,.0f}): "
                    f"{holding_text}"
                )
            else:
                lines.append("期末持仓: 空仓")
            weekly = validation.get("weekly_ohlc", {}) or {}
            weekly_labels = weekly.get("labels", [])
            closes = weekly.get("close", [])
            if weekly_labels and closes:
                start = max(0, len(weekly_labels) - 8)
                changes = " | ".join(
                    f"{weekly_labels[index]} {closes[index]:.0f}"
                    for index in range(start, min(len(weekly_labels), len(closes)))
                )
                lines.append(f"验证期 NAV 周线: {changes}")
    return "<br>".join(lines)
