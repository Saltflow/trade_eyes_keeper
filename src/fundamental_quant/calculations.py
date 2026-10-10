"""Pure accounting computations. Missing observations never become zero."""

from __future__ import annotations

import math
from datetime import date
from statistics import median

from .cashflow_bridge import requirements, resolve_bridge
from .contracts import finite, issue

VERSION = "fundamental-calculations-3"
ANNUAL_CORE = (
    "revenue",
    "operating_profit",
    "net_income_parent",
    "adjusted_net_income_parent",
    "basic_eps",
    "operating_cash_flow",
    "capital_expenditures",
    "parent_equity",
    "nopat",
    "invested_capital",
    "dividends",
    "net_buybacks",
)
SPECIAL = {
    "bank": (
        "net_interest_margin",
        "credit_cost",
        "capital_adequacy",
        "nonperforming_loan_ratio",
        "provision_coverage",
        "payout_ratio",
    ),
    "insurance": ("investment_yield", "solvency_ratio", "payout_ratio"),
    "reit": (
        "affo",
        "distributable_amount",
        "dpu",
        "interest_coverage",
        "leverage",
        "financing_cost",
        "occupancy_rate",
        "nav",
    ),
}
DERIVED_INPUTS = {
    "net_debt": (
        "short_term_borrowings",
        "current_portion_noncurrent_debt",
        "long_term_borrowings",
        "bonds_payable",
        "lease_liabilities",
        "cash_and_cash_equivalents",
    ),
    "nopat": ("profit_before_tax", "income_tax_expense", "interest_expense"),
    "invested_capital": (
        "parent_equity",
        "minority_equity",
        "short_term_borrowings",
        "current_portion_noncurrent_debt",
        "long_term_borrowings",
        "bonds_payable",
        "lease_liabilities",
        "cash_and_cash_equivalents",
    ),
    "net_reinvestment": (
        "capital_expenditures",
        "depreciation_amortization",
        "operating_working_capital",
        "acquisitions_cash",
    ),
    "equity_fcf": (
        "operating_cash_flow",
        "capital_expenditures",
        "interest_not_in_cfo",
        "lease_principal",
        "sbc_cost",
        "maintenance_acquisitions",
    ),
}

# A balance sheet is a dated stock, even when printed in an annual report.
# Fiscal dates must come from reported annual flows, never a December assumption.
ANNUAL_FLOW_ANCHORS = frozenset(
    {
        "revenue",
        "operating_profit",
        "net_income_parent",
        "adjusted_net_income_parent",
        "operating_cash_flow",
        "profit_before_tax",
        "income_tax_expense",
    }
)
ANNUAL_BALANCES = frozenset(
    {
        "parent_equity",
        "minority_equity",
        "total_assets",
        "total_liabilities",
        "short_term_borrowings",
        "current_portion_noncurrent_debt",
        "long_term_borrowings",
        "bonds_payable",
        "lease_liabilities",
        "cash_and_cash_equivalents",
        "restricted_cash",
        "operating_working_capital",
    }
)


def input_metrics(names):
    result = set(names)
    for name in names:
        result.update(DERIVED_INPUTS.get(name, ()))
    return result


