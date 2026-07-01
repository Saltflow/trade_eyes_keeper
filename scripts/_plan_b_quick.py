"""Plan B quick test: Position-Target with max_daily_adjust=0.40, 5000 samples."""
import sys, time, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd, numpy as np

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

cfg_path = Path("config/optimizer_constraints.yaml")
with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
cfg["discrete_search"]["mode"] = "position_target"
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print(f"Plan B: {len(stocks_data)} stocks, 3000 samples, numba=ON")
from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
opt = StrategyOptimizerV2(stocks_data, "a_share")
t0 = time.time()
report = opt.run(stock_codes=list(stocks_data.keys()), random_starts=3000, iterations=3000)
elapsed = time.time() - t0

cfg["discrete_search"]["mode"] = "frac"
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

mins = elapsed / 60
print(f"Elapsed: {elapsed:.0f}s ({mins:.1f}min)")
if report.top_strategies:
    t = report.top_strategies[0]
    ex_880 = t.test_return
    ex_300 = round(t.strategy_return - t.benchmark_returns.get("510300", 0), 2)
    ex_rf = round(t.strategy_return - t.benchmark_returns.get("risk_free", 0), 2)
    sl = t.params.get("position_slope", "?")
    bi = t.params.get("position_bias", "?")
    p0 = {k: v for k, v in list(t.params.items())[:4] if not k.startswith("_")}
    print(f"  strategy={t.strategy_return:+.1f}%  vs880={ex_880:+.1f}%  vs300={ex_300:+.1f}%  vsRF={ex_rf:+.1f}%")
    print(f"  dd={t.test_drawdown:.2f}%  trades={t.trade_count}  final_pos={t.final_position_pct:.0f}%  nav={t.total_nav:.0f}")
    print(f"  params: {p0}  slope={sl}  bias={bi}")
    hlist = [h for h in (t.final_holdings or []) if h.get("shares", 0) > 0]
    if hlist:
        for h in hlist[:5]:
            code = h.get("code", "?")
            sh = h.get("shares", 0)
            px = h.get("price", 0)
            val = h.get("value", 0)
            print(f"    {code} {sh:.0f}股 @{px:.2f} val={val:.0f}")
        print(f"    现金: {t.final_cash:.0f}")

    # 季度持仓
    qh = t.quarterly_holdings
    if qh:
        print(f"\n  逐季持仓明细:")
        for q in qh[:4]:
            print(f"    Q{q['quarter']}(d{q['day']}): 仓位={q['pos_pct']:.0f}%  nav={q['nav']:.0f}")
            for pos in q["positions"]:
                print(f"      {pos['code']} {pos['shares']:.0f}股 cost={pos['cost']:.2f} px={pos['price']:.2f} val={pos['value']:.0f} pnl={pos['pnl']:+.0f} ({pos['pnl_pct']:+.1f}%)")
            if not q["positions"]:
                print(f"      (空仓)")
            print(f"      现金: {q['cash']:.0f}")
else:
    print("  No strategies found")
print("DONE")
