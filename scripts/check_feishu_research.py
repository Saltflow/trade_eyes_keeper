"""Inspect research readiness; optionally run an explicit cache-only smoke job."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config_store import ConfigStore
from src.interactive.assistant.research import ResearchRunner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--image")
    parser.add_argument("--cpus", type=float)
    parser.add_argument("--memory-mb", type=int)
    parser.add_argument(
        "--project-only",
        action="store_true",
        help="Only inspect existing project API contracts",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run explicit research using only existing cached real data; never fetch or notify",
    )
    parser.add_argument(
        "--script-id",
        choices=("current_strategy", "justified_pb_value", "capm_dcf_value"),
        default="current_strategy",
    )
    parser.add_argument("--codes", nargs="+")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    raw = ConfigStore(args.project_root / "config/config.yaml").load_raw()
    settings = dict(raw.get("interactive", {}).get("feishu", {}).get("assistant", {}))
    for field, value in (
        ("docker_image", args.image),
        ("cpus", args.cpus),
        ("memory_mb", args.memory_mb),
    ):
        if value is not None:
            settings[field] = value
    runner = ResearchRunner(args.project_root, settings)
    result = (
        runner.project_compatibility(args.project_root)
        if args.project_only
        else runner.preflight()
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.smoke and result["ready"]:
        if args.project_only or not args.codes or not args.start or not args.end:
            parser.error(
                "--smoke requires --codes, --start, --end and Docker preflight"
            )
        payload = runner.prepare(
            {
                "script_id": args.script_id,
                "codes": args.codes,
                "start": args.start,
                "end": args.end,
            },
            "smoke-" + uuid.uuid4().hex[:12],
        )
        print(json.dumps(payload["preview"], ensure_ascii=False, indent=2))
        result = runner.run(payload, threading.Event(), print, cached_only=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 1
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
