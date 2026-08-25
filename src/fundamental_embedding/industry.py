"""Dated industry labels and current industry-relative fundamental features.

The free Baostock endpoint provides a current classification snapshot, not a
historical taxonomy tape. Labels therefore may only be applied after their
source-provided effective date. Historical walk-forward feature construction
must wait for a dated classification history.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from scipy.stats import rankdata

from .api import FundamentalPricingSnapshot


INDUSTRY_CLASSIFICATION_CONTRACT = "industry-classification-snapshot-1"
_INDUSTRY_CODE = re.compile(r"^([A-Z])(\d{2})")


@dataclass(frozen=True)
class IndustryClassification:
    """A label that is valid from ``effective_from`` onward."""

    symbol: str
    industry_code: str | None
    industry_name: str | None
    taxonomy: str
    effective_from: date
    source: str

    @property
    def sector_code(self) -> str | None:
        match = _INDUSTRY_CODE.match(self.industry_code or "")
        return match.group(1) if match else None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IndustryClassification":
        return cls(
            symbol=str(payload["symbol"]),
            industry_code=(
                str(payload["industry_code"])
                if payload.get("industry_code")
                else None
            ),
            industry_name=(
                str(payload["industry_name"])
                if payload.get("industry_name")
                else None
            ),
            taxonomy=str(payload.get("taxonomy") or "unspecified"),
            effective_from=date.fromisoformat(str(payload["effective_from"])),
            source=str(payload.get("source") or "unspecified"),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["effective_from"] = self.effective_from.isoformat()
        return result


class IndustryClassificationStore:
    """Read a strict dated classification snapshot without backfilling."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> list[IndustryClassification]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("contract") != INDUSTRY_CLASSIFICATION_CONTRACT:
            raise ValueError("unsupported industry classification contract")
        labels = [
            IndustryClassification.from_dict(item)
            for item in payload.get("classifications", [])
        ]
        if len({item.symbol for item in labels}) != len(labels):
            raise ValueError("industry snapshot contains duplicate symbols")
        return labels

    def labels_as_of(
        self,
        evaluation_date: date,
        symbols: Iterable[str] | None = None,
    ) -> dict[str, IndustryClassification]:
        requested = {str(item) for item in symbols or ()}
        return {
            item.symbol: item
            for item in self.read()
            if item.effective_from <= evaluation_date
            and (not requested or item.symbol in requested)
        }


@dataclass(frozen=True)
class IndustryRelativeSnapshot:
    """One current, industry-relative counterpart of a pricing snapshot."""

    feature_date: date
    symbols: tuple[str, ...]
    feature_names: tuple[str, ...]
    values: np.ndarray
    availability_mask: np.ndarray
    industry_codes: tuple[str | None, ...]
    peer_scopes: tuple[str | None, ...]
    peer_counts: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> "IndustryRelativeSnapshot":
        expected = (len(self.symbols), len(self.feature_names))
        if (
            self.values.shape != expected
            or self.availability_mask.shape != expected
            or self.peer_counts.shape != (len(self.symbols),)
            or len(self.industry_codes) != len(self.symbols)
            or len(self.peer_scopes) != len(self.symbols)
        ):
            raise ValueError("industry-relative snapshot is misaligned")
        if self.availability_mask.dtype != bool:
            raise ValueError("industry-relative availability must be boolean")
        if not np.all(np.isfinite(self.values[self.availability_mask])):
            raise ValueError("available industry-relative values must be finite")
        if np.any(self.peer_counts < 0):
            raise ValueError("industry peer counts must be nonnegative")
        return self


