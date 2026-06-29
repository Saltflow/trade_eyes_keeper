import sys
sys.path.insert(0, ".")
import pandas as pd
from datetime import datetime
from src.notification.email_notifier import build_strategy_suggestions

data = {
    "stock_code": ["601088", "601398", "000958", "601985"],
    "stock_name": ["神华", "工行", "电投", "核电"],
    "close": [39.57, 7.15, 5.60, 8.93],
    "open": [39.0, 7.10, 5.55, 8.90],
    "ma60": [37.0, 6.80, 5.50, 8.50],
    "date": ["2026-06-26"] * 4,
}
df = pd.DataFrame(data)

sug = build_strategy_suggestions(df, datetime.now())
if sug:
    label = sug["strategy_label"]
    active = sug["active_count"]
    total = sug["total_count"]
    print(f"Label: {label}")
    print(f"Active: {active}/{total}")
    for e in sug["entries"]:
        print(f"  {e['code']} {e['name']} {e['close']} signals={e['signals']}")
    print()
    print("HTML:")
    print(sug["html_rows"])
    print()
    print("Text:")
    print(sug["text_rows"])
else:
    print("No suggestions")
