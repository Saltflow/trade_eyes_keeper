"""Verify benchmark data flows through FastEvaluator end-to-end."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd, numpy as np
from collections import OrderedDict

from src.analysis.walk_forward import WalkForwardManager
from src.analysis.fast_evaluator import FastEvaluator
from src.analysis.optimizer_constraints import load_constraints

# Load stocks
stocks_data = {}
for code in ['601728', '600938']:
    df = pd.read_csv(f'cache/data/{code}.csv', encoding='utf-8')
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    stocks_data[code] = df

bench_dfs = {}
for bcode in ['510300', '510880']:
    bpath = Path(f'cache/data/{bcode}.csv')
    if bpath.exists():
        bdf = pd.read_csv(bpath, encoding='utf-8')
        bdf['date'] = pd.to_datetime(bdf['date'])
        bench_dfs[bcode] = bdf

c = load_constraints()
c.set_group('a_share')

wf = WalkForwardManager(stocks_data, benchmark_dfs=bench_dfs)
ws = wf.iter_windows()[0]

test_ind = wf.build_matrices(ws, 'test')
test_price = wf.get_price_matrix(ws, 'test')
T_test = test_ind.shape[0]

# Build benchmark series
benchmarks = OrderedDict()
for bcode in c.benchmark_codes:
    if bcode == 'risk_free':
        rf_daily = c.risk_free_rate / 252.0
        rf_series = np.cumsum(np.ones(T_test) * 100000 * rf_daily) + 100000
        benchmarks['risk_free'] = rf_series
    else:
        bc = wf.get_benchmark_price(bcode, ws, 'test')
        if bc is not None and len(bc) > 0 and not np.isnan(bc[0]):
            benchmarks[bcode] = bc

cash_baseline = benchmarks.get('risk_free', np.ones(T_test) * 100000)

print(f'Benchmarks: {list(benchmarks.keys())}')
for k, v in benchmarks.items():
    print(f'  {k}: return={(v[-1]-v[0])/v[0]*100:.1f}%')

# Evaluate
ev = FastEvaluator(initial_cash=100000, monthly_buy_limit=15000, lot_size=100)
stats = ev.evaluate(
    test_ind, test_price, cash_baseline,
    buy_builders=['deviation_cross', 'trend_follow'],
    buy_thresholds=[0.3, 0.5],
    buy_fracs=[0.15, 0.25],
    sell_builders=['sell_rsi_signal'],
    sell_thresholds=[0.4],
    sell_fracs=[0.3],
    benchmark_series=benchmarks if benchmarks else None,
)

print(f'\nStrategy return: {stats.strategy_return:.2f}%')
print(f'Excess (test_excess_return): {stats.test_excess_return:.2f}%')
print(f'Benchmark returns: {stats.benchmark_returns}')
ex_510300 = stats.excess_vs('510300')
ex_rf = stats.excess_vs('risk_free')
print(f'Excess vs 510300: {ex_510300:.2f}%')
print(f'Excess vs risk_free: {ex_rf:.2f}%')
print(f'Avg position: {stats.avg_position_pct:.2f}%')
print(f'Trades: {stats.total_trades}')
print('\n=== BENCHMARK VERIFICATION COMPLETE ===')
