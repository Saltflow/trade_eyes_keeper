"""Plan A with numba (should be fast)."""
import sys, time, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd, numpy as np

with open("config/config.yaml", "r", encoding="utf-8") as f: scfg = yaml.safe_load(f)
config_stocks = [str(s) for s in scfg.get("stocks", [])]
stocks_data = {}
for code in config_stocks:
    if code in ("510300", "510880"): continue
    if not (code.isdigit() and len(code) == 6): continue
    p = Path(f"cache/data/{code}.csv")
    if not p.exists(): continue
    df = pd.read_csv(p, encoding="utf-8"); df["date"] = pd.to_datetime(df["date"]); df = df.sort_values("date")
    if len(df) >= 252: stocks_data[code] = df

cfg_path = Path("config/optimizer_constraints.yaml")
with open(cfg_path) as f: cfg = yaml.safe_load(f)
cfg["discrete_search"]["mode"] = "frac"
cfg["discrete_search"]["frac_levels"] = [0.30, 0.45, 0.60, 0.75, 0.90, 1.00]
with open(cfg_path, "w") as f: yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

from src.analysis.fast_evaluator import HAS_NUMBA
print(f"Plan A: {len(stocks_data)} stocks, 20000 samples, numba={'ON' if HAS_NUMBA else 'OFF'}")

from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
opt = StrategyOptimizerV2(stocks_data, "a_share")
t0 = time.time()
report = opt.run(stock_codes=list(stocks_data.keys()), random_starts=20000, iterations=20000)
elapsed = time.time() - t0
print(f"Elapsed: {elapsed:.0f}s ({elapsed/60:.0f}min)")
if report.top_strategies:
    t = report.top_strategies[0]
    ex_880, ex_300, ex_rf = t.test_return, round(t.strategy_return-t.benchmark_returns.get("510300",0),2), round(t.strategy_return-t.benchmark_returns.get("risk_free",0),2)
    print(f"  strategy={t.strategy_return:+.1f}% vs880={ex_880:+.1f}% vs300={ex_300:+.1f}% vsRF={ex_rf:+.1f}%")
    print(f"  dd={t.test_drawdown:.2f}% trades={t.trade_count} final_pos={t.final_position_pct:.0f}% nav={t.total_nav:.0f}")
    print(f"  params: {dict(list(t.params.items())[:6])}")
    hlist = [h for h in t.final_holdings if h.get("shares",0) > 0]
    if hlist:
        for h in hlist:
            pnl = h["value"] - h.get("cost_value",0)
            pnl_pct = (h["price"]/h["cost"]-1)*100 if h.get("cost",0)>0 else 0
            print(f"    {h['code']} {h['shares']:.0f}股 cost={h['cost']:.2f} px={h['price']:.2f} val={h['value']:.0f} pnl={pnl:+.0f} ({pnl_pct:+.1f}%)")
        print(f"    现金: {t.final_cash:.0f}")
print("DONE")
