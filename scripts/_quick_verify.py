"""Minimal: verify benchmark data flows through optimizer end-to-end (50 samples)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np

# Load 5 A-share stocks only (fast)
stocks_data = {}
for code in ['601728', '600938', '601985', '601398', '601088']:
    df = pd.read_csv(f'cache/data/{code}.csv', encoding='utf-8')
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    stocks_data[code] = df

from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
import time

print("=== Benchmark fallback test ===")
t0 = time.time()

opt = StrategyOptimizerV2(stocks_data, "a_share")
report = opt.run(
    stock_codes=list(stocks_data.keys()),
    random_starts=100, iterations=100,
)

elapsed = time.time() - t0
print(f"Elapsed: {elapsed:.0f}s")

if report.top_strategies:
    t = report.top_strategies[0]
    print(f"Top1: return={t.test_return:.2f}%, dd={t.test_drawdown:.2f}%, trades={t.trade_count}")
    # Show benchmark info from params (if any)
    print(f"Params sample: {dict(list(t.params.items())[:4])}")
else:
    print("No strategies passed constraints with 100 samples")

# Quick check: did benchmarks load?
from src.analysis.optimizer_constraints import load_constraints
c = load_constraints()
c.set_group("a_share")
print(f"\nBenchmark codes: {c.benchmark_codes}")
print(f"Risk-free rate: {c.risk_free_rate}")

# Verify 510300 in cache
cache_path = Path("cache/data/510300.csv")
print(f"510300 cache: {cache_path.exists()}, size={cache_path.stat().st_size if cache_path.exists() else 0}")
# Check if BRK.B was fetched
brkb_path = Path("cache/data/BRK.B.csv")
print(f"BRK.B cache: {brkb_path.exists()}")
