#!/usr/bin/env python3
"""Import official CSRC quarterly industry PDFs into a dated history store."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fundamental_embedding.csrc_industry import (  # noqa: E402
    extract_pdf_text,
    parse_csrc_classification_text,
)
from src.fundamental_embedding.industry_history import (  # noqa: E402
    INDUSTRY_CLASSIFICATION_HISTORY_CONTRACT,
)


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    documents = payload.get("documents", [])
    if not isinstance(documents, list) or not documents:
        raise ValueError("CSRC source manifest must contain documents")
    required = {"period_end", "published_at", "source_url"}
    for item in documents:
        missing = required - set(item)
        if missing:
            raise ValueError(f"CSRC source document missing: {sorted(missing)}")
    return documents


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument("--min-interval-seconds", type=float, default=1.0)
    args = parser.parse_args()

    documents = _read_manifest(Path(args.source_manifest))
    session = requests.Session()
    session.headers.update({
        "User-Agent": "trade-eyes-keeper research (official CSRC taxonomy import)"
    })
    observations = []
    audit = []
    for index, document in enumerate(documents):
        if index:
            time.sleep(max(0.0, args.min_interval_seconds))
        response = session.get(
            str(document["source_url"]), timeout=max(1, args.timeout_seconds)
        )
        response.raise_for_status()
        content = response.content
        rows = parse_csrc_classification_text(
            extract_pdf_text(content),
            period_end=date.fromisoformat(str(document["period_end"])),
            published_at=date.fromisoformat(str(document["published_at"])),
            source_url=response.url,
            source_content=content,
        )
        observations.extend(rows)
        audit.append({
            "period_end": str(document["period_end"]),
            "published_at": str(document["published_at"]),
            "source_url": response.url,
            "parsed_symbol_count": len(rows),
        })
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "contract": INDUSTRY_CLASSIFICATION_HISTORY_CONTRACT,
        "taxonomy": "csrc-2012",
        "source_kind": "official_csrc_quarterly_publications",
        "publication_rule": (
            "a classification becomes usable only at its source published_at; "
            "period_end is not a feature availability date"
        ),
        "documents": audit,
        "observation_count": len(observations),
        "observations": [item.to_dict() for item in observations],
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(output)
    print(json.dumps({
        "output": str(output.resolve()),
        "document_count": len(audit),
        "observation_count": len(observations),
        "documents": audit,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
