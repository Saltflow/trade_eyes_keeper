"""Parse screenshot facts and perform conservative, source-aware plausibility checks.

These checks establish internal consistency, not proof of broker ownership. No
network call happens unless the caller supplies a quote loader.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def detect_image_mime(value: bytes) -> str:
    if not isinstance(value, bytes):
        return ""
    if value.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if value.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if value.startswith(b"RIFF") and value[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _number(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _moment(value, zone=SHANGHAI) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        if re.fullmatch(r"\d{14}", value):
            result = datetime.fromisoformat(
                f"{value[:4]}-{value[4:6]}-{value[6:8]}T{value[8:10]}:{value[10:12]}:{value[12:14]}"
            )
        else:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.replace(tzinfo=zone) if result.tzinfo is None else result


def _quote_time_issues(code, as_of, snapshot, quote, stamp, now, max_age, resolver):
    """Check closed-session claims against an independent exchange calendar."""
    from ...personal_monitor_runtime import exchange_state, instrument_identity

    _code, _currency, exchange, timezone = instrument_identity(code)
    zone = ZoneInfo(timezone)
    if quote.get("market_timezone", timezone) != timezone:
        return ["quote_market_timezone_mismatch"]
    date_only = isinstance(as_of, str) and bool(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of)
    )
    selected = (
        datetime.fromisoformat(as_of + "T23:59:59.999999").replace(tzinfo=zone)
        if date_only
        else snapshot
    )
    if date_only and selected.date() > now.astimezone(zone).date():
        return ["snapshot_time_in_future"]
    historical = date_only or (now - selected).total_seconds() > max_age
    # The provider's self-reported latest_trading_date is not sufficient. Resolve
    # the actual exchange session independently, at the screenshot's time.
    state = (resolver or exchange_state)(exchange, min(selected, now))
    if state["market_open"]:
        if quote.get("market_closed") is True:
            return ["quote_market_state_mismatch"]
        if date_only or "daily" in str(quote.get("source", "")).lower():
            return ["historical_intraday_evidence_required"]
        if not historical and (now - stamp).total_seconds() > max_age:
            return ["quote_stale"]
        return (
            []
            if abs((selected - stamp).total_seconds()) <= max_age
            else ["historical_quote_time_mismatch"]
        )
    close = state["session_close"]
    if isinstance(close, str):
        close = _moment(close)
    if not isinstance(close, datetime) or close.tzinfo is None:
        return ["closed_market_calendar_unavailable"]
    session = state["latest_completed_session"]
    if (
        quote.get("market_closed") is not True
        or quote.get("market_timezone") != timezone
        or quote.get("latest_trading_date") != session
        or stamp.astimezone(zone).date().isoformat() != session
        or close.astimezone(zone).date().isoformat() != session
        or close > min(selected, now)
        or stamp < close - timedelta(minutes=15)
    ):
        return ["closed_market_quote_date_unverified"]
    # When metadata is supplied, it must agree with the independently resolved
    # close; malformed or contradictory metadata must never gain trust.
    if (
        quote.get("session_close") is not None
        and _moment(quote["session_close"]) != close
    ):
        return ["closed_market_quote_date_unverified"]
    return []


def _normalize_units(row: dict) -> list[str]:
    """Convert only explicitly identified units and retain the extracted values."""
    issues = []
    scales = {
        "quantity": {
            "": 1,
            "股": 1,
            "份": 1,
            "shares": 1,
            "万股": 10000,
            "万份": 10000,
        },
        "price": {
            "": 1,
            "元": 1,
            "港元": 1,
            "美元": 1,
            "分": Decimal("0.01"),
            "cents": Decimal("0.01"),
        },
        "market_value": {
            "": 1,
            "元": 1,
            "港元": 1,
            "美元": 1,
            "万元": 10000,
            "万港元": 10000,
            "万美元": 10000,
        },
    }
    for field, options in scales.items():
        unit = row.get(field + "_unit") or ""
        if not isinstance(unit, str) or unit not in options:
            issues.append("unsupported_" + field + "_unit")
            continue
        if ("港元" in unit and row.get("currency") != "HKD") or (
            "美元" in unit and row.get("currency") != "USD"
        ):
            issues.append("unit_currency_mismatch")
            continue
        value = _number(row.get(field))
        multiplier = Decimal(options[unit])
        if value is not None and multiplier != 1:
            row.setdefault("raw_values", {})[field] = {
                "value": row[field],
                "unit": unit,
            }
            row[field] = float(value * multiplier)
            # Subsequent validation must not multiply the same row twice.
            row[field + "_unit"] = "股" if field == "quantity" else "元"
            if field == "price":
                decimals = row.get("price_decimals", 2)
                if isinstance(decimals, int) and not isinstance(decimals, bool):
                    row["price_decimals"] = min(8, decimals + 2)
    return issues


def parse_position_document(value: str | dict) -> dict:
    """Strict JSON envelope; missing fields remain absent instead of inferred."""
    if isinstance(value, str):
        # A single Markdown fence is accepted; explanatory prose is not.
        raw = value.strip()
        if raw.startswith("```json\n") and raw.endswith("```"):
            raw = raw[8:-3].strip()
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("截图识别结果必须为 JSON 对象。")  # noqa: TRY004
    result = copy.deepcopy(value)
    for field in ("positions", "reminders", "ambiguities"):
        if field not in result:
            result[field] = []
        if not isinstance(result[field], list):
            raise ValueError(f"截图字段 {field} 必须为数组。")  # noqa: TRY004
    if len(result["positions"]) > 300 or len(result["reminders"]) > 100:
        raise ValueError("截图识别条目超过单批上限。")
    if any(not isinstance(row, dict) for row in result["positions"]):
        raise ValueError("持仓行必须为对象。")
    if any(not isinstance(row, dict) for row in result["reminders"]):
        raise ValueError("提醒行必须为对象。")
    result["issues"] = []
    return result


def confirm_unscaled_quantity_units(document, user_text, message_id):
    """Apply an authenticated user confirmation to missing units only.

    The model resolves the user's confirmation. It cannot supply new quantities,
    multiply a number, replace a contradictory visible unit, or clear another
    uncertain field. Normal arithmetic, quotes and snapshot guards still run.
    """
    if (
        not isinstance(user_text, str)
        or not user_text.strip()
        or not isinstance(message_id, str)
        or not message_id.strip()
    ):
        raise ValueError("数量单位确认必须来自用户消息")
    result = copy.deepcopy(document)
    for row in result.get("positions", []):
        asset = row.get("asset_type")
        if asset not in {"equity", "etf", "reit"} or row.get("quantity_unit"):
            continue
        quantity, price, value = (
            _number(row.get(field)) for field in ("quantity", "price", "market_value")
        )
        if quantity is None or price is None or value is None:
            continue
        row["quantity_unit"] = "股" if asset == "equity" else "份"
        row["quantity_unit_declaration"] = {
            "message_id": message_id,
            "user_text": user_text,
            "source": "authenticated_user_confirmation",
        }
        row["uncertain_fields"] = [
            field
            for field in row.get("uncertain_fields", [])
            if field != "quantity_unit"
        ]
        row["ambiguities"] = [
            item
            for item in row.get("ambiguities", [])
            if not (isinstance(item, str) and re.match(r"^quantity_unit\s*[:：]", item))
        ]
    return result


def validate_position_document(
    document: str | dict,
    *,
    quote_loader=None,
    now: datetime | None = None,
    max_quote_age_seconds: int = 900,
    price_tolerance: float = 0.15,
    calendar_resolver=None,
) -> dict:
    """Validate rows independently. Quote loader receives (code, as_of).

    Quotes must include price, currency, and actual provider time/time stamp.
    Historical/closed quotes are checked against an independently resolved
    exchange session, using its market timezone rather than Shanghai's date.
    """
    result = parse_position_document(document)
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    as_of = result.get("as_of")
    snapshot = _moment(as_of)
    if snapshot is None:
        result["issues"].append("snapshot_time_required")
    elif snapshot > now + timedelta(minutes=5):
        result["issues"].append("snapshot_time_in_future")
    for row in result["positions"]:
        issues = _normalize_units(row)
        code = row.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,24}", code):
            issues.append("instrument_code_required")
        quantity = _number(row.get("quantity"))
        price = _number(row.get("price"))
        value = _number(row.get("market_value"))
        currency = row.get("currency")
        if row.get("market_value_currency", currency) != currency:
            issues.append("mixed_price_value_currencies")
        if row.get("asset_type") == "reverse_repo":
            # Nominal reverse-repo accounting must not pass through an equity
            # quote provider or become a stock-price threshold observation.
            issues.append("non_equity_asset_pending")
        explicit_zero = quantity == 0 and value == 0
        if quantity is None or quantity < 0:
            issues.append("invalid_quantity")
        if not explicit_zero and (price is None or price <= 0):
            issues.append("invalid_price")
        if value is None or value < 0:
            issues.append("invalid_market_value")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            issues.append("currency_required")
        if quantity is not None and price is not None and value is not None:
            # Price displayed to 2 decimals may contribute half-cent per share;
            # market value displayed to cents contributes another half-cent.
            precision = row.get("price_decimals", 2)
            if not isinstance(precision, int) or not 0 <= precision <= 8:
                precision = 2
            tolerance = abs(quantity) * Decimal(10) ** (-precision) / 2 + Decimal(
                "0.01"
            )
            if abs(quantity * price - value) > tolerance:
                issues.append("quantity_price_value_mismatch")
        if row.get("ambiguities") or row.get("uncertain_fields"):
            issues.append("ambiguous_fields")
        if result["issues"]:
            issues.extend(result["issues"])
        if not issues and not explicit_zero:
            if quote_loader is None:
                issues.append("quote_verification_unavailable")
            else:
                try:
                    quote = quote_loader(code, as_of)
                    if not isinstance(quote, dict):
                        raise TypeError("missing quote")
                    observed = _number(quote.get("price"))
                    from ...personal_monitor_runtime import instrument_identity

                    zone = ZoneInfo(instrument_identity(code)[3])
                    stamp = _moment(
                        quote.get("provider_time")
                        or quote.get("timestamp")
                        or quote.get("time"),
                        zone,
                    )
                    if quote.get("currency") != currency:
                        issues.append("quote_currency_mismatch")
                    if (
                        not isinstance(quote.get("source"), str)
                        or not quote["source"].strip()
                    ):
                        issues.append("quote_source_required")
                    if stamp is None or stamp > now + timedelta(minutes=5):
                        issues.append("quote_timestamp_invalid")
                    else:
                        issues.extend(
                            _quote_time_issues(
                                code,
                                as_of,
                                snapshot,
                                quote,
                                stamp,
                                now,
                                max_quote_age_seconds,
                                calendar_resolver,
                            )
                        )
                    if observed is None or observed <= 0:
                        issues.append("quote_price_invalid")
                    elif abs(price - observed) / observed > Decimal(
                        str(price_tolerance)
                    ):
                        issues.append("quote_price_mismatch")
                    row["quote_evidence"] = {
                        key: quote[key]
                        for key in (
                            "price",
                            "currency",
                            "provider_time",
                            "timestamp",
                            "time",
                            "source",
                            "market_timezone",
                            "market_closed",
                            "latest_trading_date",
                            "session_close",
                            "calendar_as_of",
                            "session_verified",
                        )
                        if key in quote
                    }
                except Exception:  # noqa: BLE001 - an external quote adapter must fail closed
                    issues.append("quote_verification_unavailable")
        row["issues"] = list(dict.fromkeys(issues))
        row["importable"] = not row["issues"]
    return result
