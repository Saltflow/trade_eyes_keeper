#!/usr/bin/env python3
"""Inspect local access controls; optionally verify release on the official site."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.baostock_access import BaostockAccessController


def refresh_official(controller: BaostockAccessController) -> dict:
    """No SDK login: the public website reports status for the current egress."""
    checked_at = time.time()
    with requests.Session() as session:
        # The Baostock SDK uses a direct IPv4 TCP socket, not HTTP proxies.
        session.trust_env = False
        response = session.get("https://api.ipify.org?format=json", timeout=20)
        response.raise_for_status()
        address = str(ipaddress.IPv4Address(response.json()["ip"]))
        response = session.post(
            "https://www.baostock.com/helpdocs/api/wd-blacklist-stats",
            json={"ip": address},
            timeout=20,
        )
        response.raise_for_status()
        return controller.apply_official_status(response.json(), checked_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config/config.yaml"))
    parser.add_argument(
        "--refresh-official",
        action="store_true",
        help="Read current-egress official status; clear only a confirmed released ban",
    )
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    controller = BaostockAccessController(config)
    status = (
        refresh_official(controller) if args.refresh_official else controller.status()
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 2 if status.get("blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
