"""Deterministic ranges from reviewed assumptions, never inserted as facts."""

from decimal import Decimal
from statistics import median

from .calculations import Facts, cagr
from .contracts import finite, issue

VERSION = "model-led-calculations/2"
ESTIMABLE = {
    "normalized_earnings",
    "normalized_fcf",
    "normalized_affo",
    "normalized_roe",
    "normalized_roic",
}


def calculate_research(packet, estimates, *, growth=0.02, hurdle=0.07):
    facts = Facts(packet["facts"])
    metrics, errors = {}, []
    currency = {
        f["currency"]
        for f in packet["facts"]
        if f.get("unit") in {"currency", "per_share"}
    }
    for e in estimates:
        if e["metric"] not in ESTIMABLE:
            errors.append(issue("unsupported_estimate_metric", e["metric"]))
            continue
        ratio = e["metric"] in {"normalized_roe", "normalized_roic"}
        if e["unit"] != ("ratio" if ratio else "currency") or (
            not ratio and currency != {e["currency"]}
        ):
            errors.append(issue("estimate_unit_or_currency_mismatch", e["id"]))
            continue
        if e["metric"] in metrics:
            errors.append(issue("duplicate_estimate_metric", e["metric"]))
            continue
        metrics[e["metric"]] = {
            "value": e["base"],
            "range": {k: e[k] for k in ("low", "base", "high")},
            "fact_refs": e["fact_refs"],
            "estimate_refs": [e["id"]],
            "source_refs": e["source_refs"],
            "formula": "explicit assumption range",
            "status": "estimated",
            "unit": e["unit"],
            "currency": e["currency"],
        }

    def scalar(name, value, refs, formula):
        if value is not None and finite(value):
            metrics[name] = {
                "value": value,
                "range": dict.fromkeys(("low", "base", "high"), value),
                "fact_refs": refs,
                "estimate_refs": [],
                "formula": formula,
                "status": "derived",
            }

    def derive(name, parent, divisor, formula, offset=0, refs=()):
        if parent not in metrics or divisor is None or divisor <= 0:
            return
        source = metrics[parent]
        values = {k: offset + v / divisor for k, v in source["range"].items()}
        metrics[name] = {
            "value": values["base"],
            "range": values,
            "fact_refs": list(dict.fromkeys(source["fact_refs"] + list(refs))),
            "estimate_refs": source["estimate_refs"],
            "formula": formula,
            "status": "derived",
        }

    def latest(name):
        rows = facts.rows(name)
        return (rows[-1]["value"], [rows[-1]["id"]]) if rows else (None, [])

    # Raw observations remain visible, including actual history length.
    for name in (
        "revenue",
        "adjusted_net_income_parent",
        "operating_cash_flow",
        "capital_expenditures",
        "parent_equity",
        "affo",
    ):
        rows = facts.annual_rows(name)[0]
        if rows:
            years = {int(f["period_end"][:4]): f["value"] for f in rows}
            metrics[name + "_history"] = {
                "observations": years,
                "fact_refs": [f["id"] for f in rows],
                "cagr": cagr(years),
                "status": "observed",
                "covered_years": sorted(years),
            }
    earnings = facts.annual("adjusted_net_income_parent")
    if earnings and "normalized_earnings" not in metrics:
        end = max(earnings)
        years = [y for y in sorted(earnings) if end - 4 <= y <= end]
        # This is an observable baseline, not an automatic normalization claim.
        scalar(
            "observed_earnings_median",
            median(earnings[y] for y in years),
            [
                r["id"]
                for r in facts.annual_rows("adjusted_net_income_parent")[0]
                if int(r["period_end"][:4]) in years
            ],
            "median(available annual earnings)",
        )
    cfo, capex = (
        facts.annual("operating_cash_flow"),
        facts.annual("capital_expenditures"),
    )
    metrics["raw_fcf_history"] = {
        "observations": {
            y: cfo[y] - capex[y] for y in sorted(cfo.keys() & capex.keys())
        },
        "formula": "CFO-Capex",
        "fact_refs": [
            r["id"]
            for name in ("operating_cash_flow", "capital_expenditures")
            for r in facts.annual_rows(name)[0]
        ],
        "status": "derived",
        "shareholder_cashflow": False,
    }
    market, market_refs = latest("market_cap")
    equity, equity_refs = latest("parent_equity")
    scalar("current_market_cap", market, market_refs, "reported market capitalization")
    scalar("current_parent_equity", equity, equity_refs, "reported parent equity")
    if market and market > 0:
        derive(
            "fcf_yield",
            "normalized_fcf",
            market,
            "normalized shareholder FCF / market cap",
            refs=market_refs,
        )
        derive(
            "affo_yield",
            "normalized_affo",
            market,
            "normalized AFFO / market cap",
            refs=market_refs,
        )
    template = packet["company"]["template"]
    proposed_roe = metrics.pop("normalized_roe", None)
    if proposed_roe:
        proposed_roe.update(diagnostic_only=True, valuation_input=False)
        metrics["proposed_normalized_roe"] = proposed_roe
    if template != "reit" and equity and equity > 0:
        derive(
            "normalized_roe",
            "normalized_earnings",
            equity,
            "normalized parent earnings / current parent equity",
            refs=equity_refs,
        )
        if proposed_roe:
            # A model's historical/rounded ROE is not a second valuation input.
            # Preserve it for review, but never use it to override code's ROE or
            # prevent unrelated earnings/cashflow research from proceeding.
            proposed = next(e for e in estimates if e["metric"] == "normalized_roe")
            earnings_estimate = next(
                (e for e in estimates if e["metric"] == "normalized_earnings"), None
            )
            averages = [
                f
                for f in packet["facts"]
                if f["metric"] == "average_parent_equity"
                and f["id"] in proposed["fact_refs"]
                and f["period_end"] == proposed["period_end"]
                and earnings_estimate
                and f["period_end"] == earnings_estimate["period_end"]
                and finite(f["value"])
                and f["value"] > 0
            ]
            if averages and len({f["value"] for f in averages}) == 1:
                derive(
                    "historical_normalized_roe",
                    "normalized_earnings",
                    averages[0]["value"],
                    "normalized parent earnings / cited same-period average parent equity",
                    refs=[f["id"] for f in averages],
                )

            def displayed_rounding_matches(metric):
                if not metric:
                    return False
                for k in ("low", "base", "high"):
                    displayed = Decimal(str(proposed_roe["range"][k]))
                    quantum = Decimal(1).scaleb(displayed.as_tuple().exponent)
                    expected = Decimal(str(metric["range"][k]))
                    if abs(displayed - expected) > abs(quantum) / 2:
                        return False
                return True

            explained_by = next(
                (
                    name
                    for name in ("historical_normalized_roe", "normalized_roe")
                    if displayed_rounding_matches(metrics.get(name))
                ),
                None,
            )
            metrics["roe_basis_reconciliation"] = {
                "status": "explained_basis_or_rounding"
                if explained_by
                else "unresolved_diagnostic",
                "explained_by": explained_by,
                "valuation_metric": "normalized_roe",
                "proposed_metric": "proposed_normalized_roe",
                "diagnostic_only": True,
                "blocks_valuation": False,
                "rounding_rule": "half the last displayed decimal place for each model endpoint",
                "reason": "Valuation uses code-derived current-equity ROE. The separate model proposal is not publishable as a verified metric.",
            }
        source = metrics.get("normalized_earnings")
        if source and market and market > 0:
            values = {
                k: growth + (v - growth * equity) / market
                for k, v in source["range"].items()
            }
            for name in ("implied_ke_pb", "implied_ke_pe"):
                if name == "implied_ke_pe" and source["range"]["low"] <= 0:
                    continue  # PE residual-income form is not meaningful here.
                metrics[name] = {
                    "value": values["base"],
                    "range": dict(values),
                    "fact_refs": list(
                        dict.fromkeys(source["fact_refs"] + equity_refs + market_refs)
                    ),
                    "estimate_refs": source["estimate_refs"],
                    "status": "derived",
                    "formula": "g+(normalized earnings-g*current equity)/market cap",
                    "interpretation": "constant normalized earnings/ROE residual-income proxy, not total-return forecast",
                }
    metrics["reference_conditions"] = {
        "hurdle": hurdle,
        "terminal_growth": growth,
        "binding": False,
        "status": "reference_only",
    }
    return metrics, errors