class Facts:
    def __init__(self, facts: list[dict]):
        self.facts = facts

    def rows(self, metric: str, kind: str | None = None) -> list[dict]:
        observed = [
            f
            for f in self.facts
            if f["metric"] == metric and (kind is None or f["period_type"] == kind)
        ]
        if metric == "net_debt":
            components = DERIVED_INPUTS["net_debt"]
            cells = {}
            for fact in self.facts:
                if fact["metric"] in components and (
                    kind is None or fact["period_type"] == kind
                ):
                    key = (
                        fact["period_end"],
                        fact["period_type"],
                        fact["currency"],
                        fact["unit"],
                    )
                    previous = cells.setdefault(key, {}).get(fact["metric"])
                    if (
                        previous is None
                        or fact["published_at"] > previous["published_at"]
                    ):
                        cells[key][fact["metric"]] = fact
            observed_keys = {(f["period_end"], f["period_type"]) for f in observed}
            for (period, period_type, currency, unit), inputs in cells.items():
                if (
                    set(inputs) != set(components)
                    or (period, period_type) in observed_keys
                    or inputs["cash_and_cash_equivalents"].get("basis")
                    != "cash_equivalents_excludes_restricted"
                ):
                    continue
                # No missing component becomes zero. References point to all
                # actual disclosures; this value is never a fabricated quote.
                observed.append(
                    {
                        "id": f"derived:net_debt:{period}:{period_type}",
                        "metric": "net_debt",
                        "period_end": period,
                        "period_type": period_type,
                        "currency": currency,
                        "unit": unit,
                        "published_at": max(f["published_at"] for f in inputs.values()),
                        "value": sum(inputs[name]["value"] for name in components[:5])
                        - inputs["cash_and_cash_equivalents"]["value"],
                        "status": "derived",
                        "fact_refs": [inputs[name]["id"] for name in components],
                    }
                )
        return sorted(
            observed,
            key=lambda f: (f["period_end"], f["published_at"]),
        )

    def latest(self, metric: str, kind: str | None = None) -> float | None:
        rows = self.rows(metric, kind)
        return rows[-1]["value"] if rows else None

    def annual_rows(self, metric: str) -> tuple[list[dict], list[dict]]:
        """Resolve fiscal closing stocks without rewriting their raw period type."""
        annual = self.rows(metric, "annual")
        if metric not in ANNUAL_BALANCES:
            return annual, []
        fiscal_dates = {
            f["period_end"]
            for f in self.facts
            if f["period_type"] == "annual" and f["metric"] in ANNUAL_FLOW_ANCHORS
        }
        candidates = annual + [
            f
            for f in self.rows(metric, "instant")
            if f["period_end"] in fiscal_dates
            and f.get("basis") in {None, "end", "cash_equivalents_excludes_restricted"}
        ]
        grouped = {}
        for row in candidates:
            grouped.setdefault(int(row["period_end"][:4]), []).append(row)
        resolved, errors = [], []
        for year, rows in grouped.items():
            first = rows[0]
            conflict = any(
                row["period_end"] != first["period_end"]
                or any(row.get(k) != first.get(k) for k in ("currency", "unit"))
                or not math.isclose(
                    row["value"],
                    first["value"],
                    rel_tol=1e-12,
                    abs_tol=max(
                        row.get("precision", 0) * row.get("multiplier", 1),
                        first.get("precision", 0) * first.get("multiplier", 1),
                    )
                    / 2,
                )
                for row in rows[1:]
            )
            if conflict:
                errors.append(
                    issue(
                        "conflicting_annual_balance_observations",
                        metric,
                        f"fiscal year {year}: annual and closing balances disagree",
                        stage="calculate",
                    )
                )
                continue
            # Keep legacy annual observations when consistent. Cash definitions
            # must remain explicit on the selected original observation.
            resolved.append(
                next((r for r in rows if r["period_type"] == "annual"), first)
            )
        return resolved, errors

    def annual_balance_errors(self) -> list[dict]:
        return [
            error
            for metric in sorted(ANNUAL_BALANCES)
            for error in self.annual_rows(metric)[1]
        ]

    def annual(self, metric: str) -> dict[int, float]:
        observed = {
            date.fromisoformat(f["period_end"]).year: f["value"]
            for f in self.annual_rows(metric)[0]
        }
        if metric == "nopat":
            profit, tax, interest = (
                self.annual(m)
                for m in ("profit_before_tax", "income_tax_expense", "interest_expense")
            )
            derived = {
                y: (profit[y] + interest[y]) * (1 - tax[y] / profit[y])
                for y in profit.keys() & tax.keys() & interest.keys()
                if profit[y] > 0 and 0 <= tax[y] / profit[y] <= 1
            }
            return {**derived, **observed}
        if metric == "invested_capital":
            names = (
                "parent_equity",
                "minority_equity",
                "short_term_borrowings",
                "current_portion_noncurrent_debt",
                "long_term_borrowings",
                "bonds_payable",
                "lease_liabilities",
                "cash_and_cash_equivalents",
            )
            rows = {m: self.annual(m) for m in names}
            rows["cash_and_cash_equivalents"] = {
                int(f["period_end"][:4]): f["value"]
                for f in self.annual_rows("cash_and_cash_equivalents")[0]
                if f.get("basis") == "cash_equivalents_excludes_restricted"
            }
            years = set.intersection(*(set(v) for v in rows.values()))
            end_capital = {
                y: sum(rows[m][y] for m in names[:7])
                - rows["cash_and_cash_equivalents"][y]
                for y in years
            }
            derived = {
                y: (v + end_capital[y - 1]) / 2
                for y, v in end_capital.items()
                if y - 1 in end_capital
            }
            return {**derived, **observed}
        if metric == "net_reinvestment":
            capex, dep, wc, acquisitions = (
                self.annual(m)
                for m in (
                    "capital_expenditures",
                    "depreciation_amortization",
                    "operating_working_capital",
                    "acquisitions_cash",
                )
            )
            derived = {
                y: capex[y] - dep[y] + wc[y] - wc[y - 1] + acquisitions[y]
                for y in capex.keys() & dep.keys() & wc.keys() & acquisitions.keys()
                if y - 1 in wc
            }
            return {**derived, **observed}
        return observed

    def ttm(self, metric: str) -> float | None:
        direct = self.rows(metric, "ttm")
        annual = self.rows(metric, "annual")
        interim = self.rows(metric, "interim")
        if direct and (
            not interim or direct[-1]["period_end"] >= interim[-1]["period_end"]
        ):
            return direct[-1]["value"]
        if not annual:
            return None
        last = annual[-1]
        if not interim or interim[-1]["period_end"] <= last["period_end"]:
            return last["value"]
        current = interim[-1]
        now = date.fromisoformat(current["period_end"])
        # Only explicitly cumulative interim observations can be combined.
        if not current.get("is_cumulative"):
            return None
        prior = [
            f
            for f in interim
            if f["period_end"] == f"{now.year - 1}{now.isoformat()[4:]}"
            and f.get("is_cumulative")
        ]
        if not prior or date.fromisoformat(last["period_end"]).year != now.year - 1:
            return None
        return last["value"] + current["value"] - prior[-1]["value"]


def _ratio(numerator, denominator):
    return (
        numerator / denominator
        if finite(numerator) and finite(denominator) and denominator > 0
        else None
    )


def cagr(values: dict[int, float]) -> dict:
    if len(values) < 2:
        return {"value": None, "status": "missing"}
    first, last = min(values), max(values)
    if values[first] <= 0 or values[last] <= 0:
        return {
            "value": None,
            "status": "not_meaningful",
            "absolute_change": values[last] - values[first],
            "interval_years": last - first,
        }
    return {
        "value": (values[last] / values[first]) ** (1 / (last - first)) - 1,
        "status": "derived",
        "start_year": first,
        "end_year": last,
        "interval_years": last - first,
    }


