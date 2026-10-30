"""Free, field-only completion of a supplied screening snapshot, never PDF/LLM."""

from __future__ import annotations

import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def derive_fields(records, quote, path):
    """Use the workbook's fixed periods/paths; missing adjusted ROE stays missing."""
    by_date = {}
    for row in records:
        day = str(row.get("REPORT_DATE", ""))[:10]
        # Keep the first (latest publication) for a period, not an arbitrary overwrite.
        by_date.setdefault(day, row)
    latest = by_date.get("2026-06-30", {})
    shares, price = number(quote.get("total_shares")), number(quote.get("price"))
    equity = shares * price if shares and shares > 0 and price and price > 0 else None

    def ttm(field):
        values = [
            number(by_date.get(d, {}).get(field))
            for d in ["2025-12-31", "2026-06-30", "2025-06-30"]
        ]
        return (
            values[0] + values[1] - values[2]
            if all(v is not None for v in values)
            else None
        )

    profit, adjusted = ttm("PARENTNETPROFIT"), ttm("KCFJCXSYJLR")
    bps = number(latest.get("BPS"))
    pb = price / bps if price and price > 0 and bps and bps > 0 else None
    roes = [
        number(by_date.get(f"{y}-12-31", {}).get("ROEKCJQ")) for y in range(2021, 2026)
    ]
    roes = [v / 100 for v in roes if v is not None]
    profits = [
        number(by_date.get(f"{y}-12-31", {}).get("KCFJCXSYJLR"))
        for y in range(2023, 2026)
    ]
    proxy = None
    if path == "PB" and len(roes) >= 3 and pb:
        proxy = statistics.median(roes) / pb
    if path == "PE" and equity and all(v is not None for v in profits):
        proxy = statistics.median(profits) / equity
    debt = number(latest.get("ZCFZL"))
    return {
        "market_cap_cny_100m": equity / 1e8 if equity else None,
        "pe_ttm": equity / profit if equity and profit else None,
        "adjusted_pe_ttm": equity / adjusted if equity and adjusted else None,
        "debt_assets": debt / 100 if debt is not None else None,
        "pb": pb,
        "earnings_yield_proxy": proxy,
        "path": path,
        "ttm_periods": ["2025-12-31", "2026-06-30", "2025-06-30"],
        "quote_date": quote.get("date"),
        "share_date": quote.get("date"),
        "ordinary_shares_basis": "Tencent quote total ordinary shares, same response as price",
        "adjusted_roe_years": len(roes),
    }


def evaluate(row):
    limits = {
        "1": ("market_cap_cny_100m", lambda x: x > 10),
        "2": ("pe_ttm", lambda x: 0 < x < 25),
        "3": ("adjusted_pe_ttm", lambda x: 0 < x < 25),
        "4": ("debt_assets", lambda x: x < 0.7),
        "5": ("earnings_yield_proxy", lambda x: x > 0.07),
    }
    checks = {}
    for key, (field, predicate) in limits.items():
        value = number(row.get(field))
        checks[key] = (
            "豁免"
            if key == "4" and row.get("leverage_exempt")
            else ("待核" if value is None else "通过" if predicate(value) else "未通过")
        )
    if row.get("manual_review_required"):
        checks["5"] = "待核"
    status = (
        "rejected"
        if "未通过" in checks.values()
        else ("pending" if "待核" in checks.values() else "passed")
    )
    return dict(
        row,
        status=status,
        checks=checks,
        missing_or_failed_checks=[
            k for k, v in checks.items() if v in {"待核", "未通过"}
        ],
    )


def fetch_fields(row, folder, wait):
    """One request per interface, raw bodies persisted before interpretation."""
    code = row["code"]
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    prefix = (
        "sh"
        if code.startswith("6")
        else "bj"
        if code.startswith(("4", "8", "9"))
        else "sz"
    )
    exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[prefix]
    wait()
    response = requests.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={
            "reportName": "RPT_F10_FINANCE_MAINFINADATA",
            "columns": "ALL",
            "filter": f'(SECUCODE="{code}.{exchange}")',
            "pageSize": 50,
            "pageNumber": 1,
            "sortColumns": "REPORT_DATE,NOTICE_DATE",
            "sortTypes": "-1,-1",
        },
        timeout=(10, 30),
    )
    (folder / "financial.response.txt").write_text(response.text, encoding="utf8")
    response.raise_for_status()
    records = (response.json().get("result") or {}).get("data") or []
    if not records or any(str(r.get("SECURITY_CODE")) != code for r in records):
        raise ValueError("financial_response_empty_or_code_mismatch")
    wait()
    response = requests.get(f"https://qt.gtimg.cn/q={prefix}{code}", timeout=(10, 20))
    (folder / "quote.response.bin").write_bytes(response.content)
    response.raise_for_status()
    parts = response.content.decode("gbk", errors="replace").split('"')[1].split("~")
    if len(parts) <= 73 or parts[2] != code or number(parts[3]) is None:
        raise ValueError("quote_response_invalid_or_code_mismatch")
    quote = {
        "price": number(parts[3]),
        "date": parts[30],
        "code": parts[2],
        "total_shares": number(parts[73]),
    }
    result = dict(
        row,
        **derive_fields(records, quote, row.get("path")),
        fields_checked_at=datetime.now(timezone.utc).isoformat(),
        field_source="eastmoney_f10_and_tencent; field-only, not PDF extraction",
    )
    # An absent workbook path is not replaced with a different universe valuation model.
    if result.get("path") not in {"PB", "PE"}:
        result["reason"] = "preset_industry_path_unresolved"
    return evaluate(result)


def complete_pending(snapshot, folder, stop, publish):
    """Cheap fields first; unresolved records never enter the deep queue."""
    folder = Path(folder)
    resolved = []
    next_request = 0.0

    def wait():
        nonlocal next_request
        if stop.wait(max(0, next_request - time.monotonic())):
            raise InterruptedError("stop requested")
        next_request = time.monotonic() + 5

    for row in snapshot["rows"]:
        if stop.is_set() or (folder.parent / "STOP").exists():
            raise InterruptedError("stop requested")
        if row["status"] != "pending":
            resolved.append(row)
            continue
        path = folder / row["code"] / "result.json"
        if path.exists():
            result = json.loads(path.read_text("utf8"))
        else:
            publish("screening_fields", row)
            try:
                result = fetch_fields(row, path.parent, wait)
            except InterruptedError:
                raise
            except requests.HTTPError as error:
                # Rate limits/auth bans stop the campaign; don't fan out more requests.
                if error.response.status_code in {401, 403, 429}:
                    raise RuntimeError(
                        f"screening_source_blocked_http_{error.response.status_code}"
                    ) from error
                result = dict(row, field_error=f"HTTP_{error.response.status_code}")
            except Exception as error:  # noqa: BLE001 - persist one cheap attempt, never invent fields
                result = dict(
                    row, field_error=f"{type(error).__name__}: {str(error)[:200]}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8"
            )
        resolved.append(result)
    return dict(
        snapshot,
        rows=resolved,
        field_completion_finished_at=datetime.now(timezone.utc).isoformat(),
    )
