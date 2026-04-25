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

    # 设置中文字体（跨平台字体回退链，matplotlib 3.6+ 支持逐字形回退）
    # DejaVu Sans 放首位：包含完整拉丁字母和数字字形（解决数字方框）
    # Droid Sans Fallback / 其他 CJK 字体放后面：提供中文支持
    # Windows: Microsoft YaHei / SimHei
    # Linux:   Droid Sans Fallback / WenQuanYi Micro Hei / Noto Sans CJK SC
    # macOS:   PingFang SC / Heiti SC
    plt.rcParams["font.sans-serif"] = [
        "DejaVu Sans",  # 首位：完整拉丁/数字字形（服务器上可用）
        "Microsoft YaHei",  # Windows 微软雅黑
        "SimHei",  # Windows 黑体
        "Droid Sans Fallback",  # Ubuntu Droid（已确认在服务器上存在，有CJK）
        "WenQuanYi Micro Hei",  # Ubuntu 文泉驿
        "Noto Sans CJK SC",  # Google Noto CJK
        "Noto Sans SC",  # Google Noto SC 变体名
        "PingFang SC",  # macOS 苹方
        "Heiti SC",  # macOS 黑体-简
    ]
    plt.rcParams["axes.unicode_minus"] = False

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
        low_values = df["low"].to_numpy()

        # 确定"最长"告警锚点
        anchor_names = stock_anchors[stock_code]
        longest = _pick_longest_anchor(anchor_names)

        # 股票名称
        stock_name = _get_stock_name(stock_code, stock_data)

        # 标题：代码 + 名称
        ax.set_title(f"{stock_code}\n{stock_name}", fontsize=8, fontweight="bold")

        # 画最低价线（实线）
        ax.plot(
            dates,
            low_values,
            color="#1f77b4",
            linestyle="-",
            linewidth=1.2,
            alpha=0.9,
            label="最低价",
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