def accounting_checks(packet):
    """Check available identities at the same date, with rounding-error bounds."""
    cells = {
        (f["metric"], f["period_end"], f["period_type"]): f for f in packet["facts"]
    }
    periods = {(f["period_end"], f["period_type"]) for f in packet["facts"]}
    errors = []
    for end, kind in periods:
        names = (
            "total_assets",
            "total_liabilities",
            "parent_equity",
            "minority_equity",
        )
        rows = [cells.get((name, end, kind)) for name in names]
        if all(row is not None for row in rows):
            delta = rows[0]["value"] - sum(row["value"] for row in rows[1:])
            tolerance = sum(
                row.get("precision", 0) * row.get("multiplier", 1) / 2 for row in rows
            )
            # The small numerical allowance accounts only for binary float ULPs.
            tolerance += sum(math.ulp(float(row["value"])) for row in rows) * 4
            if abs(delta) > tolerance:
                errors.append(
                    issue("balance_sheet_does_not_reconcile", end, stage="calculate")
                )
        price, shares, cap = (
            cells.get((name, end, kind))
            for name in ("price", "total_shares", "market_cap")
        )
        if (
            price
            and shares
            and cap
            and packet["company"].get("single_share_class") is True
        ):
            p, s = price["value"], shares["value"]
            ep, es, ec = (
                r.get("precision", 0) * r.get("multiplier", 1) / 2
                for r in (price, shares, cap)
            )
            tolerance = (
                abs(s) * ep
                + abs(p) * es
                + ep * es
                + ec
                + math.ulp(float(cap["value"])) * 8
            )
            if abs(p * s - cap["value"]) > tolerance:
                errors.append(
                    issue("price_shares_market_cap_conflict", end, stage="calculate")
                )
    return errors


def screen(packet: dict, as_of: date, settings: dict) -> dict:
    facts = Facts(packet["facts"])
    company = packet["company"]
    template = company["template"]
    errors, checks = [], {}
    market_cap = facts.latest("market_cap")
    checks["market_cap"] = market_cap > 1e9 if market_cap is not None else None
    if template == "reit":
        checks.pop("market_cap")
    if company.get("classification_verified") is not True:
        errors.append(issue("industry_classification_required", stage="screen"))
    membership_unverified = (
        company.get("listing_status")
        in {"pending", "provisional", "inactive", "out_of_universe"}
        or company.get("listing_verified") is False
    )
    if membership_unverified:
        errors.append(issue("universe_membership_unverified", stage="screen"))
    pe = facts.latest("pe_ttm")
    adj_pe = facts.latest("adjusted_pe_ttm")
    pe = pe if pe is not None else _ratio(market_cap, facts.ttm("net_income_parent"))
    adj_pe = (
        adj_pe
        if adj_pe is not None
        else _ratio(market_cap, facts.ttm("adjusted_net_income_parent"))
    )
    pb = facts.latest("pb")
    pb = pb if pb is not None else _ratio(market_cap, facts.latest("parent_equity"))
    if template != "reit":
        checks["pe_ttm"] = 0 < pe < 25 if pe is not None else None
        checks["adjusted_pe_ttm"] = 0 < adj_pe < 25 if adj_pe is not None else None
    if template in {"ordinary", "cyclical"}:
        debt = _ratio(facts.latest("total_liabilities"), facts.latest("total_assets"))
        checks["debt_assets"] = debt < 0.70 if debt is not None else None
    profits = facts.annual("adjusted_net_income_parent")
    last_year = max(profits, default=as_of.year - 1)
    expected_year = as_of.year - (1 if as_of.month >= 5 else 2)
    if last_year < expected_year:
        errors.append(issue("annual_history_stale", stage="screen"))
    proxy = None
    if template == "reit":
        proxy = _ratio(facts.latest("affo"), market_cap)
    elif company.get("valuation_path") == "pb" or template in {"bank", "insurance"}:
        roes = facts.annual("adjusted_roe")
        equities = facts.annual("average_parent_equity")
        for year, profit in profits.items():
            if year not in roes and equities.get(year, 0) > 0:
                roes[year] = profit / equities[year]
        values = [v for y, v in roes.items() if last_year - 4 <= y <= last_year]
        if len(values) >= 3:
            proxy = _ratio(median(values), pb)
    elif company.get("valuation_path") == "pe":
        years = range(last_year - 2, last_year + 1)
        if all(y in profits for y in years):
            proxy = _ratio(median(profits[y] for y in years), market_cap)
    checks["earnings_proxy"] = proxy > 0.07 if proxy is not None else None
    quote_rows = facts.rows("market_cap")
    if not quote_rows or (
        as_of - date.fromisoformat(quote_rows[-1]["period_end"])
    ).days > settings.get("max_quote_age_days", 7):
        errors.append(issue("quote_stale_or_missing", stage="screen"))
    if company.get("classification_verified") is not True or membership_unverified:
        result = "unknown"
    elif any(value is False for value in checks.values()):
        # A cycle exception is a research route, never a completed screen pass.
        cycle = (
            template == "cyclical"
            and checks.get("market_cap") is True
            and checks.get("debt_assets") is True
            and not errors
        )
        result = "cycle_exception" if cycle else "fail"
    elif any(value is None for value in checks.values()) or errors:
        result = "unknown"
    else:
        result = "pass"
    errors.extend(
        issue("screen_metric_missing", k, stage="screen")
        for k, v in checks.items()
        if v is None
    )
    return {
        "status": result,
        "checks": checks,
        "earnings_proxy": proxy,
        "errors": errors,
    }


