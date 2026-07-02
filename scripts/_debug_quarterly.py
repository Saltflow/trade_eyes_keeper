"""Debug WalkForwardManager price matrix column mapping + quarterly NaN issue."""
import sys
sys.path.insert(0, ".")
import pandas as pd, numpy as np
from pathlib import Path

stocks_data = {}
for code in ["601728", "600938", "601985"]:
    df = pd.read_csv(f"cache/data/{code}.csv", encoding="utf-8")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    stocks_data[code] = df
    c_min = df["close"].min()
    c_max = df["close"].max()
    print(f"{code}: {len(df)} rows, close range {c_min:.2f}-{c_max:.2f}")

from src.analysis.walk_forward import WalkForwardManager
wf = WalkForwardManager(stocks_data)
print(f"\nn_dates={wf.n_dates}")
print(f"stock_codes={wf.stock_codes}")

ws_list = wf.iter_windows()
print(f"windows={len(ws_list)}")

# Check each window's last test day close
for ws in ws_list[-3:]:
    pm = wf.get_price_matrix(ws, "test")
    T, N = pm.shape
    print(f"\nWindow {ws.window_id}: {T}d x {N} stocks")
    print(f"  dates: {wf._unified_dates[ws.test_start_idx]} -> {wf._unified_dates[ws.test_end_idx-1]}")
    for i, code in enumerate(wf.stock_codes):
        col = pm[:, i]
        valid = col[~np.isnan(col)]
        if len(valid) > 0:
            print(f"  col[{i}]={code}: valid={len(valid)}/{T}, min={valid.min():.2f}, max={valid.max():.2f}, first={valid[0]:.2f}, last={valid[-1]:.2f}")
        else:
            print(f"  col[{i}]={code}: ALL NAN ({T} days)")

# Test the quarterly snapshot logic
from src.analysis.fast_evaluator import FastEvaluator
ev = FastEvaluator(initial_cash=100000, monthly_buy_limit=100000, lot_size=100)

# Use window 0 (easiest to trace)
ws0 = ws_list[-1]  # last window for max position buildup
test_ind = wf.build_matrices(ws0, "test")
test_price = wf.get_price_matrix(ws0, "test")
cash_bl = np.ones(test_price.shape[0]) * 100000

# Quick check: what are the actual close prices in the test window?
print(f"\n=== Price check: window {ws0.window_id} ===")
for i, code in enumerate(wf.stock_codes):
    valid = test_price[:, i][~np.isnan(test_price[:, i])]
    if len(valid) > 0:
        print(f"  {code}: {valid[0]:.2f} -> {valid[-1]:.2f} ({len(valid)} valid days)")
    else:
        print(f"  {code}: ALL NaN")

# Evaluate with a generic strategy
import yaml
from pathlib import Path as P
cfg_path = P("config/optimizer_constraints.yaml")
with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
cfg["discrete_search"]["mode"] = "position_target"
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

stats = ev.evaluate_position_target(
    test_ind, test_price, cash_bl,
    buy_builders=["deviation_absolute", "deep_value"],
    buy_thresholds=[0.3, 0.0],
    position_slope=4.0, position_bias=3.0,
)

cfg["discrete_search"]["mode"] = "frac"
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print(f"\n=== Quarterly snapshot data ===")
q_sh = stats.quarter_shares
q_px = stats.quarter_prices
q_cs = stats.quarter_cash
q_nv = stats.quarter_nav
if q_sh is not None:
    NQ, NN = q_sh.shape
    print(f"shape: {NQ} quarters x {NN} stocks")
    interval = max(1, test_price.shape[0] // NQ)
    for qi in range(NQ):
        day = (qi + 1) * interval
        print(f"  Q{qi+1}(d{day}):")
        for i, code in enumerate(wf.stock_codes):
            sh = q_sh[qi, i]
            px = q_px[qi, i]
            if sh > 0.5:
                print(f"    {code}: {sh:.0f}sh @ px={px:.2f} (nan={np.isnan(px)})")
            elif not np.isnan(px) and px > 0:
                pass  # no shares but price exists
        print(f"    cash={q_cs[qi]:.0f} nav={q_nv[qi]:.0f}")

print("\nDONE")
