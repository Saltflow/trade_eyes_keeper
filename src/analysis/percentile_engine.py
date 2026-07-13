"""PercentileSignalFn（§8 新参数化）。

每只标的独立评估自身历史分位，加权求和打分，分数够高则触发买卖。
松弛 H1-H3：标的自比较分位、历史分布窗口、参数空间缩小 3 个数量级。
"""
from __future__ import annotations

import random as _random
import numpy as np
from typing import TYPE_CHECKING

from .signal_functions import SignalFn, ParamDim, ParamSpace, Params
from .fast_evaluator import IDX_ADX_PCT, IDX_RSI_PCT, IDX_DEVIATION_PCT, \
    IDX_VOL_RATIO_PCT, IDX_MA200_DEV_PCT

if TYPE_CHECKING:
    from .genetic_searcher import StrategyEncoding
    from .optimizer_constraints import DiscreteSearchConfig

# 分位列索引
PERCENTILE_COLUMNS = [
    IDX_ADX_PCT, IDX_RSI_PCT, IDX_DEVIATION_PCT,
    IDX_VOL_RATIO_PCT, IDX_MA200_DEV_PCT,
]
PERCENTILE_LABELS = [
    "adx_pct", "rsi_pct", "deviation_pct",
    "vol_ratio_pct", "ma200_dev_pct",
]
# 分位信号 → 原始指标源列名（scan_signals 计算滚动分位用）
PERCENTILE_SOURCES = {
    "adx_pct": "adx",
    "rsi_pct": "rsi",
    "deviation_pct": "deviation",
    "vol_ratio_pct": "vol_ratio",
    "ma200_dev_pct": "ma200_dev",
}
# 信号的人类可读名称
PERCENTILE_HUMAN = {
    "adx_pct": "趋势强度分位(ADX)",
    "rsi_pct": "超买超卖分位(RSI)",
    "deviation_pct": "均线偏离分位",
    "vol_ratio_pct": "量能分位",
    "ma200_dev_pct": "长期趋势分位(MA200)",
}
PCT_WINDOW = 252  # 与 walk_forward 分位窗口一致

N_SIGNALS = len(PERCENTILE_COLUMNS)  # 5
TAU_LEVELS = 10
W_LEVELS = 5  # [0.1, 0.3, 0.5, 0.7, 0.9]


def _decode_tau(level: int) -> float:
    return 0.1 + (level / max(TAU_LEVELS - 1, 1)) * 0.8


def _decode_w(level: int) -> float:
    ws = [0.1, 0.3, 0.5, 0.7, 0.9]
    return ws[min(level, len(ws) - 1)]


_POS_FRACS = [0.05, 0.15, 0.25, 0.35, 0.45]


def _decode_pos_frac(level: int) -> float:
    return _POS_FRACS[min(int(level), len(_POS_FRACS) - 1)]


