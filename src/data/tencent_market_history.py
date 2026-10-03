"""Complete raw and forward-adjusted daily bars from Tencent's public feed."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

from src.instruments.classifier import detect_market


class TencentMarketHistoryProvider:
    """Fetch matching windows; never substitute raw bars for missing qfq bars.

    Corporate actions are supplied independently by dated disclosure feeds.
    Calling code must reconcile those before accepting this price bundle.
    """

    URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

    def __init__(self, config: dict | None = None, http=None):
        config = config or {}
        self.http = http or requests.Session()
        self.timeout = int(
            (config.get("instrument_audit", {}) or {}).get("timeout_seconds", 20)
        )
        settings = (config.get("point_in_time_data", {}) or {}).get(
            "market_history", {}
        ) or {}
        self.interval = max(
            2.0, float(settings.get("tencent_request_interval_seconds", 2.0))
        )
        self._last_request = 0.0

    @staticmethod
    def symbol(code: str) -> str:
        code = str(code).strip()
        market = detect_market(code)
        if market == "hk":
            return "hk" + code.zfill(5)
        if market == "a_share" and code.isdigit() and len(code) == 6:
            return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
        raise ValueError(f"Tencent daily history does not support {code}")

    def _window(self, symbol: str, start: date, end: date, adjustment: str):
        remaining = self.interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()
        response = self.http.get(
            self.URL,
            params={"param": f"{symbol},day,{start},{end},800,{adjustment}"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        payload = body.get("data") if isinstance(body, dict) else None
        if (
            not isinstance(body, dict)
            or body.get("code") != 0
            or not isinstance(payload, dict)
        ):
            raise ValueError(
                f"Tencent rejected history window for {symbol}: {start}~{end}"
            )
        details = payload.get(symbol)
        if not isinstance(details, dict):
            raise TypeError(f"Tencent omitted instrument {symbol}")
        key = "qfqday" if adjustment else "day"
        # Some unadjusted instruments return 'day' for both requests. Exact
        # dates/volume still have to agree, and action coverage remains pending.
        rows = details.get(key)
        if rows is None and adjustment:
            rows = details.get("day")
        if not isinstance(rows, list):
            raise TypeError(f"Tencent omitted {key} bars for {symbol}")
        return rows

    def fetch(self, code: str, start: date, end: date):
        from .market_history import CorporateAction, PriceHistoryBundle

        if end < start:
            raise ValueError("history end precedes start")
        symbol = self.symbol(code)
        sides: dict[str, dict[str, list]] = {"raw": {}, "qfq": {}}
        cursor = start
        while cursor <= end:
            stop = min(cursor + timedelta(days=729), end)
            for side, adjustment in (("raw", ""), ("qfq", "qfq")):
                for row in self._window(symbol, cursor, stop, adjustment):
                    if not isinstance(row, list) or len(row) < 6:
                        raise ValueError(f"Tencent malformed {side} bar for {code}")
                    day = date.fromisoformat(str(row[0]))
                    if not cursor <= day <= stop:
                        continue
                    key = day.isoformat()
                    existing = sides[side].get(key)
                    if existing is not None and existing != row:
                        raise ValueError(f"Tencent contradictory bars for {code} {day}")
                    sides[side][key] = row
            cursor = stop + timedelta(days=1)
        if not sides["raw"]:
            raise ValueError(f"Tencent returned no history for {code}")
        if sides["raw"].keys() != sides["qfq"].keys():
            raise ValueError(f"Tencent raw/qfq dates do not align for {code}")
        records = []
        for day in sorted(sides["raw"]):
            raw, qfq = sides["raw"][day], sides["qfq"][day]
            record = {"date": day}
            for side, row in (("raw", raw), ("qfq", qfq)):
                for index, name in enumerate(("open", "close", "high", "low"), 1):
                    number = float(row[index])
                    if not np.isfinite(number) or number <= 0:
                        raise ValueError(f"Tencent invalid {side}_{name}: {code} {day}")
                    record[f"{side}_{name}"] = number
            volume = float(raw[5])
            if not np.isfinite(volume) or volume < 0:
                raise ValueError(f"Tencent invalid volume: {code} {day}")
            if not np.isclose(volume, float(qfq[5])):
                raise ValueError(f"Tencent raw/qfq volume differs: {code} {day}")
            # The mainland feed reports lots of 100 shares; HK reports shares.
            record["volume"] = volume * (100 if symbol[:2] in {"sh", "sz"} else 1)
            record["tradable"] = volume > 0
            record["qfq_factor"] = record["qfq_close"] / record["raw_close"]
            records.append(record)
        return PriceHistoryBundle(
            code=str(code),
            prices=pd.DataFrame(records),
            actions=[
                CorporateAction(
                    code=str(code),
                    action_type="action_coverage_unverified",
                    ex_date=date.fromisoformat(min(sides["raw"])),
                    source="tencent_price_only",
                    diagnostics=["independent_disclosure_coverage_required"],
                )
            ],
            source="tencent_raw_qfq",
            currency="HKD" if symbol.startswith("hk") else "CNY",
            diagnostics=[
                "corporate_actions_require_independent_disclosures",
                "tencent_window_request:"
                + json.dumps(
                    {"symbol": symbol, "start": str(start), "end": str(end)},
                    sort_keys=True,
                ),
            ],
        ).validate()
