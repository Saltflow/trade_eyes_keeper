"""
图表生成模块
为邮件生成股票价格走势图（base64 PNG，内嵌到HTML）
使用 subplots 布局，每只告警股票独立子图
"""

import logging
import math
import re
from typing import Optional, Dict, List
from io import BytesIO
import base64

import pandas as pd

logger = logging.getLogger(__name__)

# ── 服务端绘图后端 ──
try:
    import matplotlib

    matplotlib.use("Agg")  # 非交互后端，服务器渲染
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.ticker import MaxNLocator

    # 设置中文字体（统一使用 portfolio_strategy 的平台感知检测）
    from ..utils.font_setup import setup_cjk_font
    setup_cjk_font()

    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    logger.warning("matplotlib 不可用，图表功能将被跳过")
    plt = None
    mdates = None
    MaxNLocator = None


# ── 内置锚点窗口大小映射（用于判断"最长"） ──
_ANCHOR_WINDOW_MAP: Dict[str, int] = {
    "wma50": 50,
    "wma30": 30,
    "wma20": 20,
    "ma60": 60,
    "ma20": 20,
}


def _extract_window(name: str) -> int:
    """从锚点名称提取窗口大小，如 'wma50' → 50"""
    match = re.search(r"(\d+)", name)
    if match:
        return int(match.group(1))
    return _ANCHOR_WINDOW_MAP.get(name, 0)


def _pick_longest_anchor(anchor_names: List[str]) -> str:
    """
    从一组告警锚点名中选出"最长"的那个（窗口最大）
    如 ['wma20', 'wma50'] → 'wma50'
    """
    if not anchor_names:
        return ""
    return max(anchor_names, key=lambda n: _extract_window(n))


def _get_stock_name(stock_code: str, stock_data: "pd.DataFrame") -> str:
    """从 stock_data DataFrame 中查找股票名称，找不到返回 stock_code"""
    if stock_data is not None and not stock_data.empty:
        match = stock_data[stock_data["stock_code"] == stock_code]
        if not match.empty:
            name = match.iloc[0].get("stock_name")
            if pd.notna(name) and str(name).strip():
                return str(name).strip()
    return stock_code


def generate_combined_chart(
    historical_data: Dict[str, "pd.DataFrame"],
    alerts: List[dict],
    stock_data: "pd.DataFrame",
    trading_days: int = 60,
) -> tuple[Optional[str], Optional[bytes]]:
    """
    生成所有告警股票的合并走势图（subplots 布局），返回 (base64 PNG, 原始 PNG bytes)

    Args:
        historical_data: stock_code → 完整历史 DataFrame（含 date, low, wma* 等列）
        alerts: 告警列表（每项含 stock_code, anchor_name 等）
        stock_data: 最新行情 DataFrame（1行/股，含 stock_name）
        trading_days: 显示近多少交易日（默认60 ≈ 2个月）

    Returns:
        (base64 编码的 PNG 字符串, 原始 PNG bytes)，失败时返回 (None, None)
    """
    if not MATPLOTLIB_AVAILABLE:
        logger.warning("matplotlib 不可用，跳过图表生成")
        return (None, None)

    if not alerts or not historical_data:
        logger.info("无告警数据或历史数据，跳过图表生成")
        return (None, None)

    # ── 收集每只告警股票要画的数据 ──
    stock_anchors: Dict[str, List[str]] = {}
    for alert in alerts:
        code = alert.get("stock_code", "")
        anchor = alert.get("anchor_name", "")
        if code and anchor:
            stock_anchors.setdefault(code, []).append(anchor)

    if not stock_anchors:
        logger.info("告警数据无有效锚点，跳过图表生成")
        return (None, None)

    # ── 筛选有有效历史数据的股票 ──
    valid_codes: List[str] = []
    for code in stock_anchors:
        full_df = historical_data.get(code)
        if full_df is not None and not full_df.empty:
            df = full_df.tail(trading_days).copy()
            if not df.empty and "date" in df.columns and "low" in df.columns:
                valid_codes.append(code)

    if not valid_codes:
        logger.info("无有效历史数据可绘制")
        return (None, None)

    # ── 确定 subplots 布局 ──
    n_stocks = len(valid_codes)
    cols = 4
    rows = math.ceil(n_stocks / cols)

    # 根据股票数量动态调整 figure 尺寸
    fig_height = max(3, rows * 2.2)
    fig_width = 12
    fig, axes = plt.subplots(
        nrows=rows,
        ncols=cols,
        figsize=(fig_width, fig_height),
        squeeze=False,  # 始终返回二维 ndarray
    )

    fig.suptitle(
        "告警股票价格走势与锚点（近2个月）",
        fontsize=13,
        fontweight="bold",
        y=0.98,
    )

    # ── 展平 axes 以便按顺序迭代 ──
    flat_axes = axes.flatten()

    # ── 逐只股票绘制 ──
    for idx, stock_code in enumerate(valid_codes):
        ax = flat_axes[idx]

        full_df = historical_data.get(stock_code)
        df = full_df.tail(trading_days).copy()

        dates = pd.to_datetime(df["date"]).to_numpy()
        close_values = df["close"].to_numpy()

        # 确定"最长"告警锚点
        anchor_names = stock_anchors[stock_code]
        longest = _pick_longest_anchor(anchor_names)

        # 股票名称
        stock_name = _get_stock_name(stock_code, stock_data)

        # 标题：代码 + 名称
        ax.set_title(f"{stock_code}\n{stock_name}", fontsize=8, fontweight="bold")

        # 画收盘价线（实线）
        ax.plot(
            dates,
            close_values,
            color="#1f77b4",
            linestyle="-",
            linewidth=1.2,
            alpha=0.9,
            label="收盘价",
        )

        # 画锚点 MA 曲线（虚线）
        if longest and longest in df.columns:
            anchor_values = df[longest].to_numpy()
            if not pd.isna(anchor_values).all():
                ax.plot(
                    dates,
                    anchor_values,
                    color="#ff7f0e",
                    linestyle="--",
                    linewidth=1.0,
                    alpha=0.7,
                    label=longest,
                )

        # 网格
        ax.grid(True, alpha=0.25, linestyle="--")

        # 格式化 X 轴（日期）
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(MaxNLocator(5))
        ax.tick_params(axis="x", labelsize=7)
        ax.tick_params(axis="y", labelsize=7)

        # Y 轴自动留白 5%
        ymin, ymax = ax.get_ylim()
        margin = (ymax - ymin) * 0.05
        ax.set_ylim(ymin - margin, ymax + margin)

        # Legend（小字体，紧凑）
        ax.legend(
            loc="upper left",
            fontsize=6,
            framealpha=0.7,
            edgecolor="#ccc",
            ncol=1,
        )

    # ── 隐藏多余的 subplot ──
    for j in range(idx + 1, len(flat_axes)):
        flat_axes[j].set_visible(False)

    # ── 自动调整布局 ──
    fig.tight_layout(rect=[0, 0, 1, 0.95])  # 为 suptitle 留空间

    # ── 转 PNG bytes & base64 ──
    try:
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
        buf.seek(0)
        png_bytes = buf.read()
        b64_str = base64.b64encode(png_bytes).decode("utf-8")
        plt.close(fig)
        logger.info(f"图表生成成功: {n_stocks} 只股票, {rows}x{cols} 布局")
        return (b64_str, png_bytes)
    except Exception as e:
        logger.error(f"图表生成失败: {e}")
        plt.close(fig)
        return (None, None)


