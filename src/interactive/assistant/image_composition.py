"""Join complementary screenshot columns using explicit shared facts only.

The caller must select recent batches belonging to the authenticated owner and
chat. This module neither establishes that authorization nor infers snapshot
times from message timestamps. It performs no arithmetic reconstruction, quote
lookup, alias discovery, validation, persistence or delivery.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

NUMERIC_FIELDS = frozenset({"quantity", "price", "market_value"})
FACT_FIELDS = (
    "code",
    "instrument",
    "name",
    "aliases",
    "quantity",
    "price",
    "price_decimals",
    "market_value",
    "currency",
    "market_value_currency",
    "quantity_unit",
    "price_unit",
    "market_value_unit",
    "asset_type",
    "market",
    "exchange",
    "as_of",
    "maturity_date",
    "term_days",
    "due_at",
)
TIME_FIELDS = ("as_of", "declared_as_of", "user_declared_as_of", "declared_time")
UNITS = ("quantity_unit", "price_unit", "market_value_unit")
ACCOUNT_FIELDS = ("declared_account", "confirmed_account", "account_id")


def _present(value):
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _text(value):
    return re.sub(r"\s+", "", value).casefold() if isinstance(value, str) else ""


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _hashes(value):
    if not isinstance(value, list):
        return []
    return sorted({item for item in value if isinstance(item, str) and item.strip()})


def _time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return ("date", date.fromisoformat(value).isoformat())
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return ("naive", moment.isoformat())
        return ("aware", moment.astimezone(timezone.utc).isoformat())
    except ValueError:
        return ("invalid", value)


def _document_time(document):
    values = [
        _time(document[field]) for field in TIME_FIELDS if _present(document.get(field))
    ]
    if any(value is None or value[0] == "invalid" for value in values):
        return False, None
    return (
        not values or all(value == values[0] for value in values),
        values[0] if values else None,
    )


def _account_values(document):
    # Stable identifiers can be case sensitive. Only surrounding/display
    # whitespace is ignored; generic display hints are handled separately.
    return {
        re.sub(r"\s+", "", document[field])
        if isinstance(document[field], str)
        else ("invalid", field)
        for field in ACCOUNT_FIELDS
        if _present(document.get(field))
    }


def _account_compatible(left, right):
    first, second = _account_values(left), _account_values(right)
    return (
        len(first) <= 1
        and len(second) <= 1
        and not any(not isinstance(value, str) for value in first | second)
        and first == second
    )


def _same_account(left, right):
    if not _account_compatible(left, right):
        return False
    if _account_values(left):
        return True
    hint = _text(left.get("account_hint"))
    return bool(hint) and hint == _text(right.get("account_hint"))


def _code(value):
    if not _present(value):
        return None
    if not isinstance(value, str):
        return ("invalid", str(value))
    value = value.strip().upper()
    if value.endswith(".HK"):
        stem = value[:-3]
        return (
            ("HK", stem.zfill(5))
            if stem.isdigit() and 1 <= len(stem) <= 5
            else ("invalid", value)
        )
    if re.fullmatch(r"\d{5}", value):
        return ("HK", value)
    if re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", value):
        return ("A", value.split(".")[0])
    if value.endswith(".US"):
        value = value[:-3]
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*", value):
        return ("US", value)
    return ("invalid", value)


def _row_code(row):
    values = [
        _code(row[field])
        for field in ("code", "instrument")
        if _present(row.get(field))
    ]
    if any(value[0] == "invalid" for value in values) or (
        len(values) == 2 and values[0] != values[1]
    ):
        return ("invalid", "conflicting_security_identity")
    return values[0] if values else None


def _names(row):
    uncertain = _uncertain(row)
    names = set() if "name" in uncertain else {_text(row.get("name"))}
    if "aliases" not in uncertain and isinstance(row.get("aliases"), list):
        names.update(_text(alias) for alias in row["aliases"] if isinstance(alias, str))
    return names - {""}


def _uncertain(row):
    fields = row.get("uncertain_fields", [])
    if not isinstance(fields, list):
        return set(FACT_FIELDS) | {"aliases"}
    return {field for field in fields if isinstance(field, str)}


def _value_currency(row):
    """Match the value's currency without inventing an absent price currency."""
    uncertain = _uncertain(row)
    if _present(row.get("market_value_currency")):
        return (
            ""
            if "market_value_currency" in uncertain
            else _text(row["market_value_currency"])
        )
    return "" if "currency" in uncertain else _text(row.get("currency"))


