"""
图表生成模块
为邮件生成股票价格走势图（base64 PNG，内嵌到HTML）
"""

import logging
import re
from typing import Optional, Dict, List
from io import BytesIO
import base64

logger = logging.getLogger(__name__)

# ── 服务端绘图后端 ──
try:
    import matplotlib

    matplotlib.use("Agg")  # 非交互后端，服务器渲染
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.ticker import MaxNLocator

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


def generate_combined_chart(
    historical_data: Dict[str, "pd.DataFrame"],
    alerts: List[dict],
    stock_data: "pd.DataFrame",
    trading_days: int = 60,
) -> Optional[str]:
    """
    生成所有告警股票的合并走势图，返回 base64 PNG 字符串

    Args:
        historical_data: stock_code → 完整历史 DataFrame（含 date, low, wma* 等列）
        alerts: 告警列表（每项含 stock_code, anchor_name 等）
        stock_data: 最新行情 DataFrame（1行/股，含 stock_name）
        trading_days: 显示近多少交易日（默认60 ≈ 2个月）

    Returns:
        base64 编码的 PNG 图片字符串，失败时返回 None
    """
    if not MATPLOTLIB_AVAILABLE:
        logger.warning("matplotlib 不可用，跳过图表生成")
        return None

    if not alerts or not historical_data:
        logger.info("无告警数据或历史数据，跳过图表生成")
        return None

    import pandas as pd

    # ── 收集每只告警股票要画的数据 ──
    # 按 stock_code 聚合告警锚点
    stock_anchors: Dict[str, List[str]] = {}
    for alert in alerts:
        code = alert.get("stock_code", "")
        anchor = alert.get("anchor_name", "")
        if code and anchor:
            stock_anchors.setdefault(code, []).append(anchor)

    if not stock_anchors:
        logger.info("告警数据无有效锚点，跳过图表生成")
        return None

    # ── 配色方案 ──
    colors = [
        "#1f77b4",
        "#ff7f0e",
        "#2ca02c",
        "#d62728",
        "#9467bd",
        "#8c564b",
        "#e377c2",
        "#7f7f7f",
        "#bcbd22",
        "#17becf",
    ]
    linestyles = ["-", "--", "-.", ":"]

    # ── 创建图表 ──
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.set_title(
        "价格走势与告警锚点（近2个月）",
        fontsize=13,
        fontweight="bold",
        pad=12,
    )
    ax.set_xlabel("日期", fontsize=10)
    ax.set_ylabel("价格", fontsize=10)
    ax.grid(True, alpha=0.3, linestyle="--")

    plotted = 0
    color_idx = 0

    for stock_code, anchor_names in stock_anchors.items():
        # 从 historical_data 取完整 DataFrame
        full_df = historical_data.get(stock_code)
        if full_df is None or full_df.empty:
            logger.debug(f"股票 {stock_code} 无历史数据，跳过图表绘制")
            continue

        # 取最近 trading_days 天
        df = full_df.tail(trading_days).copy()
        if df.empty:
            continue

        # 确保 date 列是 datetime
        if "date" in df.columns:
            dates = pd.to_datetime(df["date"]).to_numpy()
        else:
            logger.debug(f"股票 {stock_code} 缺少 date 列，跳过")
            continue

        low = df.get("low")
        if low is None or low.isna().all():
            logger.debug(f"股票 {stock_code} 无有效 low 数据，跳过")
            continue
        low_values = low.to_numpy()

        # 确定"最长"告警锚点
        longest = _pick_longest_anchor(anchor_names)
        anchor_col = longest  # 列名直接是锚点名，如 'wma50'

        # 从 stock_data 取股票名称
        stock_name = stock_code
        if stock_data is not None and not stock_data.empty:
            match = stock_data[stock_data["stock_code"] == stock_code]
            if not match.empty:
                stock_name = match.iloc[0].get("stock_name", stock_code)

        # 标签
        label_low = f"{stock_code}({stock_name}) 最低价"
        label_ma = f"{stock_code} {longest}"

        c = colors[color_idx % len(colors)]
        ls = linestyles[(color_idx // len(colors)) % len(linestyles)]
        color_idx += 1

        # 画最低价线（实线）
        ax.plot(
            dates,
            low_values,
            color=c,
            linestyle="-",
            linewidth=1.2,
            alpha=0.9,
            label=label_low,
            marker="",
            markersize=0,
        )

        # 画锚点MA曲线（虚线），如果列存在且有有效值
        if anchor_col and anchor_col in df.columns:
            anchor_values = df[anchor_col].to_numpy()
            if not pd.isna(anchor_values).all():
                ax.plot(
                    dates,
                    anchor_values,
                    color=c,
                    linestyle=ls,
                    linewidth=1.0,
                    alpha=0.7,
                    label=label_ma,
                )

        plotted += 1

    if plotted == 0:
        plt.close(fig)
        logger.info("没有可绘制的股票数据")
        return None

    # ── 格式化 X 轴日期 ──
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=30, ha="right")

    # ── 自动调整 Y 轴留白 ──
    ymin, ymax = ax.get_ylim()
    margin = (ymax - ymin) * 0.05
    ax.set_ylim(ymin - margin, ymax + margin)

    # ── Legend ──
    legend = ax.legend(
        loc="upper left",
        fontsize=8,
        framealpha=0.8,
        edgecolor="#ccc",
        ncol=2,
    )

    # ── 紧凑布局 ──
    fig.tight_layout()

    # ── 转 base64 PNG ──
    try:
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        buf.seek(0)
        b64_str = base64.b64encode(buf.read()).decode("utf-8")
        plt.close(fig)
        logger.info(f"图表生成成功: {plotted} 只股票")
        return b64_str
    except Exception as e:
        logger.error(f"图表生成失败: {e}")
        plt.close(fig)
        return None
