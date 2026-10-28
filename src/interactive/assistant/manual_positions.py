"""Ground manual position changes in typed facts, independently of OCR context.

The model selects candidate securities; this module accepts only quantities the
user actually states. Prices, names, currencies and valuations supplied by the
model are never imported through this endpoint.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from zoneinfo import ZoneInfo

from ...personal_monitor import canonical_instrument

_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_UNIT_QUANTITY = re.compile(
    rf"(?<![A-Za-z\d.,+\-])(?P<number>{_NUMBER})\s*(?P<scale>万|千)?\s*(?:股|份|shares?)(?![A-Za-z])",
    re.IGNORECASE,
)
_LABELED_QUANTITY = re.compile(
    rf"(?:持仓数量|持有数量|数量|持仓|持有|剩余|剩下|余下)\s*(?:为|是|有)?\s*[:：=]?\s*"
    rf"(?P<number>{_NUMBER})\s*(?P<scale>万|千)?(?![\d.,eE])"
)
_CODE = re.compile(
    r"(?<![A-Za-z0-9.])(?:\d{1,5}\.HK|\d{6}(?:\.(?:SH|SS|SZ|BJ))?|\d{5}|"
    r"[A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*)(?![A-Za-z0-9.])",
    re.IGNORECASE,
)
_DATE_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:\s*[Tt ]\s*\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})?)?"
)
_LABEL_END = re.compile(
    r"(?:持仓数量|持有数量|数量|剩余|剩下|余下|价格|现价|股价|市值|持仓市值)\s*(?:为|是|有)?\s*[:：=]?\s*$"
)
_POSITION_END = re.compile(r"(?:持仓|持有)\s*(?:为|是|有)?\s*[:：=]?\s*$")
_UNITS = re.compile(r"\s*(?:万|千)?\s*(?:股|份|shares?)(?![A-Za-z])", re.IGNORECASE)
_UNASSERTED = re.compile(
    r"如果|假如|假设|假定|预计|一旦|等到|到时|计划|打算|准备|考虑|可能|是否|是不是|要不要|"
    r"尚未|并非|不是|没有|不要|不能|不必|不用|不再|别|勿|未|没|不|若|希望|想要|将要|将会"
)
_CLOSED = re.compile(
    r"已(?:经)?(?:全部)?清仓|清仓了|持仓(?:已(?:经)?)?清零|仓位(?:已(?:经)?)?清零|"
    r"已(?:经)?(?:全部)?卖(?:完|光)|已(?:经)?全部卖出"
)
_TRANSACTION = re.compile(r"买入|卖出|买了|卖了|卖掉|减持|增持")
_POSITION_LABEL = re.compile(r"持仓|持有|剩余|剩下|余下|现有|数量")
_UNASSERTED_TAIL = re.compile(
    r"^[\s，,\"'“”]*(?:(?:股|份|shares?)\s*)?(?:吗|么|？|\?|的话|时(?:候)?|以后|之后|后|才|就|再|"
    r"是否|是不是|要不要|(?:对|正确)[吗么？?]|"
    r"如果|仅为|只是|是假设|是错|是错误|(?:并)?不(?:是|代表|属实|一定|正确)|"
    r"(?:但)?(?:尚未|并未|没)确认)"
)
_NON_CODES = {
    "SHARE",
    "SHARES",
    "QUANTITY",
    "HOLDING",
    "HOLDINGS",
    "CNY",
    "RMB",
    "HKD",
    "USD",
}
_CURRENCIES = {
    "CNY": "CNY",
    "RMB": "CNY",
    "HKD": "HKD",
    "USD": "USD",
    "人民币": "CNY",
    "港元": "HKD",
    "港币": "HKD",
    "美元": "USD",
    "美金": "USD",
}
_PRICE = re.compile(
    rf"(?:价格|现价|股价)\s*(?:为|是)?\s*[:：=]?\s*(?P<number>{_NUMBER})(?![\d.,eE])\s*(?P<unit>港元|港币|美元|美金|人民币|元)?"
)
_VALUE = re.compile(
    rf"(?:持仓市值|市值)\s*(?:为|是)?\s*[:：=]?\s*(?P<number>{_NUMBER})(?![\d.,eE])\s*(?P<scale>万|千)?\s*(?P<unit>港元|港币|美元|美金|人民币|元)?"
)
_CURRENCY = re.compile(
    r"(?:币种|货币)\s*(?:为|是)?\s*[:：=]?\s*(CNY|RMB|HKD|USD|人民币|港元|港币|美元|美金)(?![A-Za-z])",
    re.IGNORECASE,
)
_HISTORICAL = re.compile(
    r"之前|以前|此前|先前|原先|原来|上次|上一组|前一组|旧|原数量|原有数量"
)
_CURRENT_CORRECTION = re.compile(
    r"现在|当前|现有|更正[为成]|改[为成]|更新为|正确数量[是为]"
)
_CLAUSE_BREAK = re.compile(r"[，、；;。\n]|(?<!\d),|,(?!\d)")
_CONDITIONAL = re.compile(
    r"如果|假如|假设|假定|一旦|若|计划|打算|准备|考虑|可能|预计|希望|想要|将要|将会"
)
_DENIAL = re.compile(
    r"不是|并非|没有|并未|尚未|不再|不正确|不属实|不对|撤销|作废|取消|无效|错误|错了"
)
_OLD_REPLACED_QUANTITY = re.compile(
    rf"(?:不是|并非|从|原(?:来|先)?(?:是|为)?)\s*{_NUMBER}\s*(?:万|千)?\s*"
    rf"(?:股|份|shares?)\s*(?:而是|应为|应是|改[为成]|更正[为成]|更新为)",
    re.IGNORECASE,
)


def _current_assertions(text, existing):
    """Mask old clauses/replaced quantities, preserving all current qualifiers.

    Keep offsets stable. A single security may inherit its identity across an
    old/current comma clause; a multi-security list must restate its targets.
    This never removes a conditional or a denial of the replacement quantity.
    """
    mentions = _mentioned(text, existing)
    identities = set().union(*(codes for _start, _end, codes, _exact in mentions))
    chars = list(text)
    start = 0
    spans = []
    for boundary in _CLAUSE_BREAK.finditer(text):
        spans.append((start, boundary.start()))
        start = boundary.end()
    spans.append((start, len(text)))
    for begin, end in spans:
        clause = text[begin:end]
        history = _HISTORICAL.search(clause)
        if not history:
            continue
        current = _CURRENT_CORRECTION.search(clause)
        mask_end = end
        if current:
            if (
                current.start() < history.end()
                or _CONDITIONAL.search(clause[: current.start()])
                or not (
                    _UNIT_QUANTITY.search(clause[current.end() :])
                    or _LABELED_QUANTITY.search(clause[current.end() :])
                )
            ):
                continue
            mask_end = begin + current.start()
        # "如果旧数量错误" still makes a following replacement conditional.
        # Only an asserted old reference may be discarded as prior context.
        if _UNASSERTED.search(clause[: history.start()]):
            continue
        protected = [
            (left, right)
            for left, right, _codes, _exact in mentions
            if len(identities) == 1 and begin <= left < right <= mask_end
        ]
        for index in range(begin, mask_end):
            if not any(left <= index < right for left, right in protected):
                chars[index] = " "
    normalized = "".join(chars)
    # Remove only the explicitly superseded old numeric operand. Negative or
    # hypothetical language outside that operand remains part of the assertion.
    for match in _OLD_REPLACED_QUANTITY.finditer(normalized):
        for index in range(match.start(), match.end()):
            chars[index] = " "
    return "".join(chars)


def _denied_candidate(evidence, tail):
    # Retraction terms are evaluated in this security's current fragment, after
    # historical context has been removed, rather than across the whole message.
    return bool(
        _UNASSERTED.search(evidence)
        or _WITHDRAWN.search(evidence)
        or _UNASSERTED_TAIL.search(tail)
        or _WITHDRAWN.search(tail)
    )


def _identity(row):
    if not isinstance(row, dict):
        raise ValueError("手工持仓行必须是对象")  # noqa: TRY004 -- one validation error contract
    return canonical_instrument(row.get("instrument", row.get("code")))


def _quantity(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("持仓数量必须是有限非负数")  # noqa: TRY004 -- bool is also an invalid quantity
    try:
        value = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("持仓数量必须是有限非负数") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("持仓数量必须是有限非负数")
    return value


def _followed_by_own_quantity(text, end):
    """A generic holding noun can introduce a security, not just its quantity."""
    suffix = re.split(r"[；;。\n]", text[end:], maxsplit=1)[0][:100]
    quantities = sorted(
        [*_UNIT_QUANTITY.finditer(suffix), *_LABELED_QUANTITY.finditer(suffix)],
        key=lambda match: match.start(),
    )
    if not quantities:
        return False
    before_quantity = suffix[: quantities[0].start()]
    time_spans = [match.span() for match in _DATE_TIME.finditer(before_quantity)]
    # A later security owns its own quantity. Do not borrow it to reinterpret
    # a six-digit position amount belonging to the preceding security.
    return not any(
        match.group().upper() not in _NON_CODES
        and not any(
            start < match.end() and match.start() < stop for start, stop in time_spans
        )
        for match in _CODE.finditer(before_quantity)
    )


def _mentioned(text, existing):
    mentions = []
    time_spans = [match.span() for match in _DATE_TIME.finditer(text)]
    for match in _CODE.finditer(text):
        if any(
            start < match.end() and match.start() < end for start, end in time_spans
        ):
            continue
        token = match.group()
        if token.upper() in _NON_CODES:
            continue
        labeled_scalar = _LABEL_END.search(text[: match.start()])
        if labeled_scalar and _quantity_list_code(text, match, mentions):
            labeled_scalar = None
        if token.isdigit() and (
            _UNITS.match(text[match.end() :])
            or labeled_scalar
            or (
                _POSITION_END.search(text[: match.start()])
                and not _followed_by_own_quantity(text, match.end())
            )
        ):
            continue
        # An English pronoun or article is not an unambiguous one-letter ticker.
        if len(token) == 1 and not re.search(
            r"(?:代码|股票|证券|标的|\$)\s*$", text[: match.start()]
        ):
            continue
        try:
            code = canonical_instrument(token)
        except ValueError:
            continue
        mentions.append((match.start(), match.end(), {code}, True))
    names = {}
    for code, position in existing.items():
        name = position.get("name")
        if isinstance(name, str) and name.strip():
            names.setdefault(name.strip(), set()).add(code)
    for name in sorted(names, key=len, reverse=True):
        for match in re.finditer(re.escape(name), text):
            # A display name immediately after an explicit code describes that
            # code. It is not a second holding inferred from the old account.
            if any(
                is_code
                and end <= match.start()
                and re.fullmatch(r"[\s:：(（-]*", text[end : match.start()])
                for _start, end, _codes, is_code in mentions
            ):
                continue
            if any(
                start < match.end() and match.start() < end
                for start, end, _codes, _exact in mentions
            ):
                continue
            mentions.append((match.start(), match.end(), names[name], False))
    return sorted(mentions)


def _quantity_list_code(text, match, mentions):
    """A quantity-list header needs this code's own same-clause holding amount.

    Price/value labels remain scalars. Neither another security nor an available
    amount or a later comma clause may supply the candidate's missing quantity.
    """
    boundaries = list(_CLAUSE_BREAK.finditer(text, 0, match.start()))
    begin = boundaries[-1].end() if boundaries else 0
    prefix = text[begin : match.start()]
    if not re.search(
        r"(?:[HA]\s*股|港股|股票|证券|标的|代码)"
        r"(?:(?:代码|持仓|持有|数量)|[及和与为是有\s:：=])*$",
        prefix,
        re.IGNORECASE,
    ) or any(
        begin <= start < match.start() for start, _end, _codes, _exact in mentions
    ):
        return False
    boundary = _CLAUSE_BREAK.search(text, match.end())
    end = boundary.start() if boundary else len(text)
    suffix = text[match.end() : end]
    quantity = _UNIT_QUANTITY.search(suffix)
    return bool(
        quantity
        and not _non_holding_quantity(prefix + suffix[: quantity.start()])
        and _followed_by_own_quantity(text[:end], match.end())
    )


def _non_holding_quantity(prefix):
    """Classify the nearest local field without borrowing the list's header.

    Generic 'quantity' inside a capacity/transaction field cannot turn it into
    holdings. An explicit later holding field can state the final total.
    """
    clause = _CLAUSE_BREAK.split(prefix)[-1]
    other = None
    for field in re.finditer(
        r"(?P<other>可(?:用|买(?:入)?|卖(?:出)?|售|交易)"
        r"(?:(?:的)?(?:数量|股数|份额|额度|上限|金额|余额))*|"
        r"(?:买入|卖出|成交|交易)(?:数量|股数|份额|金额)?)|"
        r"(?P<holding>总持仓|持仓数量|持有数量|持仓|持有|"
        r"(?:当前|现在|现有)(?:的)?(?:数量|股数|份额)|剩余|剩下|余下)|"
        r"数量|股数|份额",
        clause,
    ):
        if field.group("other"):
            other = field
        elif field.group("holding") and other is not None:
            between = clause[other.end() : field.start()]
            # A holding word inside the same capacity field is still capacity.
            # A completed preceding amount or an explicit transaction outcome
            # can introduce an independent final holding field.
            if _UNIT_QUANTITY.search(between) or (
                not other.group().startswith("可")
                and re.search(r"后\s*(?:当前|现在|现有)?\s*$", between)
            ):
                other = None
    return other is not None


def _facts(text, code, mentions):
    values = set()
    denied_values = set()
    exact = any(code in codes and is_code for _start, _end, codes, is_code in mentions)
    for index, (start, end, codes, is_code) in enumerate(mentions):
        if code not in codes:
            continue
        if len(codes) > 1:
            if not exact:
                raise ValueError("证券名称对应多个市场，请明确证券代码")
            continue
        stop = mentions[index + 1][0] if index + 1 < len(mentions) else len(text)
        boundary = re.search(r"[；;。\n]", text[end:stop])
        if boundary:
            stop = end + boundary.start()
        fragment = text[end:stop]
        # A conditional/negative prefix belongs only to this local clause.
        previous = max(
            text.rfind(mark, 0, start) for mark in ("，", ",", "；", ";", "。", "\n")
        )
        prefix = text[previous + 1 : start]
        sentence_start = (
            max(text.rfind(mark, 0, start) for mark in ("；", ";", "。", "\n")) + 1
        )
        conditional_scope = text[sentence_start:start]
        closed = _CLOSED.search(fragment)
        if closed:
            evidence = prefix + fragment[: closed.end()]
            tail = fragment[closed.end() :]
            if not _CONDITIONAL.search(conditional_scope) and not _denied_candidate(
                evidence, tail
            ):
                values.add(Decimal(0))
        candidates = list(_UNIT_QUANTITY.finditer(fragment)) + list(
            _LABELED_QUANTITY.finditer(fragment)
        )
        for match in candidates:
            if _non_holding_quantity(prefix + fragment[: match.start("number")]):
                continue
            evidence = prefix + fragment[: match.end()]
            following = min(
                (other.start() for other in candidates if other.start() >= match.end()),
                default=len(fragment),
            )
            tail = fragment[match.end() : following]
            value = Decimal(match.group("number").replace(",", ""))
            scale = match.group("scale")
            if scale:
                value *= 10000 if scale == "万" else 1000
            if _CONDITIONAL.search(conditional_scope) or _denied_candidate(
                evidence, tail
            ):
                if _DENIAL.search(evidence) or _DENIAL.search(tail):
                    denied_values.add(value)
                continue
            if _TRANSACTION.search(evidence) and not _POSITION_LABEL.search(evidence):
                continue
            values.add(value)
    if values & denied_values:
        raise ValueError(f"{code}的同一持仓数量又被否定，请明确当前数量")
    if len(values) > 1:
        raise ValueError(f"{code}出现多个不同持仓数量，请给出唯一当前数量")
    return next(iter(values)) if values else None


def _metadata(text, code, mentions):
    facts = {"price": set(), "market_value": set(), "currency": set()}
    for index, (_start, end, codes, _is_code) in enumerate(mentions):
        if codes != {code}:
            continue
        stop = mentions[index + 1][0] if index + 1 < len(mentions) else len(text)
        boundary = re.search(r"[；;。\n]", text[end:stop])
        if boundary:
            stop = end + boundary.start()
        fragment = text[end:stop]
        for field, pattern in (("price", _PRICE), ("market_value", _VALUE)):
            for match in pattern.finditer(fragment):
                local_start = (
                    max(fragment.rfind(mark, 0, match.start()) for mark in ("，", ","))
                    + 1
                )
                if _UNASSERTED.search(fragment[local_start : match.end()]):
                    continue
                if _UNASSERTED_TAIL.search(fragment[match.end() :]):
                    continue
                if (
                    field == "price"
                    and fragment[max(0, match.start() - 2) : match.start()] == "成本"
                ):
                    continue
                value = Decimal(match.group("number").replace(",", ""))
                if field == "market_value" and match.group("scale"):
                    value *= 10000 if match.group("scale") == "万" else 1000
                facts[field].add(value)
                unit = match.group("unit")
                if unit in _CURRENCIES:
                    facts["currency"].add(_CURRENCIES[unit])
        for match in _CURRENCY.finditer(fragment):
            local_start = (
                max(fragment.rfind(mark, 0, match.start()) for mark in ("，", ",")) + 1
            )
            if _UNASSERTED.search(
                fragment[local_start : match.end()]
            ) or _UNASSERTED_TAIL.search(fragment[match.end() :]):
                continue
            facts["currency"].add(_CURRENCIES[match.group(1).upper()])
    result = {}
    for field, values in facts.items():
        if len(values) > 1:
            raise ValueError(f"{code}的{field}存在多个不同明确值")
        if values:
            value = next(iter(values))
            result[field] = float(value) if isinstance(value, Decimal) else value
    return result


def validate_manual_rows(rows, text, existing_positions):
    """Return ``(grounded_rows, untouched_codes)``; never trust model metadata.

    Securities merely mentioned without an asserted current quantity are left
    untouched. At least one candidate must have a typed, unambiguous fact. A
    duplicate canonical candidate, ambiguous name or conflicting quantity fails
    closed rather than guessing which candidate the user meant.
    """
    if not isinstance(rows, list) or not 0 < len(rows) <= 200:
        raise ValueError("手工持仓必须包含1至200行")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请明确输入证券及当前持仓数量")
    existing = {}
    for row in existing_positions:
        code = _identity(row)
        if code in existing:
            raise ValueError("现持仓包含重复证券身份")
        existing[code] = row
    text = _current_assertions(text, existing)
    candidates = []
    seen = set()
    for row in rows:
        code = _identity(row)
        if code in seen:
            raise ValueError("手工持仓包含重复证券身份")
        seen.add(code)
        candidates.append((code, row))
    mentions = _mentioned(text, existing)
    grounded, untouched = [], []
    for code, row in candidates:
        value = _facts(text, code, mentions)
        if value is None:
            untouched.append(code)
            continue
        if _quantity(row.get("quantity")) != value:
            raise ValueError(f"{code}工具数量与用户明确输入不一致")
        result = copy.deepcopy(existing.get(code, {}))
        result.pop("code", None)
        result["instrument"] = code
        result["quantity"] = (
            int(value) if value == value.to_integral_value() else float(value)
        )
        metadata = _metadata(text, code, mentions)
        changed = {key for key, value in metadata.items() if result.get(key) != value}
        if changed & {"price", "currency"}:
            result.pop("quote_evidence", None)
        raw_values = result.get("raw_values")
        if isinstance(raw_values, dict):
            for field in changed | {"quantity"}:
                raw_values.pop(field, None)
            if not raw_values:
                result.pop("raw_values", None)
        result.update(metadata)
        grounded.append(result)
    if not grounded:
        raise ValueError(
            "请明确输入证券及当前持仓数量，例如证券代码和700股；零仓须明确0股或已清仓"
        )
    return grounded, untouched


def extract_manual_rows(text: str, existing_positions: list[dict]) -> list[dict]:
    """Read complete typed holdings without importing or inferring metadata.

    Account/date applicability belongs to the caller. Every mentioned security
    must have one asserted current quantity; a partial or ambiguous statement
    cannot become an executable candidate by silently dropping its other rows.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请明确输入证券及当前持仓数量")
    existing = {}
    for row in existing_positions:
        code = _identity(row)
        if code in existing:
            raise ValueError("现持仓包含重复证券身份")
        existing[code] = row
    # An explicit account label is not a security mention: 中信AH contains the
    # otherwise valid US ticker AH. Keep the original offsets and all facts.
    assertion = re.sub(
        r"(?:账户|账号)\s*[^\s，,；;。\n：:]+",
        lambda match: " " * len(match.group()),
        text,
    )
    assertion = _current_assertions(assertion, existing)
    mentions = _mentioned(assertion, existing)
    if not mentions or any(len(codes) != 1 for _a, _b, codes, _exact in mentions):
        raise ValueError("手工事实缺少唯一明确的证券身份")
    instruments = list(
        dict.fromkeys(next(iter(codes)) for _a, _b, codes, _exact in mentions)
    )
    if len(instruments) > 200:
        raise ValueError("手工持仓必须包含1至200行")
    rows = []
    for code in instruments:
        value = _facts(assertion, code, mentions)
        if value is None:
            raise ValueError(f"{code}缺少明确的当前持仓数量")
        rows.append(
            {
                "instrument": code,
                "quantity": (
                    int(value) if value == value.to_integral_value() else float(value)
                ),
            }
        )
    grounded, untouched = validate_manual_rows(rows, assertion, existing_positions)
    if untouched or len(grounded) != len(rows):
        raise ValueError("手工事实包含尚未明确的持仓数量")
    return [
        {"instrument": row["instrument"], "quantity": row["quantity"]}
        for row in grounded
    ]