def readiness(packet: dict, as_of: date) -> list[dict]:
    """Research readiness is strictly stronger than screening readiness."""
    company, facts = packet["company"], Facts(packet["facts"])
    errors = facts.annual_balance_errors()
    template = company["template"]
    incomplete_originals = {
        s.get("origin_id", s.get("id"))
        for s in packet["sources"]
        if (s.get("document_kind") or s.get("kind")) in {"annual", "interim"}
        and s.get("document_status") == "incomplete_text"
    }
    if incomplete_originals:
        errors.append(
            issue(
                "original_document_incomplete",
                "originals",
                f"{len(incomplete_originals)} original reports contain unreadable pages",
                stage="evidence",
            )
        )
    required = (
        ANNUAL_CORE
        if template in {"ordinary", "cyclical"}
        else ("adjusted_net_income_parent", "parent_equity")
    )
    if template == "reit":
        required = ("affo", "distributable_amount", "dpu")
    width = 10 if template == "cyclical" else 7
    if template == "reit":
        width = min(7, as_of.year - int(company.get("listing_year", as_of.year)))
        width = max(3, width)
    anchors = facts.annual(required[0])
    end = max(anchors, default=as_of.year - 1)
    if end < as_of.year - (1 if as_of.month >= 5 else 2):
        errors.append(issue("latest_annual_missing", stage="evidence"))
    for metric in required:
        values = facts.annual(metric)
        missing = [
            year for year in range(end - width + 1, end + 1) if year not in values
        ]
        if missing:
            errors.append(
                issue(
                    "history_incomplete",
                    metric,
                    f"missing fiscal years: {missing}",
                    stage="evidence",
                )
            )
    for metric in SPECIAL.get(template, ()):
        if facts.latest(metric) is None:
            errors.append(issue("sector_metric_missing", metric, stage="evidence"))
    if template == "insurance":
        subtype = company.get("insurance_type")
        metric = {"life": "new_business_value", "property": "combined_ratio"}.get(
            subtype
        )
        if not metric or facts.latest(metric) is None:
            errors.append(issue("insurance_business_type_required", stage="evidence"))
    if (
        template == "reit"
        and company.get("finite_life") is not False
        and facts.latest("asset_life_years") is None
    ):
        errors.append(issue("reit_remaining_life_required", stage="evidence"))
    docs = [
        s
        for s in packet["sources"]
        if s.get("kind") in {"annual", "interim"}
        and s.get("page", 0) > 0
        and s.get("url")
    ]
    years = {
        date.fromisoformat(s["period_end"]).year
        for s in docs
        if s.get("kind") == "annual" and s.get("period_end")
    }
    if not all(year in years for year in range(end - 2, end + 1)):
        errors.append(issue("three_annual_originals_required", stage="evidence"))
    # As of September the current June interim must have been disclosed.
    interim_year = as_of.year if as_of.month >= 9 else as_of.year - 1
    if not any(
        s.get("kind") == "interim"
        and str(s.get("period_end", "")).startswith(str(interim_year))
        for s in docs
    ):
        errors.append(issue("latest_interim_original_required", stage="evidence"))
    source_ids = {
        s["id"] for s in docs if s.get("verification_level") != "provider_parsed"
    }
    primary_fact_ids = {
        f["id"] for f in packet["facts"] if f.get("source_id") in source_ids
    }

    def primary(row):
        return row.get("source_id") in source_ids or (
            row.get("status") == "derived"
            and bool(row.get("fact_refs"))
            and set(row["fact_refs"]).issubset(primary_fact_ids)
        )

    primary_keys = {
        "ordinary": ("adjusted_net_income_parent", "parent_equity", "net_debt"),
        "cyclical": ("adjusted_net_income_parent", "parent_equity", "net_debt"),
        "bank": (
            "adjusted_net_income_parent",
            "parent_equity",
            "capital_adequacy",
            "nonperforming_loan_ratio",
        ),
        "insurance": ("adjusted_net_income_parent", "parent_equity", "solvency_ratio"),
        "reit": ("affo", "nav", "leverage"),
    }[template]
    for metric in primary_keys:
        if not any(primary(row) for row in facts.rows(metric)):
            errors.append(
                issue(
                    "key_metric_original_crosscheck_required", metric, stage="evidence"
                )
            )
    if template in {"ordinary", "cyclical"} and not any(
        f["metric"] == "operating_cash_flow" and f["source_id"] in source_ids
        for f in packet["facts"]
    ):
        errors.append(
            issue(
                "key_metric_original_crosscheck_required",
                "operating_cash_flow",
                stage="evidence",
            )
        )
    # Corroborate the actual normalization cells, not an arbitrary old original
    # bearing the same metric name. Equal facts from independent origins remain
    # distinct; multiple vendors repeating one filing count as one origin.
    primary_cells = {
        (f["metric"], f["period_end"], f["period_type"])
        for f in packet["facts"]
        if f["source_id"] in source_ids
    }
    primary_cells.update(
        ("net_debt", f["period_end"], f["period_type"])
        for f in facts.rows("net_debt")
        if primary(f)
    )
    normalized_metrics = (
        ("affo",) if template == "reit" else ("adjusted_net_income_parent",)
    )
    if template in {"ordinary", "cyclical"}:
        normalized_metrics += ("operating_cash_flow", "capital_expenditures")
    window = 10 if template == "cyclical" else 5
    for metric in normalized_metrics:
        for row in facts.rows(metric, "annual"):
            if (
                end - window < int(row["period_end"][:4]) <= end
                and (metric, row["period_end"], "annual") not in primary_cells
            ):
                errors.append(
                    issue(
                        "normalization_cell_primary_required",
                        row["id"],
                        stage="evidence",
                    )
                )
    for metric in primary_keys[1:]:
        rows = facts.rows(metric)
        if (
            rows
            and (metric, rows[-1]["period_end"], rows[-1]["period_type"])
            not in primary_cells
        ):
            errors.append(
                issue("current_cell_primary_required", rows[-1]["id"], stage="evidence")
            )
    return errors


