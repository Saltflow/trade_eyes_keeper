#!/usr/bin/env python3
"""Build the complete offline business-data inventory; no network access."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.dataset_catalog import DatasetCatalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    summary = DatasetCatalog(args.project_root).build()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["index_failures"] or summary["manifest_gaps"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