_ORDERED_HOLDINGS = re.compile(r"(?:H\s*股|港股|持仓)\s*分别\s*是\s*", re.IGNORECASE)
_ORDERED_CODES = re.compile(r"\s*代码\s*分别\s*是\s*(.*?)\s*[。.]?\s*$")
_NAMED_QUANTITY = re.compile(
    rf"\s*(?P<name>[^\d，,、；;。\n][^，,、；;。\n]*?)\s*"
    rf"(?P<number>{_NUMBER})\s*(?P<scale>万|千)?\s*股\s*"
    rf"(?:[，,、；;\n]|[。.]?\s*$)"
)
_WITHDRAWN = re.compile(r"撤销|作废|取消|无效|错了|不对|错误|仅为示例|举例|假数据")


def _manual_message(message):
    if not isinstance(message, dict):
        raise ValueError("手工事实消息必须是认证消息对象")  # noqa: TRY004 -- one validation contract
    identity, text = message.get("id"), message.get("text")
    if (
        not isinstance(identity, str)
        or not identity.strip()
        or len(identity) > 256
        or not isinstance(text, str)
        or not text.strip()
        or len(text) > 8000
        or message.get("role", "user") != "user"
    ):
        raise ValueError("手工事实消息缺少有效ID或原文")
    stamp = message.get("effective_time")
    try:
        if not isinstance(stamp, datetime):
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("手工事实消息时间必须含时区")
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("手工事实消息缺少有效认证时间") from exc
    return {
        "id": identity,
        "text": text,
        "time": stamp.astimezone(ZoneInfo("Asia/Shanghai")),
    }


