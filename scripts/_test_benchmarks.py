"""Quick test: benchmark config loading and data availability."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.optimizer_constraints import load_constraints

c = load_constraints()

for group in ["a_share", "non_a_share"]:
    c.set_group(group)
    print(f"\n=== {group} ===")
    print(f"  benchmarks: {c.benchmark_codes}")
    print(f"  risk_free_rate: {c.risk_free_rate}")

    cache_dir = Path("cache/data")
    for bcode in c.benchmark_codes:
        if bcode == "risk_free":
            continue
        csv_path = cache_dir / f"{bcode}.csv"
        if csv_path.exists():
            bdf = pd.read_csv(csv_path, encoding="utf-8")
            print(f"  {bcode}: {len(bdf)} rows OK")
        else:
            print(f"  {bcode}: MISSING")
