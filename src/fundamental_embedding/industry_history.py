"""Publication-dated industry classification history for walk-forward research.

Unlike a current taxonomy snapshot, this store keeps every observed
classification publication.  A historical feature date may only use a row
whose *publication date*, rather than its reporting-period end, is already in
the past.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from .industry import IndustryClassification


INDUSTRY_CLASSIFICATION_HISTORY_CONTRACT = "industry-classification-history-1"


@dataclass(frozen=True)
class IndustryClassificationObservation:
    symbol: str
    industry_code: str
    industry_name: str
    taxonomy: str
    period_end: date
    published_at: date
    source_url: str
    source_sha256: str | None = None

    def validate(self) -> "IndustryClassificationObservation":
        if not self.symbol or not self.industry_code or not self.industry_name:
            raise ValueError("industry observation needs symbol, code and name")
        if self.published_at < self.period_end:
            raise ValueError("industry observation cannot publish before its period end")
        if not self.source_url:
            raise ValueError("industry observation needs a source URL")
        return self

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["period_end"] = self.period_end.isoformat()
        payload["published_at"] = self.published_at.isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "IndustryClassificationObservation":
        return cls(
            symbol=str(payload["symbol"]),
            industry_code=str(payload["industry_code"]),
            industry_name=str(payload["industry_name"]),
            taxonomy=str(payload.get("taxonomy") or "unspecified"),
            period_end=date.fromisoformat(str(payload["period_end"])),
            published_at=date.fromisoformat(str(payload["published_at"])),
            source_url=str(payload["source_url"]),
            source_sha256=(
                str(payload["source_sha256"])
                if payload.get("source_sha256")
                else None
            ),
        ).validate()


class IndustryClassificationHistoryStore:
    """Read a history and select only the classification known on each date."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> list[IndustryClassificationObservation]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("contract") != INDUSTRY_CLASSIFICATION_HISTORY_CONTRACT:
            raise ValueError("unsupported industry classification history contract")
        observations = [
            IndustryClassificationObservation.from_dict(item)
            for item in payload.get("observations", [])
        ]
        keys = [
            (item.symbol, item.period_end, item.published_at, item.source_url)
            for item in observations
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("industry history contains duplicate observations")
        return sorted(observations, key=lambda item: (
            item.published_at, item.period_end, item.symbol
        ))

    def labels_as_of(
        self,
        evaluation_date: date,
        symbols: Iterable[str] | None = None,
    ) -> dict[str, IndustryClassification]:
        requested = {str(item) for item in symbols or ()}
        chosen: dict[str, IndustryClassificationObservation] = {}
        for item in self.read():
            if item.published_at > evaluation_date:
                continue
            if requested and item.symbol not in requested:
                continue
            previous = chosen.get(item.symbol)
            if previous is None or (item.published_at, item.period_end) > (
                previous.published_at,
                previous.period_end,
            ):
                chosen[item.symbol] = item
        return {
            symbol: IndustryClassification(
                symbol=item.symbol,
                industry_code=item.industry_code,
                industry_name=item.industry_name,
                taxonomy=item.taxonomy,
                effective_from=item.published_at,
                source=item.source_url,
            )
            for symbol, item in chosen.items()
        }

    def coverage_as_of(
        self,
        evaluation_date: date,
        symbols: Iterable[str],
    ) -> dict[str, object]:
        requested = tuple(str(item) for item in symbols)
        labels = self.labels_as_of(evaluation_date, requested)
        return {
            "evaluation_date": evaluation_date.isoformat(),
            "requested_symbol_count": len(requested),
            "classified_symbol_count": len(labels),
            "missing_symbols": sorted(set(requested) - set(labels)),
            "historical_walk_forward_eligible": len(labels) == len(requested),
        }