class IndustryRelativeSnapshotBuilder:
    """Rank each observed feature against contemporaneously known peers."""

    def __init__(self, *, minimum_industry_peers: int = 5):
        if minimum_industry_peers < 2:
            raise ValueError("minimum_industry_peers must be at least two")
        self.minimum_industry_peers = int(minimum_industry_peers)

    @staticmethod
    def _centered_rank(values: np.ndarray) -> np.ndarray:
        count = len(values)
        if count < 2:
            return np.zeros(count, dtype=np.float64)
        ranks = rankdata(values, method="average")
        return (ranks - (count + 1.0) / 2.0) / ((count - 1.0) / 2.0)

    def _resolve_groups(
        self,
        labels: list[IndustryClassification | None],
    ) -> tuple[
        list[str | None],
        list[str | None],
        np.ndarray,
        dict[tuple[str, str], np.ndarray],
    ]:
        fine: dict[str, list[int]] = defaultdict(list)
        sectors: dict[str, list[int]] = defaultdict(list)
        for index, label in enumerate(labels):
            if label is None or label.industry_code is None:
                continue
            fine[label.industry_code].append(index)
            if label.sector_code is not None:
                sectors[label.sector_code].append(index)

        keys: list[str | None] = [None] * len(labels)
        scopes: list[str | None] = [None] * len(labels)
        counts = np.zeros(len(labels), dtype=np.int64)
        recipient_rows: dict[tuple[str, str], list[int]] = defaultdict(list)
        peer_rows: dict[tuple[str, str], np.ndarray] = {}
        for index, label in enumerate(labels):
            if label is None or label.industry_code is None:
                continue
            if len(fine[label.industry_code]) >= self.minimum_industry_peers:
                scope, key = "industry", label.industry_code
                peers = fine[key]
            elif (
                label.sector_code is not None
                and len(sectors[label.sector_code]) >= self.minimum_industry_peers
            ):
                scope, key = "sector_fallback", label.sector_code
                peers = sectors[key]
            else:
                continue
            keys[index] = key
            scopes[index] = scope
            counts[index] = len(peers)
            group = (scope, key)
            recipient_rows[group].append(index)
            peer_rows[group] = np.asarray(peers, dtype=int)
        return (
            keys,
            scopes,
            counts,
            {
                group: np.asarray(rows, dtype=int)
                for group, rows in recipient_rows.items()
            },
            peer_rows,
        )

    def build(
        self,
        snapshot: FundamentalPricingSnapshot,
        labels: dict[str, IndustryClassification],
    ) -> IndustryRelativeSnapshot:
        snapshot.validate()
        selected_labels = [labels.get(symbol) for symbol in snapshot.symbols]
        keys, scopes, counts, recipient_rows, peer_rows = self._resolve_groups(
            selected_labels
        )
        output = np.zeros_like(snapshot.values, dtype=np.float64)
        available = np.zeros_like(snapshot.availability_mask, dtype=bool)
        for group, recipients in recipient_rows.items():
            peers = peer_rows[group]
            for feature_index in range(snapshot.values.shape[1]):
                observed = peers[
                    snapshot.availability_mask[peers, feature_index]
                    & np.isfinite(snapshot.values[peers, feature_index])
                ]
                if len(observed) < 2:
                    continue
                ranked = self._centered_rank(
                    snapshot.values[observed, feature_index]
                )
                rank_by_row = dict(zip(observed.tolist(), ranked.tolist()))
                for row in recipients:
                    if row in rank_by_row:
                        output[row, feature_index] = rank_by_row[row]
                        available[row, feature_index] = True
        return IndustryRelativeSnapshot(
            feature_date=snapshot.feature_date,
            symbols=snapshot.symbols,
            feature_names=tuple(
                f"industry_relative:{name}" for name in snapshot.feature_names
            ),
            values=output,
            availability_mask=available,
            industry_codes=tuple(
                item.industry_code if item is not None else None
                for item in selected_labels
            ),
            peer_scopes=tuple(scopes),
            peer_counts=counts,
            metadata={
                "contract": "industry-relative-fundamental-snapshot-1",
                "classification_semantics": (
                    "label effective_from must not be after feature_date"
                ),
                "minimum_industry_peers": self.minimum_industry_peers,
                "historical_walk_forward_eligible": False,
            },
        ).validate()
