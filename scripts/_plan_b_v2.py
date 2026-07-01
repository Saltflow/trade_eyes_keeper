"""Run Plan B with max_daily_adjust=0.40 + numba check."""
import sys, time, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd, numpy as np

# Load stocks
with open("config/config.yaml", "r", encoding="utf-8") as f:
    scfg = yaml.safe_load(f)
config_stocks = [str(s) for s in scfg.get("stocks", [])]
stocks_data = {}
for code in config_stocks:
    if code in ("510300", "510880"):
        continue
    if not (code.isdigit() and len(code) == 6):
        continue
    p = Path(f"cache/data/{code}.csv")
    if not p.exists():
        continue
    df = pd.read_csv(p, encoding="utf-8")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    if len(df) >= 252:
        stocks_data[code] = df

# Set mode=position_target
cfg_path = Path("config/optimizer_constraints.yaml")
with open(cfg_path) as f:
    cfg = yaml.safe_load(f)
cfg["discrete_search"]["mode"] = "position_target"
with open(cfg_path, "w") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

from src.analysis.fast_evaluator import HAS_NUMBA
has_numba = HAS_NUMBA
label_nb = "ON" if has_numba else "OFF"
print(f"Plan B: {len(stocks_data)} stocks, 20000 samples, max_daily_adjust=0.40, numba={label_nb}")

from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
opt = StrategyOptimizerV2(stocks_data, "a_share")
t0 = time.time()
report = opt.run(stock_codes=list(stocks_data.keys()), random_starts=20000, iterations=20000)
elapsed = time.time() - t0

# Restore
cfg["discrete_search"]["mode"] = "frac"
with open(cfg_path, "w") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

mins = elapsed / 60
print(f"Elapsed: {elapsed:.0f}s ({mins:.0f}min)")
if report.top_strategies:
    t = report.top_strategies[0]
    ex_880 = t.test_return
    ex_300 = round(t.strategy_return - t.benchmark_returns.get("510300", 0), 2)
    ex_rf = round(t.strategy_return - t.benchmark_returns.get("risk_free", 0), 2)
    print(f"  strategy={t.strategy_return:+.1f}%  vs880={ex_880:+.1f}%  "
          f"vs300={ex_300:+.1f}%  vsRF={ex_rf:+.1f}%")
    print(f"  dd={t.test_drawdown:.2f}%  trades={t.trade_count}  "
          f"final_pos={t.final_position_pct:.0f}%  nav={t.total_nav:.0f}")
    sl = t.params.get("position_slope", "?")
    bi = t.params.get("position_bias", "?")
    print(f"  params: {dict(list(t.params.items())[:4])}  slope={sl}  bias={bi}")
    if t.final_holdings:
        hlist = [h for h in t.final_holdings if h["shares"] > 0]
        if hlist:
            for h in hlist:
                print(f"    {h['code']} {h['shares']:.0f}股 @{h['price']:.2f} val={h['value']:.0f}")
        print(f"    现金: {t.final_cash:.0f}")
print("DONE")
