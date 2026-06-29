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

# Trim to last 5 years (1260 trading days)
LOOKBACK_DAYS = 1260
stocks_data = {
    code: df.tail(LOOKBACK_DAYS) if len(df) > LOOKBACK_DAYS else df
    for code, df in stocks_data.items()
}
print(f"A-shares: {len(stocks_data)} stocks: {list(stocks_data.keys())}")
print(f"Date range: {stocks_data[list(stocks_data.keys())[0]]['date'].iloc[0].strftime('%Y-%m-%d')} -> "
      f"{stocks_data[list(stocks_data.keys())[0]]['date'].iloc[-1].strftime('%Y-%m-%d')}")

from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2

opt = StrategyOptimizerV2(stocks_data, "a_share")
print(f"Running optimizer ({len(stocks_data)} stocks, 20000 samples)...")
t0 = time.time()
report = opt.run(
    stock_codes=list(stocks_data.keys()),
    random_starts=20000, iterations=20000,
)
elapsed = time.time() - t0

print(f"\n{'='*62}")
print(f"Elapsed: {elapsed:.0f}s  |  Top strategies: {len(report.top_strategies)}")
print(f"{'='*62}")

if report.top_strategies:
    for i, t in enumerate(report.top_strategies[:3]):
        ex_880 = t.test_return
        ex_300 = round(t.strategy_return - t.benchmark_returns.get('510300', 0), 2)
        ex_rf = round(t.strategy_return - t.benchmark_returns.get('risk_free', 0), 2)
        print(f"  #{i+1}: strategy={t.strategy_return:+.1f}%  "
              f"vs510880={ex_880:+.1f}%  vs510300={ex_300:+.1f}%  vsRF={ex_rf:+.1f}%")
        print(f"       dd={t.test_drawdown:.2f}%  trades={t.trade_count}  "
              f"final_pos={t.final_position_pct:.0f}%  nav={t.total_nav:.0f}")
    t1 = report.top_strategies[0]
    print(f"\n  Benchmarks (w0): {t1.benchmark_returns}")
    print(f"  Top1 params: {dict(list(t1.params.items())[:6])}")

    # 期末持仓明细
    if t1.final_holdings:
        print(f"\n  {'期末持仓明细 (测试期末)':-^60}")
        print(f"  {'代码':>8} {'持股':>8} {'成本价':>8} {'现价':>8} {'市值':>10} {'盈亏':>10} {'盈亏%':>8}")
        print(f"  {'-'*60}")
        total_unrealized = 0.0
        for h in t1.final_holdings:
            if h['shares'] <= 0:
                continue
            cost_val = h.get('cost_value', 0)
            pnl = h['value'] - cost_val
            pnl_pct = (h['price'] / h['cost'] - 1) * 100 if h.get('cost', 0) > 0 else 0.0
            total_unrealized += pnl
            print(f"  {h['code']:>8} {h['shares']:>6.0f}股 {h['cost']:>7.2f} {h['price']:>7.2f} "
                  f"{h['value']:>9.0f} {pnl:>+9.0f} {pnl_pct:>+7.1f}%")
        print(f"  {'-'*60}")
        print(f"  {'现金':>8} {'':>8} {'':>8} {'':>8} {t1.final_cash:>9.0f}")
        print(f"  {'总资产':>8} {'':>8} {'':>8} {'':>8} {t1.total_nav:>9.0f}  "
              f"浮动盈亏: {total_unrealized:+.0f}")
        print(f"  {'='*60}")
else:
    print("  No strategies passed constraints")
print(f"{'='*62}")
