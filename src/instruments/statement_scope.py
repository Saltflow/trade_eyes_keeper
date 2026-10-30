"""Source-specific legacy statement scope normalization without cache mutation."""

from __future__ import annotations

from copy import deepcopy

VERSION = "statement-profit-scope/2"


def normalize_statement_scope(record: dict) -> dict:
    result = deepcopy(record)
    if result.get("source") != "baostock_profit":
        return result
    parent = result.get("net_income_parent")
    average = result.get("average_parent_equity")
    if parent is None and average is None:
        return result
    total = result.get("net_income_total")
    conflict = total is not None and parent is not None and total != parent
    result["source_scope_correction"] = {
        "method": VERSION,
        "status": "conflict" if conflict else "corrected",
        "source_field": "Baostock.query_profit_data.netProfit",
        "reported_scope": "consolidated_net_income",
        "raw_fields": {
            "net_income_parent": parent,
            "net_income_total": total,
            "average_parent_equity": average,
        },
        "reason": "legacy netProfit mapped to parent income; derived parent equity therefore invalid",
        "action": "preserve consolidated amount; prohibit parent-income and parent-equity use",
        "cache_mutated": False,
    }
    if total is None:
        result["net_income_total"] = parent
    result["net_income_parent"] = None
    result["average_parent_equity"] = None
    result["diagnostics"] = list(result.get("diagnostics", [])) + [
        "baostock_netProfit_is_consolidated_not_parent",
        "legacy_parent_scope_normalized_in_memory"
        if not conflict
        else "legacy_consolidated_value_conflict",
    ]
    return result