def _manual_provenance(rows, quantity_message, code_message):
    evidence = {
        "message_ids": list(
            dict.fromkeys([quantity_message["id"], code_message["id"]])
        ),
        "quantity_message_id": quantity_message["id"],
        "quantity_effective_time": quantity_message["time"].isoformat(),
        "code_message_id": code_message["id"],
        "code_effective_time": code_message["time"].isoformat(),
    }
    for row in rows:
        row["manual_evidence"] = copy.deepcopy(evidence)


def _ordered_quantities(text):
    markers = list(_ORDERED_HOLDINGS.finditer(text))
    assertion = re.sub(r"分别\s*是", "", text)
    if len(markers) != 1 or _UNASSERTED.search(assertion) or _WITHDRAWN.search(text):
        return None
    body = text[markers[0].end() :]
    result, names, offset = [], set(), 0
    while offset < len(body) and body[offset:].strip():
        match = _NAMED_QUANTITY.match(body, offset)
        if not match:
            return None
        name = re.sub(r"\s+", "", match.group("name")).casefold()
        if not name or name in names or re.search(r"[\d:=：+\-]", name):
            return None
        names.add(name)
        value = Decimal(match.group("number").replace(",", ""))
        if match.group("scale"):
            value *= 10000 if match.group("scale") == "万" else 1000
        result.append(value)
        if len(result) > 20:
            return None
        offset = match.end()
    return result or None


