"""Full optimizer test with benchmarks — A-shares only, cached data."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd, yaml

with open("config/config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

config_stocks = [str(s) for s in config.get("stocks", [])]
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

print(f"A-shares: {len(stocks_data)} stocks: {list(stocks_data.keys())}")

from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
opt = StrategyOptimizerV2(stocks_data, "a_share")
print(f"Running optimizer ({len(stocks_data)} stocks, 20000 samples)...")
t0 = time.time()
report = opt.run(stock_codes=list(stocks_data.keys()), random_starts=20000, iterations=20000)
elapsed = time.time() - t0

print(f"\n{'='*62}")
print(f"Elapsed: {elapsed:.0f}s  |  Top strategies: {len(report.top_strategies)}")
print(f"{'='*62}")

if report.top_strategies:
    for i, t in enumerate(report.top_strategies[:3]):
        ex_880 = t.test_return
        ex_300 = round(t.strategy_return - t.benchmark_returns.get("510300", 0), 2)
        ex_rf = round(t.strategy_return - t.benchmark_returns.get("risk_free", 0), 2)
        md = t.params.get("_mode", "?")
        desc = getattr(t, "strategy_description", "")
        print(f"\n{'─'*62}")
        print(f"  #{i+1}: strategy={t.strategy_return:+.1f}%  vs880={ex_880:+.1f}%  "
              f"vs300={ex_300:+.1f}%  vsRF={ex_rf:+.1f}%")
        print(f"       dd={t.test_drawdown:.2f}%  trades={t.trade_count}  "
              f"final_pos={t.final_position_pct:.0f}%  nav={t.total_nav:.0f}  mode={md}")
        if desc:
            print(f"\n      策略概要:")
            for line in desc.split("\n"):
                print(f"      {line}")

    t1 = report.top_strategies[0]
    print(f"\n  Benchmarks (w0): {t1.benchmark_returns}")

    # 季度持仓
    qh = t1.quarterly_holdings
    if qh:
        print(f"\n  {'季末持仓明细':─^50}")
        for q in qh:
            qn, qd, qp, qnv, qcs = q["quarter"], q["day"], q["pos_pct"], q["nav"], q["cash"]
            print(f"  Q{qn}(d{qd}): pos={qp:.0f}%  nav={qnv:.0f}")
            for pos in q["positions"]:
                print(f"    {pos['code']} {pos['shares']:.0f}股  "
                      f"cost={pos['cost']:.2f}  px={pos['price']:.2f}  "
                      f"val={pos['value']:.0f}  pnl={pos['pnl']:+.0f}({pos['pnl_pct']:+.1f}%)")
            if not q["positions"]:
                print(f"    (空仓)")
            print(f"    现金: {qcs:.0f}")

else:
    print("  No strategies passed constraints")
print(f"{'='*62}")