def _build_weekly_ohlc(
    nav_series: list[float],
    nav_dates: list[str],
) -> dict | None:
    """从日线 NAV 构建周K OHLC 数据（供报告展示）。

    Args:
        nav_series: 日净值序列
        nav_dates: 对应日期字符串 (YYYY-MM-DD)

    Returns:
        {"labels": ["2026-W01",...], "open": [...], "high": [...],
         "low": [...], "close": [...]}，数据不足时返回 None
    """
    if len(nav_series) < 5:
        return None
    try:
        import pandas as pd
        df = pd.DataFrame({"date": pd.to_datetime(nav_dates), "nav": nav_series})
        df["week"] = df["date"].dt.isocalendar().apply(
            lambda r: f"{int(r.year)}-W{int(r.week):02d}", axis=1)
        grouped = df.groupby("week")["nav"]
        ohlc = {
            "labels": [],
            "open": [], "high": [], "low": [], "close": [],
        }
        for week, group in grouped:
            ohlc["labels"].append(week)
            ohlc["open"].append(round(float(group.iloc[0]), 2))
            ohlc["high"].append(round(float(group.max()), 2))
            ohlc["low"].append(round(float(group.min()), 2))
            ohlc["close"].append(round(float(group.iloc[-1]), 2))
        if len(ohlc["labels"]) < 3:
            return None
        return ohlc
    except Exception as e:
        logger.warning(f"周K OHLC 构建失败: {e}")
        return None


def generate_candlestick_chart(weekly_ohlc: dict) -> tuple[str, bytes] | None:
    """从周K OHLC 数据生成蜡烛图 PNG。

    Returns:
        (base64_data_uri, raw_png_bytes) 或 None
    """
    import base64
    import io

    import numpy as np

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception:
        logger.warning("matplotlib 不可用，跳过蜡烛图")
        return None

    labels = weekly_ohlc.get("labels", [])
    opens = weekly_ohlc.get("open", [])
    highs = weekly_ohlc.get("high", [])
    lows = weekly_ohlc.get("low", [])
    closes = weekly_ohlc.get("close", [])
    if len(labels) < 3:
        return None

    n = len(labels)
    x = np.arange(n)
    width = 0.6

    fig, ax = plt.subplots(figsize=(10, 4))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    body_colors = [
        "#27ae60" if closes[i] >= opens[i] else "#c0392b"
        for i in range(n)
    ]
    for i in range(n):
        # 影线
        ax.plot([x[i], x[i]], [lows[i], highs[i]], color=body_colors[i], linewidth=1)
        # 实体
        body_lo = min(opens[i], closes[i])
        body_hi = max(opens[i], closes[i])
        body_h = body_hi - body_lo
        if body_h < 1e-6:
            body_h = max(0.001 * body_hi, 0.01)
        rect = Rectangle((x[i] - width / 2, body_lo), width, body_h,
                         facecolor=body_colors[i], edgecolor=body_colors[i],
                         linewidth=0.5)
        ax.add_patch(rect)

    ax.set_xticks(x[::max(1, n // 10)])
    ax.set_xticklabels([labels[i] for i in range(0, n, max(1, n // 10))],
                       rotation=45, fontsize=8, color="#cccccc")
    ax.tick_params(colors="#cccccc", labelsize=8)
    ax.set_ylabel("NAV", color="#cccccc")
    ax.set_title("Weekly NAV Candlestick", color="#ffffff", fontsize=12)
    ax.grid(axis="y", alpha=0.2, color="#555555")

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("utf-8")
    return (f"data:image/png;base64,{b64}", buf.getvalue())
