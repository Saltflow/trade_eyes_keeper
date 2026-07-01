"""Run Plan A (Fixed-Frac maxed) then Plan B (Position-Target), compare results."""
import sys, time, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd, numpy as np

# ═══ common stock loading ═══
with open("config/config.yaml", "r", encoding="utf-8") as f:
    scfg = yaml.safe_load(f)
config_stocks = [str(s) for s in scfg.get("stocks", [])]
stocks_data = {}
for code in config_stocks:
    if code in ("510300", "510880"):
        continue
    if not (code.isdigit() and len(code) == 6):
        continue
    csv_path = Path(f"cache/data/{code}.csv")
    if not csv_path.exists():
        continue
    df = pd.read_csv(csv_path, encoding="utf-8")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    if len(df) >= 252:
        stocks_data[code] = df

STOCK_COUNT = len(stocks_data)
SAMPLES = 20000

results = {}

# ═══ Plan A ═══
print(f"\n{'='*62}")
print(f"  PLAN A: Fixed-Frac (frac up to 100%, monthly_limit=100000)")
print(f"{'='*62}")

cfg_path = Path("config/optimizer_constraints.yaml")
with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
cfg["discrete_search"]["mode"] = "frac"
cfg["discrete_search"]["frac_levels"] = [0.30, 0.45, 0.60, 0.75, 0.90, 1.00]
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
opt = StrategyOptimizerV2(stocks_data, "a_share")
t0 = time.time()
report_a = opt.run(stock_codes=list(stocks_data.keys()), random_starts=SAMPLES, iterations=SAMPLES)
elapsed_a = time.time() - t0
results["A"] = (report_a, elapsed_a)

# ═══ Plan B ═══
print(f"\n{'='*62}")
print(f"  PLAN B: Position-Target (sigmoid bullish_score → target)")
print(f"{'='*62}")

with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
cfg["discrete_search"]["mode"] = "position_target"
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

opt2 = StrategyOptimizerV2(stocks_data, "a_share")
t0 = time.time()
report_b = opt2.run(stock_codes=list(stocks_data.keys()), random_starts=SAMPLES, iterations=SAMPLES)
elapsed_b = time.time() - t0
results["B"] = (report_b, elapsed_b)

# Restore frac mode
cfg["discrete_search"]["mode"] = "frac"
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# ═══ Comparison ═══
print(f"\n{'='*62}")
print(f"  COMPARISON: Plan A vs Plan B ({STOCK_COUNT} stocks, {SAMPLES} samples)")
print(f"{'='*62}")
print(f"{'Metric':<20} {'Plan A (Frac)':>18} {'Plan B (Pos-Target)':>22}")
print(f"{'-'*62}")

for label, (report, elapsed) in results.items():
    if not report.top_strategies:
        print(f"  Plan {label}: No strategies found")
        continue

    t = report.top_strategies[0]
    ex_880 = t.test_return
    ex_300 = round(t.strategy_return - t.benchmark_returns.get('510300', 0), 2)
    ex_rf = round(t.strategy_return - t.benchmark_returns.get('risk_free', 0), 2)
    mode_tag = t.params.get("_mode", "?")

    print(f"\n  Plan {label} ({mode_tag}) | {elapsed:.0f}s")
    print(f"    strategy_return: {t.strategy_return:+.1f}%")
    print(f"    vs510880:        {ex_880:+.1f}%")
    print(f"    vs510300:        {ex_300:+.1f}%")
    print(f"    vsRF:            {ex_rf:+.1f}%")
    print(f"    max_dd:          {t.test_drawdown:.2f}%")
    print(f"    trades:          {t.trade_count}")
    print(f"    final_pos:       {t.final_position_pct:.0f}%")
    print(f"    total_nav:       {t.total_nav:.0f}")

    if label == "A":
        # Show top params for Frac
        p = {k: v for k, v in list(t.params.items())[:6] if not k.startswith("_")}
        print(f"    params:          {p}")
    else:
        p = {k: v for k, v in list(t.params.items())[:4] if not k.startswith("_")}
        sl = t.params.get("position_slope", "?")
        bi = t.params.get("position_bias", "?")
        print(f"    params:          {p}  slope={sl} bias={bi}")

    # Show holdings summary
    if t.final_holdings:
        total_val = sum(h['value'] for h in t.final_holdings if h['shares'] > 0)
        count = sum(1 for h in t.final_holdings if h['shares'] > 0)
        print(f"    holdings:        {count} stocks, value={total_val:.0f}")

print(f"\n{'='*62}")
print(f"  DONE")
print(f"{'='*62}")
