"""Plan A: Fixed-Frac maxed out (frac up to 100%, monthly_limit=100000)."""
import sys, time, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd, numpy as np

# Force frac mode in config
config_path = Path("config/optimizer_constraints.yaml")
with open(config_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
cfg["discrete_search"]["mode"] = "frac"
cfg["discrete_search"]["frac_levels"] = [0.30, 0.45, 0.60, 0.75, 0.90, 1.00]
with open(config_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# Load stocks (same as _run_full_test)
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

print(f"Plan A (Fixed-Frac maxed): {len(stocks_data)} stocks, 20000 samples")
from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
opt = StrategyOptimizerV2(stocks_data, "a_share")
t0 = time.time()
report = opt.run(stock_codes=list(stocks_data.keys()), random_starts=20000, iterations=20000)
elapsed = time.time() - t0

print(f"\n{'='*62}")
print(f"Plan A (Fixed-Frac) | Elapsed: {elapsed:.0f}s | Top: {len(report.top_strategies)}")
print(f"{'='*62}")
if report.top_strategies:
    for i, t in enumerate(report.top_strategies[:3]):
        ex_880 = t.test_return
        ex_300 = round(t.strategy_return - t.benchmark_returns.get('510300', 0), 2)
        ex_rf = round(t.strategy_return - t.benchmark_returns.get('risk_free', 0), 2)
        print(f"  #{i+1}: strategy={t.strategy_return:+.1f}%  vs510880={ex_880:+.1f}%  "
              f"vs510300={ex_300:+.1f}%  vsRF={ex_rf:+.1f}%")
        print(f"       dd={t.test_drawdown:.2f}%  trades={t.trade_count}  "
              f"final_pos={t.final_position_pct:.0f}%  nav={t.total_nav:.0f}")
    t1 = report.top_strategies[0]
    print(f"\n  Benchmarks: {t1.benchmark_returns}")
    print(f"  Top1 params: {dict(list(t1.params.items())[:8])}")
    if t1.final_holdings:
        print(f"\n  期末持仓:")
        for h in t1.final_holdings:
            if h['shares'] <= 0: continue
            pnl = h['value'] - h.get('cost_value', 0)
            pnl_pct = (h['price']/h['cost']-1)*100 if h.get('cost',0)>0 else 0
            print(f"    {h['code']} {h['shares']:.0f}股 cost={h['cost']:.2f} px={h['price']:.2f} "
                  f"val={h['value']:.0f} pnl={pnl:+.0f} ({pnl_pct:+.1f}%)")
        print(f"    现金: {t1.final_cash:.0f}  总资产: {t1.total_nav:.0f}")
print(f"{'='*62}")
