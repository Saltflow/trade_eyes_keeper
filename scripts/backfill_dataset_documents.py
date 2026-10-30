#!/usr/bin/env python3
"""Plan or explicitly execute a bounded catalog document acquisition batch."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Download only catalog-registered public source documents",
    )
    parser.add_argument("--max-documents", type=int, default=20)
    parser.add_argument("--max-seconds", type=float, default=300)
    parser.add_argument("--min-interval", type=float, default=3)
    args = parser.parse_args(argv)
    from src.data.dataset_catalog import DatasetCatalog
    from src.data.dataset_documents import DatasetDocumentWorker

    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    if not (PROJECT_ROOT / "data/dataset_catalog/catalog.sqlite3").is_file():
        parser.error(
            "Build the dataset catalog first; this command does not discover arbitrary URLs"
        )
    catalog = DatasetCatalog(PROJECT_ROOT)
    worker = DatasetDocumentWorker(catalog, stop=stop, min_interval=args.min_interval)
    if args.execute:
        result = worker.run(
            max_documents=args.max_documents, max_seconds=args.max_seconds
        )
    else:
        result = worker.plan(max_documents=args.max_documents)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return (
        1
        if args.execute
        and any(item["status"] == "failed" for item in result["processed"])
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
