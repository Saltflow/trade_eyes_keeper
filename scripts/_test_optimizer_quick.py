"""Quick optimizer integration test — cached data only, 100 samples, verify benchmark flow."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
from src.analysis.optimizer_constraints import load_constraints

# Load all cached stocks from cache/data (skip network)
cache_dir = Path("cache/data")
stocks_data: dict[str, pd.DataFrame] = {}
for f in sorted(cache_dir.glob("*.csv")):
    try:
        df = pd.read_csv(f, encoding="utf-8")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        if len(df) >= 252:
            stocks_data[f.stem] = df
    except Exception:
        pass

# Filter to config stocks
import yaml
config_path = Path("config/config.yaml")
with open(config_path, "r", encoding="utf-8") as fh:
    config = yaml.safe_load(fh)
config_stocks = [str(s) for s in config.get("stocks", [])]

# Separate A-shares and non-A (exclude benchmark codes from trading)
BENCH_CODES = {"510300", "510880"}  # Only these are benchmarks, not trading targets
a_stocks = {}
non_a_stocks = {}
for code in config_stocks:
    if code not in stocks_data:
        continue
    if code in BENCH_CODES:
        continue
    if code.isdigit() and len(code) == 6:
        a_stocks[code] = stocks_data[code]
    else:
        non_a_stocks[code] = stocks_data[code]

print(f"A-shares: {len(a_stocks)} stocks ({list(a_stocks.keys())[:5]}...)")
print(f"Non-A: {len(non_a_stocks)} stocks ({list(non_a_stocks.keys())[:5]}...)")

# Run short optimizer for A-shares
print("\n=== A-Share Quick Test (100 samples) ===")
opt_a = StrategyOptimizerV2(
    a_stocks, "a_share",
)
report_a = opt_a.run(
    stock_codes=list(a_stocks.keys()),
    random_starts=3000, iterations=3000,
)
if report_a.top_strategies:
    t = report_a.top_strategies[0]
    print(f"  Top1: test_return={t.test_return:.2f}%, drawdown={t.test_drawdown:.2f}%, trades={t.trade_count}")
    print(f"  Params: { {k: v for k, v in list(t.params.items())[:5]} }...")
else:
    print("  No strategies found (all filtered by constraints)")

# Run short optimizer for Non-A
print("\n=== Non-A Quick Test (100 samples) ===")
opt_n = StrategyOptimizerV2(
    non_a_stocks, "non_a_share",
)
report_n = opt_n.run(
    stock_codes=list(non_a_stocks.keys()),
    random_starts=3000, iterations=3000,
)
if report_n.top_strategies:
    t = report_n.top_strategies[0]
    print(f"  Top1: test_return={t.test_return:.2f}%, drawdown={t.test_drawdown:.2f}%, trades={t.trade_count}")
    print(f"  Params: { {k: v for k, v in list(t.params.items())[:5]} }...")
else:
    print("  No strategies found")
