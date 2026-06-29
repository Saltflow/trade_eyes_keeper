"""
Walk-Forward 窗口管理器

将历史数据按固定时间窗口切片，生成训练/测试期数据块。
供向量化评估器和遗传搜索器使用。

核心逻辑:
  - 窗口: 训练12月 + 测试9月, 滑动步长3月
  - 返回的每个窗口包含: train/test 的 (start_idx, end_idx)
  - 同时提供便捷方法构建每个窗口的 numpy 矩阵

用法:
    from src.analysis.walk_forward import WalkForwardManager
    manager = WalkForwardManager(stocks_data, indicators_data, wf_config)
    for window in manager.iter_windows():
        train_matrices = manager.build_matrices(window, phase="train")
        test_matrices = manager.build_matrices(window, phase="test")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class WindowSlice:
    """单个 Walk-Forward 窗口的训练/测试切片"""
    window_id: int

    # 日期范围（便于调试和理解）
    train_start_date: str
    train_end_date: str
    test_start_date: str
    test_end_date: str

    # 在统一日期数组中的索引范围（左闭右开）
    train_start_idx: int
    train_end_idx: int          # exclusive
    test_start_idx: int
    test_end_idx: int           # exclusive

    # 交易日计数
    train_days: int = 0
    test_days: int = 0

    # 测试期月数（用于计算月度交易密度）
    test_months: int = 9


# 指标列索引常量（与构建器解耦，纯 numpy 索引）
# 用途: 向量化信号生成时按索引读取矩阵切片
INDICATOR_NAMES = [
    "close", "ma60", "deviation",
    "rsi", "macd_hist", "boll_pct_b",
    "adx", "vol_ratio",
    "pct_from_ath", "ma60_slope", "ma200_dev",
]
INDICATOR_TO_IDX = {name: idx for idx, name in enumerate(INDICATOR_NAMES)}
NUM_INDICATORS = len(INDICATOR_NAMES)


class WalkForwardManager:
    """Walk-Forward 窗口管理器

    从原始股票数据构建统一的日期轴和指标矩阵，
    然后按窗口配置生成训练/测试切片。
    """

    def __init__(
        self,
        stocks_data: dict[str, pd.DataFrame],
        indicators_data: Optional[dict[str, pd.DataFrame]] = None,
        train_months: int = 12,
        test_months: int = 9,
        step_months: int = 3,
        num_windows: int = 6,
        min_trading_days: int = 200,
        benchmark_dfs: dict[str, pd.DataFrame] | None = None,
    ):
        """
        Args:
            stocks_data: {code: DataFrame with [date, open, high, low, close, volume]}
            indicators_data: {code: DataFrame with indicator columns} (预计算, 可选)
            train_months: 每个窗口训练期月数
            test_months: 每个窗口测试期月数
            step_months: 滑动步长月数
            num_windows: 窗口数量
            min_trading_days: 最少交易日数（数据不足则跳过该股票）
            benchmark_dfs: {bench_code: DataFrame} 基准 ETF 数据（可选）
        """
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months
        self.num_windows = num_windows
        self.min_trading_days = min_trading_days

        # 基准 ETF 数据（可选）
        self._benchmark_dfs: dict[str, pd.DataFrame] = benchmark_dfs or {}
        # 预对齐的基准 close 序列，按日期索引
        self._benchmark_aligned: dict[str, np.ndarray] = {}

        # ── 预处理: 构建统一的日期轴和指标矩阵 ──
        self.stock_codes: list[str] = []
        self._unified_dates: list[str] = []
        self._prepare_data(stocks_data, indicators_data)

        # ── 对齐基准 ETF 数据到统一日期轴 ──
        self._align_benchmarks()

    # ════════════════════════════════════════════════════════
    # 索引: 统一日期轴 + 指标矩阵
    # ════════════════════════════════════════════════════════

    @property
    def unified_dates(self) -> list[str]:
        """统一日期轴 (YYYY-MM-DD)"""
        return self._unified_dates

    @property
    def n_dates(self) -> int:
        """统一日期轴长度"""
        return len(self._unified_dates)

    @property
    def n_stocks(self) -> int:
        """股票数量"""
        return len(self.stock_codes)

    # ════════════════════════════════════════════════════════
    # 窗口生成
    # ════════════════════════════════════════════════════════

    def iter_windows(self) -> list[WindowSlice]:
        """生成所有 Walk-Forward 窗口切片

        Returns:
            WindowSlice 列表，按窗口 ID 排序
        """
        windows: list[WindowSlice] = []

        # 计算每个窗口的训练/测试日期范围
        for w in range(self.num_windows):
            # 起点偏移 = w * step_months
            offset_months = w * self.step_months

            train_start = self._index_at_month(offset_months)
            train_end   = self._index_at_month(offset_months + self.train_months)
            test_start  = train_end
            test_end    = self._index_at_month(offset_months + self.train_months + self.test_months)

            # 检查是否有足够数据
            if test_end > self.n_dates:
                logger.debug(
                    "窗口%d 数据不足: test_end=%d > n_dates=%d，跳过",
                    w, test_end, self.n_dates,
                )
                break

            # 日期标签
            dates = self._unified_dates
            ws = WindowSlice(
                window_id=w,
                train_start_date=dates[train_start] if 0 <= train_start < len(dates) else "?",
                train_end_date=dates[train_end - 1] if 0 <= train_end - 1 < len(dates) else "?",
                test_start_date=dates[test_start] if 0 <= test_start < len(dates) else "?",
                test_end_date=dates[test_end - 1] if 0 <= test_end - 1 < len(dates) else "?",
                train_start_idx=train_start,
                train_end_idx=train_end,
                test_start_idx=test_start,
                test_end_idx=test_end,
                train_days=train_end - train_start,
                test_days=test_end - test_start,
                test_months=self.test_months,
            )
            windows.append(ws)

        if len(windows) < self.num_windows:
            logger.info(
                "Walk-Forward: 实际生成 %d/%d 个窗口（数据不足）",
                len(windows), self.num_windows,
            )

        return windows

    # ════════════════════════════════════════════════════════
    # 基准 ETF 数据对齐
    # ════════════════════════════════════════════════════════

    def _align_benchmarks(self):
        """将基准 ETF 的收盘价对齐到统一日期轴。"""
        if not self._benchmark_dfs or not self._unified_dates:
            return
        date_to_idx = {d: i for i, d in enumerate(self._unified_dates)}
        for code, bdf in self._benchmark_dfs.items():
            if bdf is None or bdf.empty:
                continue
            bdf = bdf.copy()
            bdf["date_str"] = pd.to_datetime(bdf["date"]).dt.strftime("%Y-%m-%d")
            bdf = bdf.set_index("date_str")
            aligned = np.full(len(self._unified_dates), np.nan, dtype=np.float64)
            for i, d in enumerate(self._unified_dates):
                if d in bdf.index:
                    aligned[i] = float(bdf.loc[d, "close"])
            # forward-fill gaps (for missing dates within the data range)
            last_valid = np.nan
            for i in range(len(aligned)):
                if not np.isnan(aligned[i]):
                    last_valid = aligned[i]
                elif not np.isnan(last_valid):
                    aligned[i] = last_valid
            # Do NOT backward-fill leading NaN — benchmark is simply unavailable
            # for windows that start before its data range.
            self._benchmark_aligned[code] = aligned

    def get_benchmark_price(
        self, bench_code: str, window: "WindowSlice", phase: str = "test",
    ) -> np.ndarray | None:
        """获取基准 ETF 在指定窗口的收盘价序列。

        Args:
            bench_code: 基准代码（如 "510300"）
            window: Walk-Forward 窗口切片
            phase: "train" / "test" / "all"

        Returns:
            (T,) float64 数组，若数据不可用返回 None
        """
        if bench_code not in self._benchmark_aligned:
            return None
        full = self._benchmark_aligned[bench_code]
        if phase == "train":
            start, end = window.train_start_idx, window.train_end_idx
        elif phase == "test":
            start, end = window.test_start_idx, window.test_end_idx
        else:
            start, end = 0, len(full)
        if start < 0 or end > len(full) or end <= start:
            return None
        return full[start:end].copy()

    # ════════════════════════════════════════════════════════
    # numpy 矩阵构建
    # ════════════════════════════════════════════════════════

    def build_matrices(self, window: WindowSlice, phase: str = "all") -> np.ndarray:
        """构建指定窗口/阶段的指标矩阵

        Args:
            window: Walk-Forward 窗口
            phase: "train", "test", 或 "all"

        Returns:
            (n_dates, n_stocks, n_indicators) float32 numpy 数组
        """
        if phase == "train":
            start, end = window.train_start_idx, window.train_end_idx
        elif phase == "test":
            start, end = window.test_start_idx, window.test_end_idx
        else:  # all
            start, end = 0, self.n_dates

        return self._indicator_matrix[start:end].copy()

    def get_price_matrix(self, window: WindowSlice, phase: str = "all") -> np.ndarray:
        """获取收盘价矩阵 (n_dates, n_stocks)"""
        if phase == "train":
            start, end = window.train_start_idx, window.train_end_idx
        elif phase == "test":
            start, end = window.test_start_idx, window.test_end_idx
        else:
            start, end = 0, self.n_dates
        return self._price_matrix[start:end].copy()

    # ════════════════════════════════════════════════════════
    # 内部: 数据准备
    # ════════════════════════════════════════════════════════

    def _prepare_data(
        self,
        stocks_data: dict[str, pd.DataFrame],
        indicators_data: Optional[dict[str, pd.DataFrame]],
    ):
        """从原始/预计算数据构建统一矩阵

        步骤:
          1. 遍历所有股票，收集日期
          2. 合并为统一日期轴
          3. 按统一日期轴对齐各股票的指标值
          4. 缺失值填充为 np.nan
        """
        # Step 1: 收集所有日期，找最大范围
        all_dates_set: set[str] = set()
        for code, df in stocks_data.items():
            if df is None or df.empty:
                continue
            df_dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            all_dates_set.update(df_dates.tolist())

        if not all_dates_set:
            logger.warning("没有可用数据，WalkForwardManager 为空")
            return

        # 排序统一日期轴
        self._unified_dates = sorted(all_dates_set)
        date_to_idx = {d: i for i, d in enumerate(self._unified_dates)}

        n_dates = len(self._unified_dates)

        # Step 2: 遍历股票，填充矩阵
        stock_list: list[str] = []
        matrices: list[np.ndarray] = []

        for code, df in stocks_data.items():
            if df is None or df.empty:
                continue

            df = df.copy()
            df["date_str"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

            # 构建该股票的指标矩阵
            stock_mat = self._build_stock_matrix(df, code, indicators_data, date_to_idx, n_dates)
            if stock_mat is None:
                continue

            stock_list.append(code)
            matrices.append(stock_mat)

        if not stock_list:
            logger.warning("所有股票数据都不足，跳过")
            return

        self.stock_codes = stock_list

        # 堆叠为 (n_dates, n_stocks, n_indicators)
        self._indicator_matrix = np.stack(matrices, axis=1).astype(np.float32)

        # 提取价格矩阵 (第0列 = close)
        self._price_matrix = self._indicator_matrix[:, :, 0].copy()

        logger.info(
            "WalkForwardManager 就绪: %d 天, %d 只股票, %d 个指标",
            n_dates, len(stock_list), NUM_INDICATORS,
        )

        # 检查天数是否足够
        total_months_needed = self.train_months + self.test_months + \
            (self.num_windows - 1) * self.step_months
        total_days_needed = total_months_needed * 21  # 估算每月21个交易日
        if n_dates < total_days_needed:
            logger.warning(
                "数据仅 %d 天，但 Walk-Forward 需要约 %d 天 (%d 个月)",
                n_dates, total_days_needed, total_months_needed,
            )

    def _build_stock_matrix(
        self,
        df: pd.DataFrame,
        code: str,
        indicators_data: Optional[dict[str, pd.DataFrame]],
        date_to_idx: dict[str, int],
        n_dates: int,
    ) -> Optional[np.ndarray]:
        """为单只股票构建 (n_dates, n_indicators) 指标矩阵"""
        # 确保有必要的列
        required_cols = {"close", "high", "low", "volume"}
        missing = required_cols - set(c.lower() for c in df.columns)
        if missing:
            logger.debug("股票 %s 缺少列: %s", code, missing)
            return None

        # 价格基础指标: 确保列名统一为小写
        df_cols = {c.lower(): c for c in df.columns}
        close_col = df_cols["close"]

        # 合并预计算指标
        if indicators_data and code in indicators_data:
            ind_df = indicators_data[code].copy()
            ind_df["date_str"] = pd.to_datetime(ind_df["date"]).dt.strftime("%Y-%m-%d")
            # 合并
            df = df.merge(ind_df, on="date_str", how="left", suffixes=("", "_ind"))

        # 兜底计算 MA60 和 deviation
        close_series = df[close_col].astype(float)
        df["_ma60"] = close_series.rolling(window=60, min_periods=1).mean()
        df["_deviation"] = (close_series - df["_ma60"]) / df["_ma60"].replace(0, np.nan)

        # 兜底计算缺失指标
        if "_rsi" not in df.columns:
            df["_rsi"] = self._compute_rsi(close_series)
        if "_boll_pct_b" not in df.columns:
            df["_boll_pct_b"] = self._compute_bollinger(close_series)
        if "_vol_ratio" not in df.columns:
            vol_col = df_cols.get("volume", "volume")
            df["_vol_ratio"] = self._compute_vol_ratio(df[vol_col].astype(float))

        # 兜底计算新增指标
        # pct_from_ath: 距2年高点的距离
        if "_pct_from_ath" not in df.columns:
            ath = close_series.rolling(window=504, min_periods=1).max()
            df["_pct_from_ath"] = close_series / ath.replace(0, np.nan) - 1.0

        # ma60_slope: MA60 20日涨跌
        if "_ma60_slope" not in df.columns:
            ma60_s = df["_ma60"]
            df["_ma60_slope"] = ma60_s / ma60_s.shift(20).replace(0, np.nan) - 1.0

        # ma200_dev: 200日均线偏离
        if "_ma200_dev" not in df.columns:
            ma200 = close_series.rolling(window=200, min_periods=1).mean()
            df["_ma200_dev"] = (close_series - ma200) / ma200.replace(0, np.nan)

        # 兜底填充缺失列为 NaN
        matrix = np.full((n_dates, NUM_INDICATORS), np.nan, dtype=np.float32)

        for i, row in df.iterrows():
            d = row.get("date_str", str(row.get("date", "")))
            if d not in date_to_idx:
                continue
            idx = date_to_idx[d]

            # 按 INDICATOR_NAMES 顺序填充
            matrix[idx, 0] = row.get("close", np.nan)
            matrix[idx, 1] = row.get("_ma60", np.nan)
            matrix[idx, 2] = row.get("_deviation", np.nan)
            # 预计算指标：优先取传入的，再取兜底计算的
            for j, name in enumerate(INDICATOR_NAMES[3:8], 3):
                col_name = f"_{name}" if not name.startswith("_") and name in df.columns else name
                val = row.get(col_name, row.get(name, np.nan))
                matrix[idx, j] = val if not (isinstance(val, float) and pd.isna(val)) else np.nan
            # 新增指标（索引 8-10）
            for j, bare_name in enumerate(["pct_from_ath", "ma60_slope", "ma200_dev"], 8):
                val = row.get(f"_{bare_name}", row.get(bare_name, np.nan))
                matrix[idx, j] = val if not (isinstance(val, float) and pd.isna(val)) else np.nan

        # 检查数据充足性
        valid_days = (~np.isnan(matrix[:, 0])).sum()
        if valid_days < self.min_trading_days:
            logger.info("股票 %s 仅 %d 天数据(<%d)，跳过", code, valid_days, self.min_trading_days)
            return None

        return matrix

    @staticmethod
    def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
        """兜底 RSI 计算"""
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100.0 - 100.0 / (1.0 + rs)

    @staticmethod
    def _compute_bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.Series:
        """兜底布林带 %B 计算"""
        ma = close.rolling(window=window, min_periods=1).mean()
        std = close.rolling(window=window, min_periods=1).std()
        upper = ma + num_std * std
        lower = ma - num_std * std
        bandwidth = upper - lower
        pct_b = (close - lower) / bandwidth.replace(0, np.nan)
        return pct_b.clip(0, 1)

    @staticmethod
    def _compute_vol_ratio(volume: pd.Series, window: int = 20) -> pd.Series:
        """兜底量比计算"""
        vol_ma = volume.rolling(window=window, min_periods=1).mean()
        ratio = volume / vol_ma.replace(0, np.nan)
        return ratio

    # ════════════════════════════════════════════════════════
    # 内部: 月索引计算
    # ════════════════════════════════════════════════════════

    def _index_at_month(self, months_from_start: float) -> int:
        """根据起始 offset (月数) 估计统一日期轴中的索引

        简单按比例映射（month → day index）:
          total_days ≈ total_months * 21 (每月约21交易日)

        如需精确对齐，建议使用日期字符串映射。
        """
        # 估算日均行驶月数
        total_days = self.n_dates
        if total_days <= 0:
            return 0

        # 第一个日期对应的"起始月"为 0，末尾为 total_months
        # 简单线性插值
        total_months_estimate = total_days / 21.0
        if total_months_estimate <= 0:
            return 0

        idx = int(months_from_start / total_months_estimate * total_days)
        return min(max(idx, 0), total_days)


def create_walk_forward_manager(
    stocks_data: dict[str, pd.DataFrame],
    indicators_data: Optional[dict[str, pd.DataFrame]] = None,
    train_months: int = 12,
    test_months: int = 9,
    step_months: int = 3,
    num_windows: int = 6,
) -> WalkForwardManager:
    """便捷构造函数"""
    return WalkForwardManager(
        stocks_data=stocks_data,
        indicators_data=indicators_data,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
        num_windows=num_windows,
    )