def _unknown_asset_type(row):
    return "asset_type" in _uncertain(row) and row.get("asset_type") in {
        None,
        "",
        "other",
        "unknown",
    }


def _market(value):
    text = _text(value)
    return {
        "a股": "a",
        "ashare": "a",
        "mainland": "a",
        "港股": "hk",
        "hongkong": "hk",
        "美股": "us",
    }.get(text, text)


def _field_equal(field, left, right):
    if field in NUMERIC_FIELDS or field in {"price_decimals", "term_days"}:
        first, second = _number(left), _number(right)
        return first is not None and second is not None and first == second
    if field in {"code", "instrument"}:
        return _code(left) == _code(right)
    if field in {"as_of", "maturity_date", "due_at"}:
        first, second = _time(left), _time(right)
        return first is not None and first[0] != "invalid" and first == second
    if field == "market":
        return _market(left) == _market(right)
    return _text(left) == _text(right)


def _pair(left, right):
    """Return candidate/conflict/irrelevant without relying on row position."""
    currency = _value_currency(left)
    value = _number(left.get("market_value"))
    other = _number(right.get("market_value"))
    if (
        not currency
        or currency != _value_currency(right)
        or value is None
        or other is None
        or value < 0
        or value != other
    ):
        return "irrelevant"
    if any(
        field in _uncertain(row) for row in (left, right) for field in ("market_value",)
    ):
        return "irrelevant"
    first_code, second_code = _row_code(left), _row_code(right)
    same_code = (
        first_code is not None
        and first_code == second_code
        and first_code[0] != "invalid"
        and not any(
            field in _uncertain(row)
            for row in (left, right)
            for field in ("code", "instrument")
        )
    )
    shared_name = bool(_names(left) & _names(right))
    if not same_code and not shared_name:
        return "irrelevant"
    if not _account_compatible(left, right):
        return "conflict"
    if (first_code and first_code[0] == "invalid") or (
        second_code and second_code[0] == "invalid"
    ):
        return "conflict"
    if first_code and second_code and first_code != second_code:
        return "conflict"
    if _time(left.get("as_of")) != _time(right.get("as_of")):
        return "conflict"
    for row, code in ((left, first_code), (right, second_code)):
        declared = _market(row.get("market"))
        if code and declared in {"a", "hk", "us"} and code[0].casefold() != declared:
            return "conflict"
    for field in (
        *NUMERIC_FIELDS,
        *UNITS,
        "currency",
        "market_value_currency",
        "asset_type",
        "market",
        "exchange",
        "as_of",
        "maturity_date",
        "term_days",
        "due_at",
    ):
        if field == "asset_type" and (
            _unknown_asset_type(left) or _unknown_asset_type(right)
        ):
            continue
        if (
            _present(left.get(field))
            and _present(right.get(field))
            and not _field_equal(field, left[field], right[field])
        ):
            return "conflict"
        if field in NUMERIC_FIELDS:
            for row in (left, right):
                if _present(row.get(field)) and _number(row[field]) is None:
                    return "conflict"
    # An explicit exchange suffix also disambiguates securities with equal
    # numeric bodies; do not erase this evidence while normalizing providers.
    first = str(left.get("code") or left.get("instrument") or "").upper()
    second = str(right.get("code") or right.get("instrument") or "").upper()
    suffixes = {"SH", "SZ", "BJ"}
    if (
        first.rsplit(".", 1)[-1] in suffixes
        and second.rsplit(".", 1)[-1] in suffixes
        and first.rsplit(".", 1)[-1] != second.rsplit(".", 1)[-1]
    ):
        return "conflict"
    return "candidate"


def _sources(row, field, fallback, allowed):
    existing = row.get("field_sources", {})
    if isinstance(existing, dict) and field in existing:
        return sorted(set(_hashes(existing[field])) & allowed)
    return sorted(set(fallback) & allowed)


def _append_unique(target, values):
    for value in values:
        if value not in target:
            target.append(copy.deepcopy(value))


def _context_row(row, document):
    result = dict(row)
    for field in ("market", "exchange", "as_of", *ACCOUNT_FIELDS):
        if not _present(result.get(field)) and _present(document.get(field)):
            result[field] = document[field]
    return result


