import sys, os, yaml
sys.path.insert(0, "/root/trade_eyes_keeper")
os.chdir("/root/trade_eyes_keeper")
from main import load_config
from src.data.data_source import DataSource
from src.analysis.portfolio_strategy import _detect_fine_group
config = load_config()
stocks = config.get("stocks", [])
us = [s for s in stocks if _detect_fine_group(str(s)) == "us"]
print("US codes:", us)
ds = DataSource(config)
for c in us:
    df = ds.fetch_stock_data(str(c), days=1095)
    n = len(df) if df is not None and not df.empty else 0
    print("  %s: %d days" % (c, n))
    if n > 0:
        # Check latest date
        print("    last date:", df["date"].iloc[-1])
