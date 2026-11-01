"""Load explicitly identified Ke return artifacts without old-strategy fallback."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

CURVE_CONTRACT = "ke-return-curves/1"
STRATEGY_ID = "ke_threshold_allocation"
MARKETS = ("a_share", "hk", "us")


def configured_universe(config: dict) -> list[str]:
    result = []
    for item in config.get("stocks", []):
        code = (
            item.get("stock_code", item.get("code", ""))
            if isinstance(item, dict)
            else item
        )
        code = str(code).strip()
        if code and code not in result:
            result.append(code)
    return result


def unavailable_curves(reason: str) -> dict:
    return {
        "contract": CURVE_CONTRACT,
        "strategy_id": STRATEGY_ID,
        "markets": {
            group: {"coverage": {"status": "unavailable", "message": reason}}
            for group in MARKETS
        },
    }


def load_return_curves(config: dict, *, today: date | None = None) -> dict:
    """Fail closed on stale, wrong-universe, future or malformed result files.

    This reads local completed simulations only. It never promotes a strategy,
    scans orders, recalculates old technical strategies or mutates an account.
    """
    path = (config.get("report", {}) or {}).get("return_curve_artifact")
    if not path:
        return unavailable_curves("Ke收益曲线尚未生成")
    current = today or datetime.now(timezone.utc).date()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("curve payload")
        if (
            payload.get("contract") != CURVE_CONTRACT
            or payload.get("strategy_id") != STRATEGY_ID
        ):
            raise ValueError("curve contract")
        if set(map(str, payload.get("universe", []))) != set(
            configured_universe(config)
        ):
            raise ValueError("curve universe")
        generated = datetime.fromisoformat(payload["generated_at"])
        if generated.tzinfo is None or generated.date() > current:
            raise ValueError("curve generation date")
        max_age = int(
            (config.get("report", {}) or {}).get("return_curve_max_age_days", 7)
        )
        if max_age < 0 or (current - generated.date()).days > max_age:
            raise ValueError("stale curve artifact")
        markets = payload["markets"]
        if not isinstance(markets, dict) or set(markets) != set(MARKETS):
            raise ValueError("curve markets")
        for item in markets.values():
            if not isinstance(item, dict) or not isinstance(item.get("coverage"), dict):
                raise TypeError("curve market schema")
            status = item["coverage"]["status"]
            if status not in {"complete", "partial", "unavailable"}:
                raise ValueError("curve coverage")
            if status == "unavailable":
                continue
            if not isinstance(item.get("dates"), list) or not isinstance(
                item.get("values"), list
            ):
                raise TypeError("curve axes schema")
            days = [date.fromisoformat(stamp) for stamp in item["dates"]]
            values = item["values"]
            capital = float(item["initial_capital"])
            if not math.isfinite(capital) or capital <= 0:
                raise ValueError("curve capital")
            if len(days) != len(values) or not 2 <= len(days) <= 10000:
                raise ValueError("curve axes")
            if any(a >= b for a, b in zip(days, days[1:])) or days[-1] > current:
                raise ValueError("curve dates")
            if (current - days[-1]).days > max_age:
                raise ValueError("stale curve observations")
            if any(
                isinstance(v, bool) or not math.isfinite(float(v)) or float(v) <= 0
                for v in values
            ):
                raise ValueError("curve nav")
            if item.get("data_as_of") != days[-1].isoformat():
                raise ValueError("curve cutoff")
        return payload
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return unavailable_curves("Ke收益曲线数据不就绪或已过期")