def _remaining_ambiguities(values, uncertain, resolved):
    """Only the prompt's explicit field-name prefix can retire a note."""
    remaining = []
    for value in values:
        matched = (
            re.match(r"^\s*([a-z_]+)\s*[:：]", value)
            if isinstance(value, str)
            else None
        )
        if matched and matched[1] in uncertain and matched[1] in resolved:
            continue
        remaining.append(copy.deepcopy(value))
    return remaining


def compose_position_document(
    current_document, current_hash, previous_batches, *, current_image_hashes=None
):
    """Fill uniquely correlated empty columns, preserving uncertainty and proof.

    Return a deep copy with the same current row order and count. ``composition``
    reports row matches, source batches and blocked joins. Both absent snapshot
    times are permitted only because the caller has already limited recency;
    one absent time cannot inherit a prior image's declared/visible time.
    ``current_image_hashes`` must be calculated by the caller from the actual
    image bytes. For a multi-image batch, ``current_hash`` may identify that
    batch but is never substituted for the physical image provenance.
    """
    if not isinstance(current_document, dict):
        raise TypeError("current_document_must_be_object")
    if not isinstance(current_hash, str) or not current_hash.strip():
        raise ValueError("current_image_hash_required")
    if current_image_hashes is None:
        physical_hashes = {current_hash}
    else:
        if not isinstance(current_image_hashes, list):
            raise TypeError("current_image_hashes_must_be_list")
        physical_hashes = set(_hashes(current_image_hashes))
        if not physical_hashes:
            raise ValueError("current_physical_image_hashes_required")
    if not isinstance(previous_batches, list):
        raise TypeError("previous_batches_must_be_list")
    result = copy.deepcopy(current_document)
    rows = result.get("positions", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("positions_must_be_object_array")
    current_time_ok, current_time = _document_time(result)
    eligible = []
    seen_batches = {}
    collision_hashes = set()
    for batch in previous_batches:
        if not isinstance(batch, dict) or not isinstance(batch.get("document"), dict):
            continue
        hashes = _hashes(batch.get("source_image_hashes"))
        if not hashes or physical_hashes.intersection(hashes):
            continue
        fingerprint = json.dumps(
            batch["document"], ensure_ascii=False, sort_keys=True, default=str
        )
        key = tuple(hashes)
        if key in seen_batches:
            if seen_batches[key] != fingerprint:
                collision_hashes.update(hashes)
            continue
        seen_batches[key] = fingerprint
        document = batch["document"]
        time_ok, moment = _document_time(document)
        if (
            not _same_account(result, document)
            or not current_time_ok
            or not time_ok
            or moment != current_time
        ):
            continue
        previous_rows = document.get("positions", [])
        if not isinstance(previous_rows, list):
            continue
        eligible.append({"batch": batch, "hashes": hashes, "rows": previous_rows})
    eligible = [
        batch for batch in eligible if not set(batch["hashes"]) & collision_hashes
    ]
    allowed = set(physical_hashes)
    for batch in eligible:
        allowed.update(batch["hashes"])
    candidates, reverse, conflicts = {}, {}, set()
    for index, row in enumerate(rows):
        for batch_index, batch in enumerate(eligible):
            for previous_index, previous in enumerate(batch["rows"]):
                if not isinstance(previous, dict):
                    continue
                status = _pair(
                    _context_row(row, result),
                    _context_row(previous, batch["batch"]["document"]),
                )
                identity = (batch_index, previous_index)
                if status == "conflict":
                    conflicts.add(index)
                elif status == "candidate":
                    candidates.setdefault(index, []).append(identity)
                    reverse.setdefault(identity, []).append(index)
    matched, source_batches, blocked = [], [], []
    total_hashes = set(physical_hashes)
    for index, row in enumerate(rows):
        field_sources = {}
        for field in FACT_FIELDS:
            if _present(row.get(field)):
                field_sources[field] = _sources(
                    row, field, list(physical_hashes), allowed
                )
        own_hashes = {source for values in field_sources.values() for source in values}
        choices = candidates.get(index, [])
        if index in conflicts:
            blocked.append({"row_index": index, "reason": "conflicting_facts"})
            choices = []
        elif len(choices) > 1 or (choices and len(reverse[choices[0]]) > 1):
            blocked.append({"row_index": index, "reason": "ambiguous_candidates"})
            choices = []
        if len(choices) == 1:
            batch_index, previous_index = choices[0]
            batch = eligible[batch_index]
            previous = batch["rows"][previous_index]
            uncertain = row.get("uncertain_fields", [])
            current_uncertain = _uncertain(row)
            donor_uncertain = _uncertain(previous)
            donor_resolved = {
                field
                for field in donor_uncertain
                if _present(row.get(field))
                and field not in current_uncertain
                and (
                    not _present(previous.get(field))
                    or _field_equal(field, row[field], previous[field])
                    or (field == "asset_type" and _unknown_asset_type(previous))
                )
            }
            resolved = set()
            filled = []
            missing_price = not _present(row.get("price"))
            for field in FACT_FIELDS:
                if field in donor_uncertain or not _present(previous.get(field)):
                    continue
                if field == "price_decimals" and (
                    not _present(previous.get("price")) or "price" in donor_uncertain
                ):
                    continue
                if (
                    not _present(row.get(field))
                    or (
                        field == "price_decimals"
                        and missing_price
                        and "price" in filled
                    )
                    or (field == "asset_type" and _unknown_asset_type(row))
                ):
                    sources = _sources(
                        previous, field, batch["hashes"], set(batch["hashes"])
                    )
                    if not sources:
                        continue
                    row[field] = copy.deepcopy(previous[field])
                    field_sources[field] = sources
                    filled.append(field)
                    resolved.add(field)
            if filled:
                if isinstance(uncertain, list):
                    row["uncertain_fields"] = [
                        field for field in uncertain if field not in resolved
                    ]
                # Legacy free prose and validation flags are preserved. A new
                # field-prefixed note is retired only with that exact declared
                # field resolved by clear evidence from the other image.
                if isinstance(row.get("ambiguities"), list):
                    row["ambiguities"] = _remaining_ambiguities(
                        row["ambiguities"], current_uncertain, resolved
                    )
                if isinstance(previous.get("ambiguities"), list):
                    existing = row.get("ambiguities", [])
                    if isinstance(existing, list):
                        _append_unique(
                            existing,
                            _remaining_ambiguities(
                                previous["ambiguities"], donor_uncertain, donor_resolved
                            ),
                        )
                        if existing or "ambiguities" in row:
                            row["ambiguities"] = existing
                if (
                    isinstance(previous.get("aliases"), list)
                    and "aliases" not in donor_uncertain
                    and _sources(
                        previous, "aliases", batch["hashes"], set(batch["hashes"])
                    )
                ):
                    aliases = row.get("aliases", [])
                    if isinstance(aliases, list):
                        before = list(aliases)
                        _append_unique(
                            aliases,
                            [
                                alias
                                for alias in previous["aliases"]
                                if isinstance(alias, str)
                            ],
                        )
                        if aliases:
                            row["aliases"] = aliases
                        if aliases != before:
                            field_sources["aliases"] = sorted(
                                set(field_sources.get("aliases", []))
                                | set(
                                    _sources(
                                        previous,
                                        "aliases",
                                        batch["hashes"],
                                        set(batch["hashes"]),
                                    )
                                )
                            )
                own_hashes.update(batch["hashes"])
                matched.append(
                    {
                        "row_index": index,
                        "fields": filled,
                        "source_image_hashes": batch["hashes"],
                    }
                )
                if _present(batch["batch"].get("batch_id")):
                    source_batches.append(batch["batch"]["batch_id"])
        row["field_sources"] = field_sources
        row["source_image_hashes"] = sorted(own_hashes or physical_hashes)
        total_hashes.update(row["source_image_hashes"])
    # Keep a stable receipt when replaying an already composed document. No
    # source or field is appended twice and the input objects remain untouched.
    previous_composition = result.get("composition", {})
    if isinstance(previous_composition, dict):
        for old in previous_composition.get("matched_rows", []):
            if (
                isinstance(old, dict)
                and set(_hashes(old.get("source_image_hashes"))) <= allowed
                and old not in matched
            ):
                matched.append(copy.deepcopy(old))
        for old in previous_composition.get("source_batch_ids", []):
            if old not in source_batches:
                source_batches.append(copy.deepcopy(old))
    result["source_image_hashes"] = sorted(total_hashes)
    result["composition"] = {
        "matched_rows": matched,
        "source_batch_ids": source_batches,
        "blocked": blocked,
    }
    return result