def calculate(packet: dict, adjustments=(), settings=None) -> tuple[dict, list[dict]]:
    settings = settings or {}
    f = Facts(packet["facts"])
    template = packet["company"]["template"]
    errors = accounting_checks(packet) + f.annual_balance_errors()
    metrics = {}
    currency = {
        row["currency"]
        for row in packet["facts"]
        if row["unit"] in {"currency", "money"}
    }
    if len(currency) > 1:
        return {}, [
            issue("mixed_currency_requires_explicit_conversion", stage="calculate")
        ]

    def put(name, value, formula, inputs=()):
        expanded = input_metrics(inputs)
        metrics[name] = {
            "value": value,
            "status": "derived" if value is not None else "missing",
            "formula": formula,
            "fact_refs": [
                row["id"] for row in packet["facts"] if row["metric"] in expanded
            ],
        }

    series = {name: f.annual(name) for name in ANNUAL_CORE}
    series["affo"] = f.annual("affo")
    fcf = {
        y: v - series["capital_expenditures"][y]
        for y, v in series["operating_cash_flow"].items()
        if y in series["capital_expenditures"]
    }
    series["equity_fcf"] = dict(fcf)
    bridge_rows = {}
    # Accounting interpretations live in a separately reviewed bridge, never
    # masquerading as printed raw observations. Historical raw FCF stays intact.
    if template in {"ordinary", "cyclical"}:
        bridge_rows, bridge_errors = resolve_bridge(packet, settings)
        errors += bridge_errors
        years = {year for _, year in requirements(packet, settings)}
        series["equity_fcf"] = {
            year: fcf[year]
            - sum(row["amount"] for (name, y), row in bridge_rows.items() if y == year)
            for year in years
            if year in fcf and sum(y == year for _, y in bridge_rows) == 4
        }
    for adj in adjustments:
        metric = adj.get("metric")
        if metric not in {"adjusted_net_income_parent", "equity_fcf", "nopat", "affo"}:
            errors.append(
                issue("adjustment_metric_not_allowed", str(metric), stage="calculate")
            )
            continue
        try:
            year = date.fromisoformat(adj["period_end"]).year
            if year not in series[metric]:
                raise ValueError("missing base observation")
            referenced = [
                row for row in packet["facts"] if row["id"] in adj.get("fact_refs", [])
            ]
            originals = {
                s["id"]
                for s in packet["sources"]
                if s.get("kind") in {"annual", "interim"}
                and s.get("url")
                and s.get("page", 0) > 0
                and s.get("verification_level") != "provider_parsed"
            }
            if not referenced or any(
                row["currency"] != adj.get("currency")
                or row["unit"] != adj.get("unit")
                or row["period_end"] != adj["period_end"]
                or row["source_id"] not in originals
                for row in referenced
            ):
                raise ValueError("adjustment accounting basis mismatch")
            if metric == "equity_fcf" and adj.get("amount", 0) < 0:
                deducted_refs = {
                    ref
                    for row in bridge_rows.values()
                    if row["amount"] > 0
                    for ref in row["fact_refs"]
                }
                if deducted_refs.intersection(adj.get("fact_refs", [])):
                    raise ValueError("expenditure already deducted in cashflow bridge")
            # An adjustment must tie to a recorded observed amount. A model
            # cannot invent a discretionary haircut disguised as a fact.
            if not any(
                math.isclose(
                    abs(row["value"]),
                    abs(adj["amount"]),
                    rel_tol=1e-9,
                    abs_tol=row.get("precision", 0) / 2,
                )
                for row in referenced
            ):
                raise ValueError("adjustment amount not supported by fact")
            series[metric][year] += adj["amount"]
        except (KeyError, ValueError, TypeError) as exc:
            errors.append(
                issue(
                    "invalid_adjustment_basis",
                    str(adj.get("id")),
                    str(exc),
                    stage="calculate",
                )
            )
    normalization_anchor = max(
        series.get("affo" if template == "reit" else "adjusted_net_income_parent", {}),
        default=0,
    )

    def norm(name):
        rows = series.get(name, f.annual(name))
        if not rows:
            return None
        end = normalization_anchor
        width = (
            settings.get("normalized_fcf_years", 5)
            if name == "equity_fcf"
            else 10
            if template == "cyclical"
            else 5
        )
        values = [v for y, v in rows.items() if end - width + 1 <= y <= end]
        count = (
            10
            if template == "cyclical" and name != "equity_fcf"
            else min(width, len(values))
        )
        # Missing recent years cannot be hidden by normalizing an older subset.
        consecutive = count >= 3 and all(
            y in rows for y in range(end - count + 1, end + 1)
        )
        return (
            median(rows[y] for y in range(end - count + 1, end + 1))
            if consecutive
            else None
        )

    earnings, equity, market = (
        norm("adjusted_net_income_parent"),
        f.latest("parent_equity"),
        f.latest("market_cap"),
    )
    if template == "cyclical":
        years = list(range(normalization_anchor - 9, normalization_anchor + 1))
        if all(
            series["revenue"].get(y, 0) > 0
            and y in series["adjusted_net_income_parent"]
            for y in years
        ):
            cycle_margin = median(
                series["adjusted_net_income_parent"][y] / series["revenue"][y]
                for y in years
            )
            earnings = median(series["revenue"][y] for y in years) * cycle_margin
            put(
                "mid_cycle_net_margin",
                cycle_margin,
                "ten-year median(adjusted parent earnings/revenue)",
                ["adjusted_net_income_parent", "revenue"],
            )
        else:
            earnings = None
            errors.append(issue("mid_cycle_margin_history_required", stage="calculate"))
    for name, expected in (
        ("pb", _ratio(market, equity)),
        ("pe_ttm", _ratio(market, f.ttm("net_income_parent"))),
        ("adjusted_pe_ttm", _ratio(market, f.ttm("adjusted_net_income_parent"))),
    ):
        quoted = f.rows(name)
        if quoted and expected is not None:
            value = quoted[-1]
            tolerance = value.get("precision", 0) * value.get("multiplier", 1) / 2
            if not math.isclose(
                value["value"], expected, rel_tol=1e-6, abs_tol=tolerance
            ):
                errors.append(
                    issue(
                        "quoted_multiple_accounting_conflict",
                        name,
                        "Quoted multiple does not reconcile with dated earnings/equity and current market cap",
                        stage="calculate",
                    )
                )
    roe = _ratio(earnings, equity)
    roic_years = {
        y: series["nopat"][y] / value
        for y, value in series["invested_capital"].items()
        if value > 0
        and y in series["nopat"]
        and (
            not any(
                row["period_end"].startswith(str(y))
                for row in f.rows("invested_capital")
            )
            or any(
                row.get("basis") == "average" and row["period_end"].startswith(str(y))
                for row in f.rows("invested_capital")
            )
        )
    }
    roic_values = [
        roic_years[y]
        for y in range(
            normalization_anchor - (9 if template == "cyclical" else 4),
            normalization_anchor + 1,
        )
        if y in roic_years
    ]
    roic = (
        median(roic_values)
        if len(roic_values) >= (10 if template == "cyclical" else 3)
        and all(
            y in roic_years
            for y in range(normalization_anchor - 2, normalization_anchor + 1)
        )
        else None
    )
    pe, pb = _ratio(market, earnings), _ratio(market, equity)
    growth, hurdle = settings.get("terminal_growth", 0.02), settings.get("hurdle", 0.07)
    if not 0 <= growth < hurdle:
        return {}, [issue("invalid_growth_hurdle", stage="calculate")]
    ke_pb = (
        growth + (roe - growth) / pb
        if roe is not None and pb and pb > 0 and roe > growth
        else None
    )
    ke_pe = (
        growth + (1 - growth / roe) / pe
        if roe and roe > growth and pe and pe > 0
        else None
    )
    normalized_fcf = norm("equity_fcf")
    average_equities = f.annual("average_parent_equity")
    historic_roes = {
        y: value / average_equities[y]
        for y, value in series["adjusted_net_income_parent"].items()
        if average_equities.get(y, 0) > 0
    }
    historic_values = [
        historic_roes[y]
        for y in range(normalization_anchor - 6, normalization_anchor + 1)
        if y in historic_roes
    ]
    for name, value, formula, inputs in (
        (
            "normalized_earnings",
            earnings,
            "median(adjusted annual earnings)",
            ["adjusted_net_income_parent"],
        ),
        (
            "normalized_roe",
            roe,
            "normalized forward earnings/current parent equity",
            ["adjusted_net_income_parent", "parent_equity"],
        ),
        (
            "median_historical_roe",
            median(historic_values) if len(historic_values) >= 3 else None,
            "median(annual adjusted parent earnings/average parent equity), up to seven years",
            ["adjusted_net_income_parent", "average_parent_equity"],
        ),
        (
            "normalized_pe",
            pe,
            "current market cap/normalized parent earnings",
            ["market_cap", "adjusted_net_income_parent"],
        ),
        (
            "current_pb",
            pb,
            "current market cap/current parent equity",
            ["market_cap", "parent_equity"],
        ),
        ("net_debt", f.latest("net_debt"), "latest reported net debt", ["net_debt"]),
        (
            "net_debt_to_market_cap",
            _ratio(f.latest("net_debt"), market),
            "net debt/current market cap",
            ["net_debt", "market_cap"],
        ),
        (
            "normalized_roic",
            roic,
            "median(NOPAT/average invested capital)",
            ["nopat", "invested_capital"],
        ),
        (
            "normalized_fcf",
            normalized_fcf,
            "median(CFO-capex-interest-lease-SBC-maintenance acquisitions+verified adjustments)",
            [
                "operating_cash_flow",
                "capital_expenditures",
                "interest_not_in_cfo",
                "lease_principal",
                "sbc_cost",
                "maintenance_acquisitions",
            ],
        ),
        (
            "raw_fcf_latest",
            fcf[max(fcf)] if fcf else None,
            "CFO-capex",
            ["operating_cash_flow", "capital_expenditures"],
        ),
        (
            "implied_ke_pb",
            ke_pb,
            "g+(ROE-g)/PB",
            ["market_cap", "parent_equity", "adjusted_net_income_parent"],
        ),
        (
            "implied_ke_pe",
            ke_pe,
            "g+(1-g/ROE)/normalized_PE",
            ["market_cap", "parent_equity", "adjusted_net_income_parent"],
        ),
        (
            "fcf_yield",
            _ratio(normalized_fcf, market),
            "normalized equity FCF/market cap",
            ["market_cap", "equity_fcf"],
        ),
        (
            "affo_yield",
            _ratio(norm("affo"), market),
            "normalized AFFO/market cap",
            ["affo", "market_cap"],
        ),
        (
            "normalized_affo",
            norm("affo"),
            "median(annual AFFO with verified adjustments)",
            ["affo"],
        ),
        (
            "safety_margin",
            ke_pb - hurdle if ke_pb is not None else None,
            "implied Ke-hurdle (fraction; display in percentage points)",
            ["market_cap", "parent_equity", "adjusted_net_income_parent"],
        ),
        (
            "fcf_conversion",
            _ratio(normalized_fcf, norm("net_income_parent")),
            "normalized equity FCF/normalized parent earnings",
            ["equity_fcf", "net_income_parent"],
        ),
        (
            "reinvestment_rate",
            _ratio(norm("net_reinvestment"), norm("nopat")),
            "normalized net reinvestment/normalized NOPAT",
            ["net_reinvestment", "nopat"],
        ),
        (
            "shareholder_yield",
            _ratio(
                (norm("dividends") + norm("net_buybacks"))
                if norm("dividends") is not None and norm("net_buybacks") is not None
                else None,
                market,
            ),
            "(dividends+net buybacks)/market cap",
            ["dividends", "net_buybacks", "market_cap"],
        ),
    ):
        put(name, value, formula, inputs)
    for metric in metrics.values():
        metric["adjustment_refs"] = [adj["id"] for adj in adjustments]
    if template == "cyclical":
        metrics["normalized_earnings"]["normalization_method"] = (
            "ten_year_median_revenue_times_mid_cycle_adjusted_net_margin"
        )
        metrics["normalized_earnings"]["formula"] = (
            "median(ten-year revenue)*median(ten-year adjusted earnings/revenue)"
        )
        metrics["normalized_earnings"]["fact_refs"] += [
            r["id"] for r in packet["facts"] if r["metric"] == "revenue"
        ]
    series["raw_fcf"] = fcf
    for name in ("revenue", "adjusted_net_income_parent", "equity_fcf", "raw_fcf"):
        rows = series[name]
        if rows:
            end = max(rows)
            metrics[f"{name}_cagr"] = {
                **cagr({y: v for y, v in rows.items() if end - 7 <= y <= end}),
                "cashflow_basis": "CFO-capex"
                if name == "raw_fcf"
                else "reviewed shareholder bridge"
                if name == "equity_fcf"
                else None,
                "fact_refs": [
                    r["id"]
                    for r in packet["facts"]
                    if r["metric"]
                    in input_metrics(
                        [name]
                        if name != "raw_fcf"
                        else ["operating_cash_flow", "capital_expenditures"]
                    )
                ],
            }
    for name in (
        "price",
        "market_cap",
        "enterprise_value",
        "pe_ttm",
        "adjusted_pe_ttm",
        "pb",
        "dividend_yield",
        "total_shares",
        "ev_ebit",
    ):
        output_name = "quoted_pb" if name == "pb" else f"current_{name}"
        put(
            output_name,
            f.latest(name),
            "latest verified observation; missing remains unknown",
            [name],
        )
    put(
        "growth_to_fcf_yield_sanity",
        _ratio(
            metrics.get("raw_fcf_cagr", {}).get("value"),
            metrics["fcf_yield"]["value"],
        ),
        "observed raw CFO-capex CAGR/normalized shareholder FCF yield; different cashflow bases, sanity check only",
        ["equity_fcf", "market_cap"],
    )
    for metric in metrics.values():
        metric["normalization_end_year"] = normalization_anchor or None
        metric["normalization_window_years"] = 10 if template == "cyclical" else 5
    for name in ("normalized_fcf", "fcf_yield", "fcf_conversion", "equity_fcf_cagr"):
        if name in metrics:
            metrics[name]["normalization_window_years"] = settings.get(
                "normalized_fcf_years", 5
            )
            proposals = [
                row
                for row in bridge_rows.values()
                if row.get("origin") != "reported_fact"
            ]
            metrics[name]["bridge_refs"] = [row["id"] for row in proposals]
            metrics[name]["fact_refs"] = list(
                dict.fromkeys(
                    [
                        *metrics[name].get("fact_refs", []),
                        *(ref for row in proposals for ref in row["fact_refs"]),
                    ]
                )
            )
    value_pass = (
        ke_pb is not None and ke_pe is not None and ke_pb >= hurdle and ke_pe >= hurdle
    )
    cash_pass = (
        metrics["fcf_yield"]["value"] is not None
        and metrics["fcf_yield"]["value"] >= hurdle
    )
    if template == "reit":
        eligible = (
            metrics["affo_yield"]["value"] is not None
            and metrics["affo_yield"]["value"] >= hurdle
        )
    elif template in {"bank", "insurance"}:
        eligible = value_pass
    else:
        eligible = value_pass and cash_pass and roic is not None
    metrics["hurdle_pass"] = {
        "value": eligible and not errors,
        "value_gate": value_pass if template != "reit" else None,
        "cash_gate": cash_pass if template in {"ordinary", "cyclical"} else None,
        "affo_gate": eligible if template == "reit" else None,
        "gate_state": "both_pass"
        if template in {"ordinary", "cyclical"} and value_pass and cash_pass
        else "one_gate_only"
        if template in {"ordinary", "cyclical"} and value_pass != cash_pass
        else "sector_pass"
        if eligible
        else "not_passed",
        "hurdle": hurdle,
        "status": "derived",
        "fact_refs": list(
            dict.fromkeys(
                ref
                for metric in metrics.values()
                for ref in metric.get("fact_refs", [])
            )
        ),
    }
    return metrics, errors


