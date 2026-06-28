"""Full optimizer test with benchmarks — A-shares only, cached data."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd, yaml

# Load config stocks, A-shares only, exclude 510300/510880
with open("config/config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

config_stocks = [str(s) for s in config.get("stocks", [])]
stocks_data = {}
for code in config_stocks:
    if code in ("510300", "510880"):  # benchmarks, not trading targets
        continue
    if not (code.isdigit() and len(code) == 6):  # A-shares only
        continue
    csv_path = Path(f"cache/data/{code}.csv")
    if not csv_path.exists():
        continue
    df = pd.read_csv(csv_path, encoding="utf-8")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    if len(df) >= 252:
        stocks_data[code] = df

print(f"A-shares: {len(stocks_data)} stocks: {list(stocks_data.keys())}")

from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2

opt = StrategyOptimizerV2(stocks_data, "a_share")
print(f"Running optimizer ({len(stocks_data)} stocks, 3000 samples)...")
t0 = time.time()
report = opt.run(
    stock_codes=list(stocks_data.keys()),
    random_starts=3000, iterations=3000,
)
elapsed = time.time() - t0

print(f"\n{'='*60}")
print(f"Elapsed: {elapsed:.0f}s")
print(f"Top strategies: {len(report.top_strategies)}")

if report.top_strategies:
    for i, t in enumerate(report.top_strategies[:5]):
        print(f"  #{i+1}: ret={t.test_return:+.2f}%  dd={t.test_drawdown:.2f}%  "
              f"trades={t.trade_count}")
    # Show first strategy params
    t1 = report.top_strategies[0]
    print(f"\n  Top1 params: {dict(list(t1.params.items())[:6])}")
else:
    print("  No strategies passed constraints")
    print("  (Try more samples or relax min_avg_position_pct in config)")
print(f"{'='*60}")
