"""Local quick preview: compare Fixed-Frac vs Position-Target on cached data."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.fast_evaluator import FastEvaluator

# Load config stock list
config_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
with open(config_path, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)
config_stocks = [str(s) for s in config.get("stocks", [])]
print(f"Config stocks ({len(config_stocks)}): {config_stocks[:8]}..." if len(config_stocks) > 8 else f"Config stocks ({len(config_stocks)}): {config_stocks}")

# Load only stocks that exist in both config and cache
cache_dir = Path("cache/data")
dfs = {}
for f in sorted(cache_dir.glob("*.csv")):
    code = f.stem
    if code not in config_stocks:
        continue
    try:
        df = pd.read_csv(f, encoding="utf-8")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        if len(df) >= 252:
            dfs[code] = df
    except Exception:
        pass

# Warn about config stocks not found locally
missing = [s for s in config_stocks if s not in dfs]
if missing:
    print(f"Missing from local cache ({len(missing)}): {missing}")

codes = sorted(dfs.keys())
# Exclude benchmark ETFs from trading pool
trading_codes = [c for c in codes if c not in ("510300", "510880")]
print(f"Loaded {len(dfs)} stocks total, {len(trading_codes)} trading: {trading_codes[:5]}...{trading_codes[-3:]}")
print()

# Find common date range
all_dates = set(dfs[trading_codes[0]]["date"].dt.strftime("%Y-%m-%d"))
for c in trading_codes[1:]:
    all_dates &= set(dfs[c]["date"].dt.strftime("%Y-%m-%d"))
dates = sorted(all_dates)
N = len(trading_codes)
T = min(len(dates), 365)
dates = dates[-T:]
print(f"Common trading days: {len(dates)}, using last {T}")
print()

# Build indicator matrix (T, N, 8)
ind = np.zeros((T, N, 8), dtype=np.float32)
p_close = np.zeros((T, N), dtype=np.float32)
p_open = np.zeros((T, N), dtype=np.float32)

for n, code in enumerate(trading_codes):
    df = dfs[code].copy()
    df["ds"] = df["date"].dt.strftime("%Y-%m-%d")
    df = df.set_index("ds")
    roll_ma = df["close"].rolling(60).mean()
    for t, d in enumerate(dates):
        if d in df.index:
            row = df.loc[d]
            c = float(row["close"])
            o = float(row["open"])
            ind[t, n, 0] = c
            p_close[t, n] = c
            p_open[t, n] = o
            ma = float(roll_ma.loc[d]) if d in roll_ma.index and not np.isnan(roll_ma.loc[d]) else c
            ind[t, n, 1] = ma
            ind[t, n, 2] = (c - ma) / ma if ma > 0 else 0.0
            # Simple ADX and MACD placeholder (trend_follow needs these)
            if t >= 14:
                tr_sum = 0.0
                for bt in range(t - 13, t + 1):
                    pd_close = float(df.loc[dates[bt], "close"]) if dates[bt] in df.index else c
                    pd_open = float(df.loc[dates[bt], "open"]) if dates[bt] in df.index else c
                    tr_sum += abs(pd_close - pd_open)
                ind[t, n, 6] = min(tr_sum / 14 / (c + 0.01) * 100, 100.0)  # rough ADX
            # Simple MACD: 12-26 EMA diff
            if t >= 26:
                ema12 = 0.0
                ema26 = 0.0
                for bt in range(t - 25, t + 1):
                    pc = float(df.loc[dates[bt], "close"]) if dates[bt] in df.index else c
                    if bt == t - 25:
                        ema12 = pc
                        ema26 = pc
                    else:
                        ema12 = pc * 0.15 + ema12 * 0.85
                        ema26 = pc * 0.075 + ema26 * 0.925
                ind[t, n, 4] = (ema12 - ema26) / (c + 0.01)
            # Simple RSI-14
            if t >= 14:
                gains = 0.0
                losses = 0.0
                for bt in range(t - 13, t + 1):
                    pc = float(df.loc[dates[bt], "close"]) if dates[bt] in df.index else 0
                    pp = float(df.loc[dates[bt - 1], "close"]) if bt > 0 and dates[bt - 1] in df.index else pc
                    diff = pc - pp
                    if diff > 0:
                        gains += diff
                    else:
                        losses -= diff
                rs = gains / (losses + 0.01)
                ind[t, n, 3] = 100.0 - 100.0 / (1.0 + rs)  # RSI
            # Volume ratio
            if t >= 5:
                vol_now = float(df.loc[dates[t], "volume"]) if dates[t] in df.index else 1
                vol_avg = 0.0
                for bt in range(max(0, t - 5), t):
                    vol_avg += float(df.loc[dates[bt], "volume"]) if dates[bt] in df.index else 1
                vol_avg /= min(5, t)
                ind[t, n, 7] = vol_now / (vol_avg + 0.01) if vol_avg > 0 else 1.0

print(f"Indicator: {T}d x {N} stocks x 8 features")
# Cash baseline (still needed as fallback)
rf_daily = 0.02 / 252
cash_baseline = np.cumsum(np.ones(T) * 100000 * rf_daily) + 100000

# Build benchmark series: 510300 first (primary), then 510880, then risk_free
from collections import OrderedDict
benchmarks = OrderedDict()
for bench_code in ["510300", "510880"]:
    bench_path = cache_dir / f"{bench_code}.csv"
    if bench_path.exists():
        bdf = pd.read_csv(bench_path, encoding="utf-8")
        bdf["date"] = pd.to_datetime(bdf["date"])
        bdf["ds"] = bdf["date"].dt.strftime("%Y-%m-%d")
        bdf = bdf.set_index("ds")
        b_close = np.zeros(T, dtype=np.float64)
        for t, d in enumerate(dates):
            if d in bdf.index:
                b_close[t] = float(bdf.loc[d, "close"])
            elif t > 0:
                b_close[t] = b_close[t - 1]
        if b_close[0] > 0:
            benchmarks[bench_code] = b_close
            print(f"Benchmark {bench_code}: first={b_close[0]:.2f}, last={b_close[-1]:.2f}")
    else:
        print(f"Warning: {bench_code}.csv not found")

# Risk-free (A股: 2.0%)
risk_free = np.cumsum(np.ones(T) * 100000 * 0.02 / 252) + 100000
benchmarks["risk_free"] = risk_free

# Strategy: trend_follow (works in bull markets) + rsi_signal (oversold dips)
buy_builders = ["trend_follow", "rsi_signal"]
buy_thresholds = [0.3, 0.5]
buy_fracs = [0.15, 0.25]  # old mode only

sell_builders = ["sell_rsi_signal", "sell_deviation_cross"]
sell_thresholds = [0.3, 0.3]
sell_fracs = [0.3, 0.3]  # old mode only

ev = FastEvaluator(initial_cash=100000, monthly_buy_limit=15000, lot_size=100)

# === Old: Fixed-Frac ===
s_old = ev.evaluate(
    ind, p_close, cash_baseline,
    buy_builders, buy_thresholds, buy_fracs,
    sell_builders, sell_thresholds, sell_fracs,
    price_open_matrix=p_open,
    benchmark_series=benchmarks,
)

# === New: Position-Target ===
s_new = ev.evaluate_position_target(
    ind, p_close, cash_baseline,
    buy_builders, buy_thresholds,
    sell_builders, sell_thresholds,
    position_slope=3.0, position_bias=0.0,
    price_open_matrix=p_open,
    benchmark_series=benchmarks,
)

print()
print("=" * 65)
print(f"  Stocks: {N} ({trading_codes[0]}..{trading_codes[-1]}), {T} trading days")
print("=" * 65)
print(f"{'Metric':<20} {'Fixed-Frac':>20} {'Position-Target':>22}")
print("-" * 65)
print(f"{'Strategy Return %':<20} {s_old.strategy_return:>20.2f} {s_new.strategy_return:>22.2f}")
print(f"{'Excess (vs 510300)%':<20} {s_old.excess_vs('510300'):>20.2f} {s_new.excess_vs('510300'):>22.2f}")
print(f"{'Excess (vs 510880)%':<20} {s_old.excess_vs('510880'):>20.2f} {s_new.excess_vs('510880'):>22.2f}")
print(f"{'Excess (vs risk_free)%':<20} {s_old.excess_vs('risk_free'):>20.2f} {s_new.excess_vs('risk_free'):>22.2f}")
print(f"{'Max Drawdown %':<20} {s_old.max_drawdown_pct:>20.2f} {s_new.max_drawdown_pct:>22.2f}")
print(f"{'Avg Position %':<20} {s_old.avg_position_pct:>20.2f} {s_new.avg_position_pct:>22.2f}")
print(f"{'Sharpe Ratio':<20} {s_old.sharpe_ratio:>20.4f} {s_new.sharpe_ratio:>22.4f}")
print(f"{'Total Trades':<20} {s_old.total_trades:>20} {s_new.total_trades:>22}")
print(f"{'Trades/Month':<20} {s_old.trades_per_month:>20.1f} {s_new.trades_per_month:>22.1f}")
print("=" * 65)
print()
print(f"Benchmarks: {s_old.benchmark_returns}")

# Slope scan
print()
print("Slope scan (bias=0):")
for slope in [1.0, 2.0, 3.0, 5.0, 8.0]:
    s = ev.evaluate_position_target(
        ind, p_close, cash_baseline,
        buy_builders, buy_thresholds,
        sell_builders, sell_thresholds,
        position_slope=slope, position_bias=0.0,
        price_open_matrix=p_open,
        benchmark_series=benchmarks,
    )
    print(
        f"  slope={slope:.0f}: "
        f"strategy={s.strategy_return:+.2f}%  "
        f"ex510300={s.excess_vs('510300'):+.2f}%  "
        f"dd={s.max_drawdown_pct:.2f}%  "
        f"pos={s.avg_position_pct:.1f}%  "
        f"trades={s.total_trades:>3}  "
        f"t/m={s.trades_per_month:.1f}"
    )

print()
print("Bias scan (slope=3.0):")
for bias in [-2.0, -1.0, 0.0, 1.0, 2.0]:
    s = ev.evaluate_position_target(
        ind, p_close, cash_baseline,
        buy_builders, buy_thresholds,
        sell_builders, sell_thresholds,
        position_slope=3.0, position_bias=bias,
        price_open_matrix=p_open,
        benchmark_series=benchmarks,
    )
    print(
        f"  bias={bias:+.0f}: "
        f"strategy={s.strategy_return:+.2f}%  "
        f"ex510300={s.excess_vs('510300'):+.2f}%  "
        f"dd={s.max_drawdown_pct:.2f}%  "
        f"pos={s.avg_position_pct:.1f}%  "
        f"trades={s.total_trades:>3}  "
        f"t/m={s.trades_per_month:.1f}"
    )
