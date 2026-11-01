"""Read frozen disclosed-EPS and official fund-reference supplements."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .historical_fund_ke import FundKeHistory, FundKeObservation
from .historical_ke_filings import EpsFact, earnings_as_of


class KeHistoryStore:
    def __init__(self, root: Path | str):
        directory = Path(root) / "ke_history"
        self.facts: dict[str, list[EpsFact]] = {}
        self.funds: dict[str, FundKeHistory] = {}
        self.max_ages: dict[str, int] = {}
        path = directory / "eps_facts.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for row in payload["facts"]:
                fact = EpsFact.from_dict(row)
                self.facts.setdefault(fact.symbol, []).append(fact)
        path = directory / "fund_histories.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.max_ages = payload.get("max_age_days", {})
            for code, row in payload["histories"].items():
                row = dict(row)
                observations = []
                for item in row.pop("observations", []):
                    item = dict(item)
                    for field in ("valuation_date", "available_from"):
                        item[field] = date.fromisoformat(item[field])
                    observations.append(FundKeObservation(**item))
                self.funds[code] = FundKeHistory(
                    observations=tuple(observations), **row
                )
                if self.funds[code].fund_code != code:
                    raise ValueError("fund history security identity mismatch")

    def earnings(self, code: str, day: date) -> dict | None:
        known = [f for f in self.facts.get(code, []) if f.available_from <= day]
        return earnings_as_of(known, day, symbol=code) if known else None

    def fund(self, code: str, day: date) -> dict | None:
        history = self.funds.get(code)
        if history is None:
            return None
        # A daily published index close is observable at that day's close.
        # Only yesterday's available PE/EPS anchor may be used. Monthly PDF
        # closes have a later publication clock and cannot supply this price.
        current = next(
            (
                row.reference_price
                for row in history.observations
                if row.valuation_date == day
                and row.availability_basis == "index_eod_next_calendar_day_policy"
            ),
            None,
        )
        return history.at(
            day,
            max_age_days=int(self.max_ages.get(code, 14)),
            reference_price=current,
            reference_price_as_of=day if current is not None else None,
        ).as_dict()

    @property
    def source_contracts(self) -> dict:
        return {
            code: {
                "valuation_basis": row.valuation_basis,
                "vintage_basis": row.vintage_basis,
                "qualified_for_promotion": row.qualified_for_promotion,
            }
            for code, row in self.funds.items()
        }