def scenarios(
    packet: dict, metrics: dict, rows: list[dict], settings: dict
) -> tuple[list[dict], list[dict]]:
    """Three-stage residual income; finite-life REIT cashflows have no perpetuity."""
    if (
        not isinstance(rows, list)
        or {r.get("name") for r in rows if isinstance(r, dict)}
        != {"conservative", "base", "optimistic"}
        or len(rows) != 3
    ):
        return [], [issue("three_scenarios_required", stage="calculate")]
    f, results, errors = Facts(packet["facts"]), [], []
    hurdle, terminal = (
        settings.get("hurdle", 0.07),
        settings.get("terminal_growth", 0.02),
    )
    source_ids = {s["id"] for s in packet["sources"]}
    template = packet["company"]["template"]
    for row in rows:
        g1, g2, rate = (
            row.get("growth_1_5"),
            row.get("growth_6_10"),
            row.get("reinvestment_rate"),
        )
        if (
            not all(finite(v) for v in (g1, g2, rate))
            or not 0 <= rate <= 1
            or min(g1, g2) <= -1
            or not row.get("source_refs")
            or not set(row["source_refs"]).issubset(source_ids)
        ):
            errors.append(
                issue("invalid_scenario", row.get("name", ""), stage="calculate")
            )
            continue
        roe = metrics["normalized_roe"]["value"]
        capital_return = (
            roe
            if template in {"bank", "insurance"}
            else metrics["normalized_roic"]["value"]
        )
        if template != "reit" and (
            capital_return is None
            or roe is None
            or max(g1, g2, terminal) > min(capital_return * rate, roe)
        ):
            errors.append(
                issue(
                    "growth_exceeds_reinvestment_capacity",
                    row["name"],
                    stage="calculate",
                )
            )
            continue
        if template == "reit":
            cash = metrics["normalized_affo"]["value"]
            life = f.latest("asset_life_years")
            if cash is None or (
                packet["company"].get("finite_life") is not False
                and (life is None or not 0 < life <= 100)
            ):
                errors.append(issue("reit_life_or_cashflow_missing", stage="calculate"))
                continue
            horizon = int(life) if life is not None else 10
            value = 0
            for year in range(1, horizon + 1):
                cash *= 1 + (g1 if year <= 5 else g2 if year <= 10 else terminal)
                value += cash / (1 + hurdle) ** year
            if life is None:
                value += (
                    cash * (1 + terminal) / (hurdle - terminal) / (1 + hurdle) ** 10
                )
        else:
            book = f.latest("parent_equity")
            value = book
            for year in range(1, 11):
                value += (roe - hurdle) * book / (1 + hurdle) ** year
                book *= 1 + (g1 if year <= 5 else g2)
            value += (roe - hurdle) * book / (hurdle - terminal) / (1 + hurdle) ** 10
        results.append(
            {
                **row,
                "equity_value": value,
                "price_to_value": _ratio(f.latest("market_cap"), value),
                "method": (
                    "finite_reit_affo" if life is not None else "perpetual_reit_affo"
                )
                if template == "reit"
                else "residual_income_constant_normalized_roe",
                "discount_rate": hurdle,
                "terminal_growth": terminal,
            }
        )
    return results, errors


