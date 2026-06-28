import pandas as pd
import numpy as np
from pathlib import Path

df = pd.read_csv("cache/data/000958.csv", encoding="utf-8")
df["date"] = pd.to_datetime(df["date"])
df["ds"] = df["date"].dt.strftime("%Y-%m-%d")
df = df.set_index("ds")

# Get dates from preview
all_stocks_dates = set(df.index.tolist())
print(f"Stock dates count: {len(all_stocks_dates)}")
print(f"First: {sorted(all_stocks_dates)[0]}")
print(f"Last: {sorted(all_stocks_dates)[-1]}")

# Check common dates with another stock
df2 = pd.read_csv("cache/data/601728.csv", encoding="utf-8")
df2["date"] = pd.to_datetime(df2["date"])
df2["ds"] = df2["date"].dt.strftime("%Y-%m-%d")
df2_dates = set(df2["ds"].tolist())
common = all_stocks_dates & df2_dates
print(f"Common with 601728: {len(common)} dates")

# With a short-history stock
df3 = pd.read_csv("cache/data/00883.csv", encoding="utf-8")
df3["date"] = pd.to_datetime(df3["date"])
df3["ds"] = df3["date"].dt.strftime("%Y-%m-%d")
df3_dates = set(df3["ds"].tolist())
common3 = all_stocks_dates & df2_dates & df3_dates
print(f"Common with 601728+00883: {len(common3)} dates")
print(f"Range: {sorted(common3)[0]} to {sorted(common3)[-1]}")

# Test date matching
dates_sorted = sorted(common3)[-365:]
d = dates_sorted[0]
print(f"\nTest: checking {d} in df.index: {d in df.index}")
print(f"Sample df index entries: {list(df.index[:3])}")

# Build indicator
roll = df["close"].rolling(60).mean()
for t, d in enumerate(dates_sorted[:5]):
    if d in df.index:
        row = df.loc[d]
        c = float(row["close"])
        ma = float(roll.loc[d]) if d in roll.index and not np.isnan(roll.loc[d]) else c
        dev = (c - ma) / ma if ma > 0 else 0
        print(f"  {d}: close={c:.2f}, ma={ma:.2f}, dev={dev:.4f}")
    else:
        print(f"  {d}: NOT IN INDEX")
