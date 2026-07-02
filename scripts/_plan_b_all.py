"""Plan B: all A-shares, relaxed DD, quarterly holdings."""
import sys, time, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd, numpy as np

with open("config/config.yaml", "r", encoding="utf-8") as f:
    scfg = yaml.safe_load(f)
stocks_data = {}
for code in [str(s) for s in scfg.get("stocks", [])]:
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
cfg["genetic_search"]["num_generations"] = 1
old_dd = cfg["hard_constraints"]["max_drawdown_pct"]
old_pos = cfg["hard_constraints"]["min_avg_position_pct"]
cfg["hard_constraints"]["max_drawdown_pct"] = -50
cfg["hard_constraints"]["min_avg_position_pct"] = 5
cfg["hard_constraints"]["min_avg_position_pct"] = 5  # Position-Target naturally low
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print(f"Plan B: {len(stocks_data)} stocks, 2000 samples, max_dd=-50")
from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
opt = StrategyOptimizerV2(stocks_data, "a_share")
t0 = time.time()
report = opt.run(stock_codes=list(stocks_data.keys()), random_starts=2100, iterations=2100)
elapsed = time.time() - t0

cfg["discrete_search"]["mode"] = "frac"
cfg["genetic_search"]["num_generations"] = 5
cfg["hard_constraints"]["max_drawdown_pct"] = old_dd
cfg["hard_constraints"]["min_avg_position_pct"] = old_pos
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print(f"{elapsed:.0f}s, top={len(report.top_strategies)}")
if report.top_strategies:
    t = report.top_strategies[0]
    sl = t.params.get("position_slope", "?")
    bi = t.params.get("position_bias", "?")
    stl = f"strategy={t.strategy_return:+.1f}%"
    vsl = f"vs880={t.test_return:+.1f}%"
    ddl = f"dd={t.test_drawdown:.2f}%"
    fpl = f"final_pos={t.final_position_pct:.0f}%"
    nvl = f"nav={t.total_nav:.0f}"
    print(f"  {stl}  {vsl}  {ddl}  {fpl}  {nvl}")
    print(f"  slope={sl}  bias={bi}")

    qh = t.quarterly_holdings
    if qh:
        print("  逐季持仓:")
        for q in qh:
            qn = q["quarter"]
            qd = q["day"]
            qp = q["pos_pct"]
            qnv = q["nav"]
            qcs = q["cash"]
            print(f"    Q{qn}(d{qd}): pos={qp:.0f}%  nav={qnv:.0f}")
            for pos in q["positions"]:
                cd = pos["code"]
                sh = pos["shares"]
                cb = pos["cost"]
                px = pos["price"]
                vl = pos["value"]
                pn = pos["pnl"]
                pp = pos["pnl_pct"]
                print(f"      {cd} {sh:.0f}股 cost={cb:.2f} px={px:.2f} val={vl:.0f} pnl={pn:+.0f}({pp:+.1f}%)")
            if not q["positions"]:
                print("      (空仓)")
            print(f"      现金:{qcs:.0f}")
else:
    print("  No strategies found")
print("DONE")