def qualification(metrics, forecasts, breakers):
    """Qualification is code-owned, including base-only scenario exclusions."""
    passes = {
        row["name"]: row.get("price_to_value") is not None
        and 0 < row["price_to_value"] <= 1
        for row in forecasts
    }
    complete = set(passes) == {"conservative", "base", "optimistic"}
    robust = complete and all(passes.values())
    state = (
        "all_scenarios_pass"
        if robust
        else "incomplete"
        if not complete
        else "base_only"
        if passes.get("base") and not passes.get("conservative")
        else "optimistic_only"
        if passes.get("optimistic") and not passes.get("base")
        else "not_passed"
    )
    return {
        "candidate": bool(metrics.get("hurdle_pass", {}).get("value"))
        and robust
        and bool(breakers)
        and all(b["state"] == "clear" for b in breakers),
        "scenario_state": state,
        "scenario_passes": passes,
        "hurdle_state": metrics.get("hurdle_pass", {}).get("gate_state"),
    }


def source_crosschecks(packet):
    """Expose provenance dependence; mirrored vendors never become independent."""
    sources = {s["id"]: s for s in packet["sources"]}
    cells = {}
    for fact in packet["facts"]:
        key = (fact["metric"], fact["period_end"], fact["period_type"])
        row = cells.setdefault(
            key,
            {
                "metric": key[0],
                "period_end": key[1],
                "period_type": key[2],
                "fact_refs": [],
                "origin_ids": set(),
                "primary_origins": set(),
            },
        )
        row["fact_refs"].append(fact["id"])
        source = sources[fact["source_id"]]
        row["origin_ids"].add(source["origin_id"])
        if (
            source.get("kind") in {"annual", "interim"}
            and source.get("verification_level") != "provider_parsed"
        ):
            row["primary_origins"].add(source["origin_id"])
    return [
        {
            **row,
            "origin_ids": sorted(row["origin_ids"]),
            "primary_origins": sorted(row["primary_origins"]),
            "second_origin_status": "available_for_review"
            if len(row["origin_ids"]) > 1
            else "not_available",
            "limit": "Distinct origins require semantic independence review; numeric agreement alone is insufficient",
        }
        for row in cells.values()
    ]


