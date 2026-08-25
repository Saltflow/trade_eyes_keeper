#!/usr/bin/env python3
"""Merge publication-dated official industry histories without deduping facts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("inputs", nargs="+", help="history JSON files")
    args = parser.parse_args()

    observations = []
    documents = []
    source_kinds = []
    for raw_path in args.inputs:
        path = Path(raw_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("contract") != "industry-classification-history-1":
            raise ValueError(f"unsupported history contract: {path}")
        observations.extend(payload.get("observations", []))
        documents.extend(payload.get("documents", []))
        source_kinds.append(str(payload.get("source_kind") or "unknown"))

    keys = [
        (
            item["symbol"],
            item["period_end"],
            item["published_at"],
            item["source_url"],
        )
        for item in observations
    ]
    if len(keys) != len(set(keys)):
        raise ValueError("merged industry histories contain duplicate observations")
    observations.sort(key=lambda item: (
        item["published_at"], item["period_end"], item["symbol"]
    ))
    documents.sort(key=lambda item: (item["published_at"], item["period_end"]))
    result = {
        "contract": "industry-classification-history-1",
        "taxonomy": "mixed-official-csrc-capco",
        "source_kind": "+".join(sorted(set(source_kinds))),
        "publication_rule": (
            "a classification becomes usable only at its source published_at; "
            "period_end is not a feature availability date"
        ),
        "documents": documents,
        "observation_count": len(observations),
        "observations": observations,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(output)
    print(json.dumps({
        "output": str(output.resolve()),
        "observation_count": len(observations),
        "document_count": len(documents),
        "source_kinds": sorted(set(source_kinds)),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