def _ordered_codes(text):
    match = _ORDERED_CODES.fullmatch(text)
    assertion = re.sub(r"分别\s*是", "", text)
    if not match or _UNASSERTED.search(assertion) or _WITHDRAWN.search(text):
        return None
    tokens = re.split(r"[，,、；;\s]+", match[1].strip())
    if not 1 <= len(tokens) <= 20 or any(
        not re.fullmatch(r"\d{5}", token) for token in tokens
    ):
        return None
    codes = [canonical_instrument(token) for token in tokens]
    return codes if len(set(codes)) == len(codes) else None


def validate_manual_messages(rows, messages, existing_positions):
    """Ground selected rows in bounded, externally authenticated text messages.

    The caller verifies owner/chat/account scope and revision guards. Ordered
    cross-message mapping is limited to an explicit named quantity list followed
    within 15 minutes by an explicit code list on the same Beijing calendar day.
    Neither assistant text nor image ordering is accepted here as a fact source.
    """
    if not isinstance(messages, list) or not 1 <= len(messages) <= 5:
        raise ValueError("手工事实须选择1至5条认证消息")
    selected = [_manual_message(message) for message in messages]
    if len({message["id"] for message in selected}) != len(selected):
        raise ValueError("手工事实消息ID重复")
    selected.sort(key=lambda message: message["time"])
    # Prefer a complete assertion within one authenticated message. A newer
    # conflicting quantity must not be hidden by falling back to an older one.
    existing = {_identity(row): row for row in existing_positions}
    for message in reversed(selected):
        try:
            grounded, untouched = validate_manual_rows(
                rows, message["text"], existing_positions
            )
        except ValueError:
            asserted = _current_assertions(message["text"], existing)
            mentions = _mentioned(asserted, existing)
            if isinstance(rows, list) and any(
                _facts(asserted, _identity(row), mentions) is not None for row in rows
            ):
                raise
        else:
            # A known display name can ground just one item in an otherwise
            # ordered list. It must not short-circuit the explicit code mapping
            # for the remaining requested securities.
            if not untouched:
                _manual_provenance(grounded, message, message)
                return grounded, untouched
        # A newer denial/retraction/question must never be swallowed by a
        # matching old assertion or by an older ordered list pair. A complete
        # explicitly corrected assertion above is accepted before this barrier.
        original_mentions = _mentioned(message["text"], existing)
        requested = {_identity(row) for row in rows}
        refers_to_requested = any(
            codes & requested for _a, _b, codes, _exact in original_mentions
        )
        # "分别是" is an explicit list assertion, not the negative word "别".
        assertion = re.sub(r"分别\s*是", "", message["text"])
        doubtful = _UNASSERTED.search(assertion) or re.search(r"[？?]", assertion)
        if _WITHDRAWN.search(message["text"]) or (
            doubtful
            and (refers_to_requested or _POSITION_LABEL.search(message["text"]))
        ):
            raise ValueError(
                "最新消息撤销、否定或尚未明确当前持仓，请提供当前事实；不能回退旧数量"
            )
    pairs = []
    for quantity_message in selected:
        quantities = _ordered_quantities(quantity_message["text"])
        if not quantities:
            continue
        for code_message in selected:
            codes = _ordered_codes(code_message["text"])
            if not codes or len(codes) != len(quantities):
                continue
            elapsed = (code_message["time"] - quantity_message["time"]).total_seconds()
            if (
                quantity_message["id"] != code_message["id"]
                and quantity_message["time"].date() == code_message["time"].date()
                and 0 <= elapsed <= 900
            ):
                pairs.append((quantity_message, code_message, codes, quantities))
    if len(pairs) != 1:
        raise ValueError("跨消息持仓缺少唯一明确的分别映射，请补充代码和当前数量")
    quantity_message, code_message, codes, quantities = pairs[0]
    # This normalization is derived only from authenticated list items; model
    # row order, names, prices, market values and currencies have no authority.
    normalized = "；".join(
        f"{code} 持仓{quantity}股" for code, quantity in zip(codes, quantities)
    )
    grounded, untouched = validate_manual_rows(rows, normalized, existing_positions)
    _manual_provenance(grounded, quantity_message, code_message)
    return grounded, untouched