def evaluate_breakers(
    packet: dict, breakers: list[dict]
) -> tuple[list[dict], list[dict]]:
    f, states, errors = Facts(packet["facts"]), [], []
    ops = {
        "lt": lambda a, b: a < b,
        "lte": lambda a, b: a <= b,
        "gt": lambda a, b: a > b,
        "gte": lambda a, b: a >= b,
    }
    sources = {s["id"] for s in packet["sources"]}
    for breaker in breakers:
        if (
            not breaker.get("id")
            or breaker.get("operator") not in ops
            or not finite(breaker.get("threshold"))
            or not isinstance(breaker.get("periods"), int)
            or not 1 <= breaker["periods"] <= 10
            or not breaker.get("reason")
            or not breaker.get("source_refs")
            or not set(breaker["source_refs"]).issubset(sources)
        ):
            errors.append(issue("invalid_breaker_contract", stage="calculate"))
            continue
        by_period = {r["period_end"]: r for r in f.rows(breaker["metric"], "annual")}
        rows = list(by_period.values())[-breaker["periods"] :]
        years = [int(r["period_end"][:4]) for r in rows]
        complete = len(rows) == breaker["periods"] and all(
            b - a == 1 for a, b in zip(years, years[1:])
        )
        state = (
            "unknown"
            if not complete
            else "triggered"
            if all(
                ops[breaker["operator"]](r["value"], breaker["threshold"]) for r in rows
            )
            else "clear"
        )
        states.append(
            {"id": breaker["id"], "state": state, "fact_refs": [r["id"] for r in rows]}
        )
    if not breakers:
        errors.append(issue("breakers_required", stage="calculate"))
    return states, errors
