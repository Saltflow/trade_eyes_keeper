"""CLI adapter for the same public API used by Feishu/Pi and Python callers."""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

from .common import atomic_write, load, now
from .engine import (
    _execute_existing,
    cancel_research,
    get_status,
    resume_research,
    start_research,
    wait_research,
)
from .models import ResearchSpec
from .observability import bounded_log_tail
from .store import Store

COMMANDS = {
    "run",
    "status",
    "logs",
    "artifacts",
    "cancel",
    "resume",
    "_worker",
    "_host",
}


def _identity(args):
    if args.source:
        record = load(args.source / "run.json")
        return record["id"], Path(record["store_root"])
    if not args.store_root:
        raise ValueError("--run-id requires --store-root")
    return args.run_id, args.store_root


def _profiles(values):
    result = {}
    for value in values:
        identity, equals, names = value.partition("=")
        if not equals or not names:
            raise ValueError("profile_requires_instrument=profile[,profile]")
        result[identity] = list(dict.fromkeys(names.split(",")))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="统一财报研究：离线计划、跨批缓存、受预算控制的全流程及状态查询"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="默认离线计划；--paid及明确预算执行全流程")
    run.add_argument("--company", action="append", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--years", type=int, default=7)
    run.add_argument("--start-year", type=int)
    run.add_argument("--end-year", type=int)
    run.add_argument("--focus", default="")
    run.add_argument("--assumptions", type=Path)
    run.add_argument("--market-snapshot", type=Path)
    run.add_argument("--source-manifest", type=Path)
    run.add_argument("--cache-root", type=Path, action="append", default=[])
    run.add_argument("--store-root", type=Path)
    run.add_argument(
        "--authority-root",
        type=Path,
        help="显式同机共享资料/请求权限域；跨项目复用时指向相同绝对目录",
    )
    run.add_argument("--profile", action="append", default=[])
    run.add_argument("--issuer-map", type=Path)
    run.add_argument("--budget-limit-usd", type=Decimal)
    run.add_argument("--repair-fraction", type=Decimal, default=Decimal(".1"))
    run.add_argument("--concurrency", type=int, default=20)
    run.add_argument("--paid", action="store_true")
    run.add_argument("--background", action="store_true")
    for name in sorted(COMMANDS - {"run"}):
        command = commands.add_parser(
            name, help=argparse.SUPPRESS if name in {"_worker", "_host"} else name
        )
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument("--source", type=Path)
        group.add_argument("--run-id")
        command.add_argument("--store-root", type=Path)
        if name == "logs":
            command.add_argument("--follow", action="store_true")
            command.add_argument("--after", type=int, default=0)
            command.add_argument("--limit", type=int, default=100)
        if name == "resume":
            command.add_argument("--paid", action="store_true", required=True)
            command.add_argument("--foreground", action="store_true")
        if name in {"_worker", "_host"}:
            command.add_argument("--resuming", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            if args.paid and args.budget_limit_usd is None:
                parser.error(
                    "--paid requires explicit --budget-limit-usd (0 is valid for cache-only)"
                )
            spec = ResearchSpec(
                companies=tuple(args.company),
                output=args.output,
                budget_limit_usd=args.budget_limit_usd or Decimal(0),
                paid=args.paid,
                years=args.years,
                start_year=args.start_year,
                end_year=args.end_year,
                focus=args.focus,
                assumptions=args.assumptions,
                market_snapshot=args.market_snapshot,
                source_manifest=args.source_manifest,
                cache_roots=tuple(args.cache_root),
                store_root=args.store_root,
                authority_root=args.authority_root,
                concurrency=args.concurrency,
                repair_fraction=args.repair_fraction,
                profiles=_profiles(args.profile),
                issuer_map=load(args.issuer_map) if args.issuer_map else {},
            )
            result = start_research(spec, background=spec.paid or args.background)
            if spec.paid and not args.background:
                print(
                    json.dumps(
                        {
                            "id": result["id"],
                            "state": result["state"],
                            "output": str(spec.output),
                            "status_entry": str(spec.output / "index.html"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                result = wait_research(
                    result["id"],
                    store_root=spec.store_root,
                    event_callback=lambda event: print(
                        json.dumps(event, ensure_ascii=False), flush=True
                    ),
                )
        else:
            run_id, root = _identity(args)
            if args.command == "status":
                result = get_status(run_id, store_root=root)
            elif args.command == "logs":
                store = Store(root)
                cursor = args.after
                while True:
                    result = bounded_log_tail(
                        store, run_id, after=cursor, limit=args.limit
                    )
                    events = result.get("events", [])
                    for event in events:
                        print(json.dumps(event, ensure_ascii=False), flush=True)
                        cursor = max(cursor, event["seq"])
                    if not args.follow or store.get_run(run_id)["state"] in {
                        "complete",
                        "partial",
                        "failed",
                        "cancelled",
                        "budget_stop",
                        "planned",
                        "awaiting_approval",
                    }:
                        return 0
                    time.sleep(0.5)
            elif args.command == "artifacts":
                result = {
                    "id": run_id,
                    "artifacts": Store(root).get_run(run_id).get("artifacts", []),
                }
            elif args.command == "cancel":
                result = cancel_research(run_id, store_root=root)
            elif args.command == "resume":
                result = resume_research(
                    run_id, store_root=root, background=not args.foreground
                )
            elif args.command == "_host":
                from .host import supervise

                return supervise(
                    run_id, root, resuming=args.resuming, source=args.source
                )
            else:
                output = Path(Store(root).get_run(run_id)["spec"]["output"])
                code = 1
                try:
                    result = _execute_existing(run_id, root, resuming=args.resuming)
                    code = 0 if result["state"] == "complete" else 2
                finally:
                    atomic_write(
                        output / "worker_exit.json", {"at": now(), "exit_code": code}
                    )
                print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
                return code
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)
        return 2 if result.get("state") in {"partial", "failed", "budget_stop"} else 0
    except (ValueError, OSError, KeyError) as error:
        print(
            json.dumps(
                {"error": str(error), "type": type(error).__name__}, ensure_ascii=False
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2
