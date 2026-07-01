"""Plan B with quarterly holdings, gen=1."""
import sys, time, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd, numpy as np

with open("config/config.yaml", "r", encoding="utf-8") as f:
    scfg = yaml.safe_load(f)
stocks_data = {}
for code in [str(s) for s in scfg.get("stocks", [])][:5]:
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
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print(f"Plan B: {len(stocks_data)} stocks, 2000 samples, gen=1")
from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
opt = StrategyOptimizerV2(stocks_data, "a_share")
t0 = time.time()
report = opt.run(stock_codes=list(stocks_data.keys()), random_starts=2000, iterations=2000)
elapsed = time.time() - t0

cfg["discrete_search"]["mode"] = "frac"
cfg["genetic_search"]["num_generations"] = 5
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print(f"{elapsed:.0f}s, top={len(report.top_strategies)}")
if report.top_strategies:
    t = report.top_strategies[0]
    sl = t.params.get("position_slope", "?")
    bi = t.params.get("position_bias", "?")
    print(f"  strategy={t.strategy_return:+.1f}%  vs880={t.test_return:+.1f}%  dd={t.test_drawdown:.2f}%  final_pos={t.final_position_pct:.0f}%  nav={t.total_nav:.0f}")
    print(f"  slope={sl}  bias={bi}")

    qh = t.quarterly_holdings
    if qh:
        print(f"\n  逐季持仓明细 (窗口0):")
        for q in qh:
            qn = q["quarter"]
            qd = q["day"]
            qp = q["pos_pct"]
            qnav = q["nav"]
            qcash = q["cash"]
            print(f"    Q{qn}(d{qd}): pos={qp:.0f}%  nav={qnav:.0f}")
            for pos in q["positions"]:
                code = pos["code"]
                sh = pos["shares"]
                cb = pos["cost"]
                px = pos["price"]
                val = pos["value"]
                pnl = pos["pnl"]
                pnlp = pos["pnl_pct"]
                print(f"      {code} {sh:.0f}股 cost={cb:.2f} px={px:.2f} val={val:.0f} pnl={pnl:+.0f} ({pnlp:+.1f}%)")
            if not q["positions"]:
                print(f"      (空仓)")
            print(f"      现金: {qcash:.0f}")
else:
    print("  No strategies found")
print("DONE")
