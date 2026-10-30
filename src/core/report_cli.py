"""Lightweight public report submission, before importing the analysis stack."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .config_store import DEFAULT_CONFIG_PATH, ConfigStore
from .report_execution import submit_report


def main(argv):
    parser = argparse.ArgumentParser(
        description="Submit a report to the shared executor"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--once", action="store_true")
    modes.add_argument("--brief", nargs="?", const="morning_snapshot")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--group", choices=("a_share", "hk", "us"))
    args = parser.parse_args(argv)
    config = ConfigStore(args.config).load_runtime()
    row = submit_report(
        config,
        kind="daily" if args.once else "brief",
        report_id=args.brief or "morning_snapshot",
        force=os.getenv("BOT_FORCE") == "1",
        source="cli",
        wait=True,
        config_path=args.config,
    )
    print(
        json.dumps(
            {
                "run_id": row["id"],
                "status": row["status"],
                "reason": row.get("reason", ""),
            },
            ensure_ascii=False,
        )
    )
    return 0 if row["status"] in {"succeeded", "skipped"} else 1
