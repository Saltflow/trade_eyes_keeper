"""Contract-isolated archive containing ranking information only."""

from __future__ import annotations

import heapq
import json
import math
from pathlib import Path

from .contracts import SearchProblem


FORBIDDEN_KEYS = {"validation", "holdout", "isolated", "embargo"}


class SearchArchive:
    def __init__(self, path: Path | str, problem: SearchProblem):
        self.path = Path(path)
        self.problem = problem

    def append(self, records: list[dict[str, object]]) -> None:
        for record in records:
            self._assert_ranking_only(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            for record in records:
                payload = {
                    "search_contract_hash": self.problem.contract_hash,
                    "data_hash": self.problem.data_hash,
                    "strategy_id": self.problem.metadata.get("strategy_id"),
                    "market": self.problem.metadata.get("market"),
                    **record,
                }
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    def top_records(self, limit: int | None = None) -> list[dict[str, object]]:
        """Stream the best feasible ranking records without loading the archive."""

        if not self.path.exists() or (limit is not None and int(limit) <= 0):
            return []
        bounded = None if limit is None else max(0, int(limit))
        heap: list[tuple[float, int, dict[str, object]]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for order, line in enumerate(handle):
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("search_contract_hash") != self.problem.contract_hash:
                    continue
                if not bool(record.get("feasible", False)):
                    continue
                score = float(record.get("selection_score", -float("inf")))
                if not math.isfinite(score):
                    continue
                item = (score, -order, record)
                if bounded is None:
                    heap.append(item)
                elif len(heap) < bounded:
                    heapq.heappush(heap, item)
                elif item[:2] > heap[0][:2]:
                    heapq.heapreplace(heap, item)
        heap.sort(key=lambda item: (-item[0], -item[1]))
        return [record for _score, _order, record in heap]

    def _assert_ranking_only(self, value: object, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower()
                if any(forbidden in lowered for forbidden in FORBIDDEN_KEYS):
                    raise ValueError(
                        "search archive cannot persist non-ranking field "
                        f"{path + str(key)!r}"
                    )
                self._assert_ranking_only(item, f"{path}{key}.")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                self._assert_ranking_only(item, f"{path}{index}.")
