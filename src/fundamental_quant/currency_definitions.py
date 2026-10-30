"""Literal yuan glossary definitions, shared by retrieval and validation.

Whitespace is ignored for recognition only. Returned quotations retain the exact
original characters, so recognition never manufactures documentary evidence.
"""

from __future__ import annotations

import re

_ALIAS = r"(?:百万元|千万元|亿元|万元|千元|元)"
_SEPARATOR = r"[、,，/及和]"
_QUALIFIER = (
    r"(?:除(?:特别注明(?:的币种)?|另有(?:说明|注明))外[，,]"
    r"(?:均)?(?:指)?|(?:如无|若无)特别(?:说明|注明)[，,](?:均)?(?:指)?)?"
)
_DEFINITION = re.compile(
    rf"(?P<aliases>{_ALIAS}(?:{_SEPARATOR}{_ALIAS})*)"
    rf"(?:指|=|：|:){_QUALIFIER}"
    rf"(?P<meanings>人民币{_ALIAS}(?:{_SEPARATOR}人民币{_ALIAS})*)"
)


def _compact(text: str) -> str:
    return "".join(text.split())


def valid_currency_definition(quote: str) -> bool:
    """Accept only explicit, matching CNY unit aliases, never functional currency."""
    if not isinstance(quote, str):
        return False
    match = _DEFINITION.fullmatch(_compact(quote).rstrip("。；;"))
    if not match:
        return False
    aliases = re.split(_SEPARATOR, match["aliases"])
    meanings = re.split(_SEPARATOR, match["meanings"].replace("人民币", ""))
    return aliases == meanings and len(aliases) == len(set(aliases))


def find_currency_definitions(text: str) -> list[str]:
    """Return exact source spans for supported glossary rows, including wrapping."""
    offsets = [index for index, char in enumerate(text) if not char.isspace()]
    compact = "".join(text[index] for index in offsets)
    definitions = []
    for match in _DEFINITION.finditer(compact):
        # Do not recognize the suffix of 美元, 人民币元, or a longer money alias.
        if match.start() and compact[match.start() - 1] in "美港欧日币十万千百亿":
            continue
        # A longer list or a foreign-currency alternative cannot be truncated.
        following = compact[match.end() : match.end() + 6]
        if re.match(r"[、,，/及和]|或|美元|港元|欧元", following):
            continue
        quote = text[offsets[match.start()] : offsets[match.end() - 1] + 1]
        if valid_currency_definition(quote):
            definitions.append(quote)
    return definitions


def currency_definition_supports_unit(quote: str, unit_quote: str) -> bool:
    """A glossary only authorizes aliases explicitly defined in that glossary."""
    if not valid_currency_definition(quote):
        return False
    definition = _DEFINITION.fullmatch(_compact(quote).rstrip("。；;"))
    aliases = set(re.split(_SEPARATOR, definition["aliases"]))
    units = re.findall(r"[十百千万亿]*元", _compact(unit_quote))
    return bool(units) and all(unit in aliases for unit in units)


# Presentation currency is distinct from a company's functional currency.
_STATEMENT_CURRENCY = re.compile(
    r"(?:本财务报表以人民币列示|"
    r"(?:除有特别说明外[，,])?本财务报表均以人民币元为单位表示)"
)
_STATEMENT_HEADING = re.compile(
    r"(?m)^[ \t]*(?:[一二三四五六七八九十\d]+[、.．][ \t]*)?"
    r"(?P<basis>合并|母公司)(?:资产负债表|利润表|现金流量表|所有者权益变动表)"
    r"[ \t]*(?:[（(]续[）)])?[ \t]*$"
)
_SECTION_END = re.compile(
    r"(?m)^[ \t]*(?:[一二三四五六七八九十\d]+[、.．][ \t]*)?"
    r"(?:公司基本情况|财务报表附注|财务报表的编制基础|重要会计政策)"
)


def find_statement_currency_declarations(text: str) -> list[str]:
    """Return exact presentation-declaration spans, never functional currency."""
    offsets = [index for index, char in enumerate(text) if not char.isspace()]
    compact = "".join(text[index] for index in offsets)
    return [
        text[offsets[m.start()] : offsets[m.end() - 1] + 1]
        for m in _STATEMENT_CURRENCY.finditer(compact)
    ]


def same_filing(left: dict, right: dict) -> bool:
    """Require an actual filing identity, not coincidentally missing keys."""
    keys = ("origin_id", "published_at", "issuer_id")
    return all(left.get(key) and left.get(key) == right.get(key) for key in keys)


def statement_context_pages(source: dict, sources: list[dict]) -> list[dict]:
    """A bounded, contiguous original-page sequence ending at the target page.

    Returned context is retrieval evidence only. The fact-level check below
    also locates the quotation relative to a statement heading and its end.
    """
    try:
        page = int(source["page"])
    except (KeyError, TypeError, ValueError):
        return []
    pages = {}
    for item in sources:
        if not same_filing(source, item):
            continue
        try:
            number = int(item["page"])
        except (KeyError, TypeError, ValueError):
            continue
        if page - 3 <= number <= page:
            # Conflicting duplicate page text cannot establish a scope.
            if number in pages and pages[number].get("text") != item.get("text"):
                return []
            pages[number] = item
    result = []
    for number in range(page, page - 4, -1):
        item = pages.get(number)
        if item is None:
            break
        result.insert(0, item)
    return result


def statement_currency_supports_fact(
    currency_quote: str,
    unit_quote: str,
    source: dict,
    unit_source: dict,
    currency_source: dict,
    sources: list[dict],
    fact_quote: str,
) -> bool:
    """Verify a CNY declaration applies to this *statement* row, not all prose.

    Scope must be proven by contiguous original pages and a named statement
    heading preceding the quoted row. Front summaries and notes do not inherit
    this narrowly supported scope. Consolidation remains a separate contract.
    """
    if not all(same_filing(source, other) for other in (unit_source, currency_source)):
        return False
    if (
        not isinstance(currency_quote, str)
        or not _STATEMENT_CURRENCY.fullmatch(_compact(currency_quote).rstrip("。；;"))
        or currency_quote not in currency_source.get("text", "")
        or not re.search(r"元", unit_quote)
        or re.search(
            r"美元|港元|港币|欧元|日元|英镑|USD|HKD|EUR|JPY|GBP",
            unit_quote,
            re.IGNORECASE,
        )
    ):
        return False
    text = source.get("text", "")
    if not fact_quote or fact_quote not in text or text.count(fact_quote) != 1:
        return False
    pages = statement_context_pages(source, sources)
    if not pages or unit_source.get("id") not in {p["id"] for p in pages}:
        return False
    prior = "\n".join(p.get("text", "") for p in pages[:-1])
    prefix = prior + "\n" + text[: text.index(fact_quote)]
    headings = list(_STATEMENT_HEADING.finditer(prefix))
    if not headings:
        return False
    segment = prefix[headings[-1].end() :]
    if _SECTION_END.search(segment) or re.search(
        r"单位\s*[:：][^\n]*(?:美元|港元|港币|欧元|日元|英镑|USD|HKD|EUR|JPY|GBP)",
        segment,
        re.IGNORECASE,
    ):
        return False
    # The unit header must lie within this statement, not a preceding table.
    return unit_quote in segment or (
        unit_source.get("id") == source.get("id") and unit_quote in fact_quote
    )
