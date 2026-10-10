"""Explicit cash currency contract; foreign dividends use causal HKMA valuation.

This is an ex-date HKD valuation, not an assertion of issuer-paid HKD cash.
Historical HKMA observations are preserved as retrieved, not a vintage dataset.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

CORPORATE_ACTION_CURRENCY_CONTRACT = "declared-cash+causal-hkma-valuation/1"
HKMA_FX_URL = "https://api.hkma.gov.hk/public/market-data-and-statistics/monthly-statistical-bulletin/er-ir/er-eeri-daily"


def native_currency(code: str) -> str:
    if re.fullmatch(r"\d{5}", str(code)):
        return "HKD"
    if re.fullmatch(r"\d{6}", str(code)):
        return "CNY"
    return "USD"


def validate_cash_currency(action, expected: str | None = None) -> None:
    if getattr(action, "cash_per_share", None) is None:
        return
    expected = expected or native_currency(action.code)
    currency = getattr(action, "currency", None)
    # Legacy domestic/US records predate explicit currency; their native
    # contract is unchanged. HK cash must be explicit after this repair.
    if currency is None and expected != "HKD":
        return
    if currency != expected:
        raise ValueError(
            f"cash currency mismatch: {action.code} {currency} != {expected}"
        )
    declared = getattr(action, "declared_currency", None)
    if not declared or declared == currency:
        return
    proof = getattr(action, "cash_conversion", None) or {}
    try:
        rate = float(proof["rate"])
        day = date.fromisoformat(proof["rate_date"])
        original = float(action.declared_cash_per_share)
        valid = (
            declared == "USD"
            and currency == "HKD"
            and proof.get("from_currency") == declared
            and proof.get("to_currency") == currency
            and proof.get("method") == CORPORATE_ACTION_CURRENCY_CONTRACT
            and proof["source_url"].startswith(HKMA_FX_URL + "?")
            and proof["source_record"]["end_of_day"] == proof["rate_date"]
            and float(proof["source_record"]["usd"]) == rate
            and re.fullmatch(r"[a-f0-9]{64}", proof["source_sha256"])
            and 0 < (action.ex_date - day).days <= 7
            and math.isfinite(rate)
            and rate > 0
            and original > 0
            and math.isfinite(original)
            and math.isclose(
                float(action.cash_per_share), original * rate, rel_tol=1e-12
            )
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(
            f"invalid foreign cash valuation evidence: {action.code} {action.ex_date}"
        )


class HkmaCashValuation:
    def __init__(self, config=None, http=None):
        settings = (config or {}).get("point_in_time_data", {}) or {}
        self.root = Path(settings.get("output_dir", "data/point_in_time")) / "hkma_fx"
        self.http = http or requests.Session()
        self.timeout = float(settings.get("hkma_fx_timeout_seconds", 20))

    def quote(self, ex_date: date) -> dict:
        start, end = ex_date - timedelta(days=7), ex_date - timedelta(days=1)
        params = {
            "choose": "end_of_day",
            "from": start.isoformat(),
            "to": end.isoformat(),
            "fields": "end_of_day,usd",
            "pagesize": 100,
        }
        url = requests.Request("GET", HKMA_FX_URL, params=params).prepare().url
        path = self.root / (ex_date.isoformat() + ".json")
        if path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
            raw = stored["body"]
            if (
                stored.get("url") != url
                or stored.get("sha256") != hashlib.sha256(raw.encode()).hexdigest()
            ):
                raise ValueError("HKMA cached response integrity mismatch")
        else:
            block_path = self.root / "provider_block.json"
            now = datetime.now(timezone.utc)
            if block_path.exists():
                blocked = json.loads(block_path.read_text(encoding="utf-8"))
                until = datetime.fromisoformat(blocked["retry_after"])
                if now < until:
                    raise ValueError(
                        "HKMA provider blocked; retry_after=" + blocked["retry_after"]
                    )
            response = self.http.get(url, timeout=self.timeout)
            if getattr(response, "status_code", None) in {403, 405, 429}:
                self.root.mkdir(parents=True, exist_ok=True)
                blocked = {
                    "status": "blocked",
                    "http_status": response.status_code,
                    "source_url": url,
                    "observed_at": now.isoformat(),
                    "retry_after": (now + timedelta(hours=6)).isoformat(),
                    "response_sha256": hashlib.sha256(
                        response.text.encode()
                    ).hexdigest(),
                    "reason": "official_provider_refused_request_no_alternate_host_or_rate_guess",
                }
                block_path.write_text(json.dumps(blocked, indent=2), encoding="utf-8")
                raise ValueError(
                    "HKMA provider blocked; retry_after=" + blocked["retry_after"]
                )
            response.raise_for_status()
            raw = response.text
        payload = json.loads(raw)
        if payload.get("header", {}).get("success") is not True:
            raise ValueError("HKMA FX request was not successful")
        records = payload.get("result", {}).get("records", [])
        if (
            not records
            or len(records) != payload["result"].get("datasize")
            or len(records) >= 100
        ):
            raise ValueError("HKMA FX response missing or potentially truncated")
        dates = [date.fromisoformat(r["end_of_day"]) for r in records]
        if len(set(dates)) != len(dates) or any(not start <= d <= end for d in dates):
            raise ValueError("HKMA ignored requested dates or duplicated observations")
        row = max(records, key=lambda r: r["end_of_day"])
        rate = float(row["usd"])
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("HKMA USD rate is invalid")
        sha = hashlib.sha256(raw.encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"url": url, "sha256": sha, "body": raw}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
            temporary.replace(path)
        return {
            "from_currency": "USD",
            "to_currency": "HKD",
            "rate": rate,
            "rate_date": row["end_of_day"],
            "source_url": url,
            "source_sha256": sha,
            "method": CORPORATE_ACTION_CURRENCY_CONTRACT,
            "basis": "previous_observed_day_HKD_valuation_not_actual_HKD_receipt",
            "source_record": row,
        }

    def convert(self, action):
        if action.currency == "HKD":
            validate_cash_currency(action, "HKD")
            return action
        if action.currency != "USD" or action.published_at is None:
            raise ValueError(
                f"unsupported or unverified foreign cash currency: {action.code} {action.currency}"
            )
        proof = self.quote(action.ex_date)
        result = deepcopy(action)
        result.declared_cash_per_share = action.cash_per_share
        result.declared_currency = action.currency
        result.cash_per_share = action.cash_per_share * proof["rate"]
        result.currency = "HKD"
        result.cash_conversion = proof
        result.diagnostics.append(
            "cash_is_HKD_ex_date_valuation_not_issuer_HKD_payment"
        )
        validate_cash_currency(result, "HKD")
        return result