class PercentileSignalFn(SignalFn):
    """分位评分引擎 — 新参数化, 松弛 H1/H2/H3。

    参数空间: 5 个分位信号 × (τ, w) + τ_buy + τ_sell + pos_frac。
    信号输出: 评分矩阵由各加权分位数计算, 每个标的用自己的历史分位。
    """

    def __init__(self):
        dims = []
        for lbl in PERCENTILE_LABELS:
            dims.append(ParamDim(f"{lbl}_tau", TAU_LEVELS, 0.1, 0.9))
            dims.append(ParamDim(f"{lbl}_w", W_LEVELS, 0.1, 0.9))
        dims.append(ParamDim("buy_score_thresh", TAU_LEVELS, 0.1, 0.9))
        dims.append(ParamDim("sell_score_thresh", TAU_LEVELS, 0.1, 0.9))
        dims.append(ParamDim("position_frac", 5, 0.05, 0.45))
        self._space = ParamSpace(dims)

    @property
    def name(self) -> str:
        return "percentile"

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def evaluate(
        self, params: Params, indicator_matrix: np.ndarray,
    ) -> np.ndarray:
        T, N, K = indicator_matrix.shape
        buy_scores = np.zeros((T, N), dtype=np.float32)
        sell_scores = np.zeros((T, N), dtype=np.float32)
        total_w = 0.0

        for ci, col in enumerate(PERCENTILE_COLUMNS):
            lbl = PERCENTILE_LABELS[ci]
            tau = _decode_tau(params.values.get(f"{lbl}_tau", 5))
            w = _decode_w(params.values.get(f"{lbl}_w", 2))
            if w <= 0 or col >= K:
                continue
            col_data = indicator_matrix[:, :, col]
            valid = ~np.isnan(col_data)
            above = (valid & (col_data > tau)).astype(np.float32)
            below = (valid & (col_data < tau)).astype(np.float32)
            buy_scores += w * above
            sell_scores += w * below
            total_w += w

        if total_w > 0:
            buy_scores /= total_w
            sell_scores /= total_w

        return np.stack([buy_scores, sell_scores], axis=-1).astype(np.float32)

    def to_human_readable(self, params) -> str:
        vals = getattr(params, "values", params) if not isinstance(params, dict) else params
        lines = ["分位评分策略 (PercentileSignalFn)"]
        for ci, lbl in enumerate(PERCENTILE_LABELS):
            tau = _decode_tau(vals.get(f"{lbl}_tau", 5))
            w = _decode_w(vals.get(f"{lbl}_w", 2))
            lines.append(f"  {PERCENTILE_HUMAN[lbl]}: tau={tau:.2f}, w={w:.2f}")
        buy_th = _decode_tau(vals.get("buy_score_thresh", 5))
        sell_th = _decode_tau(vals.get("sell_score_thresh", 5))
        pos_frac = _decode_pos_frac(vals.get("position_frac", 2))
        lines.append(f"  买入阈值 τ_buy={buy_th:.2f}  卖出阈值 τ_sell={sell_th:.2f}")
        lines.append(f"  仓位比例 frac={pos_frac:.2f}")
        return "\n".join(lines)

    # ── 信号扫描（显示层用；每只标的算自身滚动分位）──

    def _rolling_percentile(self, series, window: int = PCT_WINDOW) -> float | None:
        """最新值在过去 window 天内的分位排名 (0-1)。"""
        import numpy as _np
        vals = _np.asarray(series, dtype=float)
        vals = vals[~_np.isnan(vals)]
        if len(vals) < 20:
            return None
        win = vals[-window:]
        cur = win[-1]
        return float((win <= cur).sum()) / max(len(win), 1)

    @staticmethod
    def _rolling_rank_series(arr, window: int = PCT_WINDOW):
        """对整条序列算滚动分位排名 (T,)：每个 t 用过去 window 天。

        与 walk_forward 分位口径一致：pct[t] = (#[t-win+1..t] <= v[t]) / win。
        """
        import numpy as _np
        a = _np.asarray(arr, dtype=float)
        T = len(a)
        out = _np.full(T, _np.nan, dtype=_np.float32)
        for t in range(T):
            lo = max(0, t - window + 1)
            w = a[lo:t + 1]
            valid = w[~_np.isnan(w)]
            if len(valid) < 20 or _np.isnan(a[t]):
                continue
            out[t] = float((valid <= a[t]).sum()) / max(len(valid), 1)
        return out

    def score_timeseries(self, params, hist_df):
        """整段历史的每日净买/卖评分 (T,)，供日报组合回测用。

        Returns: (net, -net) 各 (T,) float，已按权重归一。
        净分 = 买分 - 卖分（[-1,1]），使买卖天然互斥。
        """
        import numpy as _np
        vals = getattr(params, "values", params) if not isinstance(params, dict) else params
        df = self._ensure_source_columns(hist_df)
        if df is None or df.empty:
            return _np.zeros(0), _np.zeros(0)
        T = len(df)
        buy = _np.zeros(T, dtype=_np.float64)
        sell = _np.zeros(T, dtype=_np.float64)
        total_w = 0.0
        for lbl in PERCENTILE_LABELS:
            w = _decode_w(vals.get(f"{lbl}_w", 2))
            tau = _decode_tau(vals.get(f"{lbl}_tau", 5))
            if w <= 0:
                continue
            src = PERCENTILE_SOURCES[lbl]
            if src not in df.columns:
                continue
            total_w += w
            pct = self._rolling_rank_series(df[src].values)
            above = _np.nan_to_num((pct > tau).astype(_np.float64))
            below = _np.nan_to_num((pct < tau).astype(_np.float64))
            buy += w * above
            sell += w * below
        if total_w > 0:
            buy /= total_w
            sell /= total_w
        net = buy - sell  # [-1, 1]
        return net, -net

    def scan_signals(self, params, today: dict, history=None) -> list[dict]:
        """用分位评分逻辑判断今日买/卖信号。

        对每个分位信号：算标的自身该指标的滚动分位排名，
        分位 > tau 计入买入加权分，分位 < tau 计入卖出加权分；
        加权归一后与 τ_buy / τ_sell 比较。
        """
        vals = getattr(params, "values", params) if not isinstance(params, dict) else params
        import pandas as _pd

        hist = self._ensure_source_columns(history)
        if hist is None:
            return []

        buy_score = 0.0
        sell_score = 0.0
        total_w = 0.0
        buy_hits: list[str] = []
        sell_hits: list[str] = []

        for lbl in PERCENTILE_LABELS:
            w = _decode_w(vals.get(f"{lbl}_w", 2))
            tau = _decode_tau(vals.get(f"{lbl}_tau", 5))
            if w <= 0:
                continue
            total_w += w
            src_col = PERCENTILE_SOURCES[lbl]

            pct = None
            if src_col in hist.columns:
                pct = self._rolling_percentile(hist[src_col].values)
            if pct is None:
                continue

            human = PERCENTILE_HUMAN[lbl]
            if pct > tau:
                buy_score += w
                buy_hits.append(f"{human}分位{pct:.0%}>{tau:.0%}")
            elif pct < tau:
                sell_score += w
                sell_hits.append(f"{human}分位{pct:.0%}<{tau:.0%}")

        if total_w > 0:
            buy_score /= total_w
            sell_score /= total_w

        buy_th = _decode_tau(vals.get("buy_score_thresh", 5))
        sell_th = _decode_tau(vals.get("sell_score_thresh", 5))
        net = buy_score - sell_score  # [-1, 1]，买卖天然互斥

        out: list[dict] = []
        if net > buy_th:
            out.append({
                "side": "buy",
                "label": f"分位评分买入 (net {net:.2f}>{buy_th:.2f})",
                "detail": " | ".join(buy_hits[:3]) if buy_hits else "强看涨信号",
            })
        elif net < -sell_th:
            out.append({
                "side": "sell",
                "label": f"分位评分卖出 (net {net:.2f}<-{sell_th:.2f})",
                "detail": " | ".join(sell_hits[:3]) if sell_hits else "强看跌信号",
            })
        return out

    @staticmethod
    def _ensure_source_columns(history):
        """确保 history DataFrame 含 scan/score 所需全部 5 个源列。

        扫描器/日报可能传原始 OHLCV 数据，缺少 adx/rsi/vol_ratio；
        这里兜底计算所有分位源指标。
        """
        import pandas as _pd
        if history is None or not isinstance(history, _pd.DataFrame) or history.empty:
            return None
        if "close" not in history.columns:
            return history
        need = {"adx", "rsi", "vol_ratio", "deviation", "ma200_dev"}
        if need.issubset(history.columns):
            return history
        df = history.copy()
        close = df["close"].astype(float)
        # deviation
        if "deviation" not in df.columns:
            ma60 = close.rolling(60, min_periods=1).mean()
            df["deviation"] = (close - ma60) / ma60.replace(0, float("nan"))
        # ma200_dev
        if "ma200_dev" not in df.columns:
            ma200 = close.rolling(200, min_periods=1).mean()
            df["ma200_dev"] = (close - ma200) / ma200.replace(0, float("nan"))
        # rsi (14-period Wilder)
        if "rsi" not in df.columns:
            import numpy as _np
            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = (-delta).clip(lower=0)
            avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
            rs = avg_gain / avg_loss.replace(0, _np.nan)
            df["rsi"] = 100.0 - 100.0 / (1.0 + rs)
        # adx (14-period)
        if "adx" not in df.columns:
            import numpy as _np
            high = df.get("high", close).astype(float)
            low = df.get("low", close).astype(float)
            prev_close = close.shift(1)
            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            tr = _np.maximum(_np.maximum(tr1, tr2), tr3)
            up = high.diff()
            down = (-low).diff()
            atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
            plus_di = 100 * (up.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean() / atr.replace(0, _np.nan))
            minus_di = 100 * (down.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean() / atr.replace(0, _np.nan))
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, _np.nan)
            adx_series = _pd.Series(dx, index=df.index)
            def _wilder_smooth(s, period):
                result = s.copy()
                for i in range(period, len(result)):
                    result.iloc[i] = (result.iloc[i - 1] * (period - 1) + result.iloc[i]) / period
                return result
            df["adx"] = _wilder_smooth(adx_series, 14)
        # vol_ratio (20-day)
        if "vol_ratio" not in df.columns:
            vol = df.get("volume", _pd.Series(1, index=df.index)).astype(float)
            vol_ma = vol.rolling(20, min_periods=1).mean()
            df["vol_ratio"] = vol / vol_ma.replace(0, float("nan"))
        return df

    def describe_rules(self, params) -> dict:
        """把分位参数翻译成买卖规则名称（带权重的信号即为激活规则）。"""
        vals = getattr(params, "values", params) if not isinstance(params, dict) else params
        buy_th = _decode_tau(vals.get("buy_score_thresh", 5))
        sell_th = _decode_tau(vals.get("sell_score_thresh", 5))
        active = []
        for lbl in PERCENTILE_LABELS:
            w = _decode_w(vals.get(f"{lbl}_w", 2))
            tau = _decode_tau(vals.get(f"{lbl}_tau", 5))
            if w > 0:
                active.append(f"{PERCENTILE_HUMAN[lbl]}(τ={tau:.2f} w={w:.1f})")
        return {
            "buy": [f"加权分位≥{buy_th:.2f}买入"] + active,
            "sell": [f"加权分位≥{sell_th:.2f}卖出"] + active,
        }

    def engine_brief(self) -> str:
        return (
            "分位评分引擎 (percentile)\n"
            "  原理: 每只标的对自身 252 日历史算各指标分位排名, 加权求和打分\n"
            "  买入: 加权分位分 > τ_buy (看涨指标处于历史高位)\n"
            "  卖出: 加权分位分 > τ_sell (看跌指标处于历史高位)\n"
            f"  信号: {', '.join(PERCENTILE_HUMAN.values())}"
        )

    def execution_params(self, params) -> dict:
        vals = getattr(params, "values", params) if not isinstance(params, dict) else params
        return {
            "buy_threshold": _decode_tau(vals.get("buy_score_thresh", 5)),
            "sell_threshold": _decode_tau(vals.get("sell_score_thresh", 5)),
            "position_frac": _decode_pos_frac(vals.get("position_frac", 2)),
        }

    def sensitivity_check(
        self, params, buy_scores, sell_scores, price,
        initial_cash=100000.0, monthly_limit=15000.0,
    ) -> list[dict]:
        """参数敏感性验证：扰动买卖阈值 ±2 级别，看收益是否脆。

        Returns:
            [{"key": "buy_score_thresh -2", "orig_lvl": 5, "new_lvl": 3,
              "ret": +10.2, "orig_ret": +10.2, "drop_pct": 0.0}, ...]
        """
        import numpy as _np
        from .signal_functions import simulate_portfolio
        vals = getattr(params, "values", params) if not isinstance(params, dict) else params
        buy_lvl = int(vals.get("buy_score_thresh", 5))
        sell_lvl = int(vals.get("sell_score_thresh", 5))
        pos_lvl = int(vals.get("position_frac", 2))
        max_buy = TAU_LEVELS - 1
        max_sell = TAU_LEVELS - 1

        # 基准收益
        ex = self.execution_params(vals)
        base_tr = simulate_portfolio(
            buy_scores, sell_scores, price, initial_cash,
            ex["buy_threshold"], ex["sell_threshold"], ex["position_frac"],
            100, monthly_limit, 0.002, [""] * buy_scores.shape[0],
            ["X"] * buy_scores.shape[1],
        )
        base_ret = base_tr.total_return_pct

        # 单个日期太短无法算夏普→用收益差
        results: list[dict] = []
        # 扰动买入阈值
        for delta in (-2, -1, 1, 2):
            nl = max(0, min(buy_lvl + delta, max_buy))
            if nl == buy_lvl:
                continue
            bt = _decode_tau(nl)
            st = ex["sell_threshold"]
            pf = ex["position_frac"]
            tr = simulate_portfolio(
                buy_scores, sell_scores, price, initial_cash,
                bt, st, pf, 100, monthly_limit, 0.002,
                [""] * buy_scores.shape[0], ["X"] * buy_scores.shape[1],
            )
            results.append({
                "key": f"buy_score_thresh {delta:+d}",
                "orig_lvl": buy_lvl, "new_lvl": nl,
                "ret": round(tr.total_return_pct, 2),
                "orig_ret": round(base_ret, 2),
                "drop_pct": round(base_ret - tr.total_return_pct, 2),
            })
        # 扰动卖出阈值
        for delta in (-2, -1, 1, 2):
            nl = max(0, min(sell_lvl + delta, max_sell))
            if nl == sell_lvl:
                continue
            bt = ex["buy_threshold"]
            st = _decode_tau(nl)
            pf = ex["position_frac"]
            tr = simulate_portfolio(
                buy_scores, sell_scores, price, initial_cash,
                bt, st, pf, 100, monthly_limit, 0.002,
                [""] * buy_scores.shape[0], ["X"] * buy_scores.shape[1],
            )
            results.append({
                "key": f"sell_score_thresh {delta:+d}",
                "orig_lvl": sell_lvl, "new_lvl": nl,
                "ret": round(tr.total_return_pct, 2),
                "orig_ret": round(base_ret, 2),
                "drop_pct": round(base_ret - tr.total_return_pct, 2),
            })
        return results

    def random_perturbations(
        self, params, n: int = 10, rng=None,
    ) -> list:
        """生成 n 组随机扰动的参数副本（每维随机偏移 ±1~3 级别）。

        用于参数敏感性评估：最差表现衡量过拟合风险。
        """
        import random as _random
        r = rng or _random.Random()
        vals = getattr(params, "values", params) if not isinstance(params, dict) else params
        copies: list[dict] = []
        max_levels = {d.name: d.levels - 1 for d in self.param_space.dims}
        for _ in range(n):
            c = dict(vals)
            for name, max_lvl in max_levels.items():
                delta = r.randint(-3, 3)
                if delta == 0:
                    continue
                cur = c.get(name, 0)
                c[name] = max(0, min(cur + delta, max_lvl))
            copies.append(c)
        return copies

    def cross_day_volatility(
        self, params, buy_scores, sell_scores, price,
        lookback_days: int = 5,
        initial_cash=100000.0, monthly_limit=15000.0,
    ) -> dict:
        """跨天波动率：过去 N 天每前移 1 天跑一遍回测，记录收益波动。

        Returns:
            {"returns": [r_N, r_{N-1}, ...], "min": -3.5, "max": 5.2, "range": 8.7}
        """
        import numpy as _np
        from .signal_functions import simulate_portfolio
        vals = getattr(params, "values", params) if not isinstance(params, dict) else params
        ex = self.execution_params(vals)
        T = buy_scores.shape[0]
        returns = []
        for offset in range(lookback_days):
            cutoff = T - offset
            if cutoff < 100:  # 数据不足
                break
            bs = buy_scores[:cutoff]
            ss = sell_scores[:cutoff]
            pr = price[:cutoff]
            tr = simulate_portfolio(
                bs, ss, pr, initial_cash,
                ex["buy_threshold"], ex["sell_threshold"], ex["position_frac"],
                100, monthly_limit, 0.005, [""] * cutoff,
                [f"S{i}" for i in range(buy_scores.shape[1])],
            )
            returns.append(round(tr.total_return_pct, 2))
        if not returns:
            return {"returns": [], "min": 0.0, "max": 0.0, "range": 0.0}
        return {
            "returns": returns,
            "min": round(min(returns), 2),
            "max": round(max(returns), 2),
            "range": round(max(returns) - min(returns), 2),
        }
