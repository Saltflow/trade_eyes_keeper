"""One research entry, one final report; preparing is offline by default."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import research_inputs as inputs
from scripts import research_rounds as rounds
from scripts import run_company_summary as io


def execute(folder, post=None, key=None):
    folder = folder.resolve()
    if io.sha(folder / "freeze.json") != (folder / "freeze.sha256").read_text():
        raise ValueError("freeze_manifest_changed")
    io.verify(io.load(folder / "freeze.json")["files"])
    plan = io.load(folder / "plan.json")
    maximum = Decimal(plan["authorized_usd"]) if plan["authorized_usd"] else Decimal(0)
    if not maximum.is_finite() or maximum < Decimal(plan["reserved_usd_peak"]):
        raise ValueError("explicit_budget_required_or_insufficient")
    if plan["maximum_calls"] != 3 or [j["id"] for j in plan["jobs"]] != [
        "draft",
        "review",
        "write",
    ]:
        raise ValueError("three_frozen_rounds_required")
    if datetime.now(timezone.utc).date().isoformat() not in plan["pricing_utc_dates"]:
        raise ValueError("outside_frozen_pricing_dates")
    if key is None:
        from dotenv import dotenv_values

        key = os.getenv("DEEPSEEK_API_KEY") or dotenv_values(
            inputs.REPO / "config/.env"
        ).get("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("missing_api_key")
    io.save(folder / "execution_started.json", {"at": io.now(), "maximum_calls": 3})
    receipts, spent, known, last, status = [], Decimal(0), Decimal(0), -1, "complete"
    for index, job in enumerate(plan["jobs"]):
        io.verify(io.load(folder / "freeze.json")["files"])
        if spent + Decimal(job["reserved_usd_peak"]) > maximum:
            status = "budget_exhausted"
            break
        try:
            request, frozen = rounds.request(folder, plan, index)
        except ValueError as error:
            status = str(error)
            break
        receipt = io.perform(folder, job, request, plan, key, post=post)
        receipts.append(receipt)
        if (folder / "jobs" / job["id"] / "content.md").stat().st_size:
            last = index
        actual = receipt["cost"]["usd"]
        known += Decimal(actual) if actual is not None else Decimal(0)
        spent += (
            Decimal(actual) if actual is not None else Decimal(job["reserved_usd_peak"])
        )
        io.verify(frozen["files"])
        if io.sha(folder / "jobs" / job["id"] / "request.json") != io.sha(
            folder / "requests" / f"{job['id']}.json"
        ):
            raise ValueError("sent_request_changed")
        if index < 2:
            rounds.present_round(folder, job, receipt)
        if receipt["status"] != "complete":
            status = receipt["status"]
            break
    notes = rounds.final(folder, plan, last, status)
    result = {
        "status": status,
        "receipts": receipts,
        "receipt": receipts[-1] if receipts else None,
        "requests_sent": len(receipts),
        "automatic_retries": 0,
        "simulated": post is not None,
        "known_usage_usd": str(known),
        "cost_usd": str(known)
        if all(r["cost"]["usd"] is not None for r in receipts)
        else None,
        "budget_consumed_or_reserved_usd": str(spent),
        "authorized_usd": plan["authorized_usd"],
        "presentation": notes,
        "delivered_round": plan["jobs"][last]["id"] if last >= 0 else None,
        "request_bytes_match": True,
        "raw_content_preserved": True,
    }
    io.verify(io.load(folder / "freeze.json")["files"])
    io.save(folder / "执行结果.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--source",
        nargs="+",
        type=Path,
        help="Existing draft/materials; default: prepare only",
    )
    mode.add_argument(
        "--execute", type=Path, help="Execute a frozen three-round run with --paid"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--focus", default="")
    parser.add_argument(
        "--companies",
        nargs="*",
        default=[],
        help="Declared company codes for local prices; otherwise use stage3 manifest",
    )
    parser.add_argument("--feedback", nargs="*", type=Path, default=[])
    parser.add_argument(
        "--market-snapshot",
        type=Path,
        help="Read an explicit existing snapshot; never fetch quotes",
    )
    parser.add_argument("--budget-limit-usd", type=Decimal)
    parser.add_argument("--paid", action="store_true")
    args = parser.parse_args(argv)
    if args.execute:
        if (
            not args.paid
            or args.output
            or args.config
            or args.focus
            or args.companies
            or args.feedback
            or args.market_snapshot
            or args.budget_limit_usd is not None
        ):
            parser.error("execute requires --paid and accepts no preparation overrides")
        result = execute(args.execute)
    else:
        if args.paid or not args.output:
            parser.error(
                "prepare requires --output and cannot send requests; --paid belongs to --execute"
            )
        result = inputs.prepare(
            args.source + ([args.market_snapshot] if args.market_snapshot else []),
            args.output,
            args.config or inputs.REPO / "config/research_report.json",
            args.feedback,
            args.focus,
            args.budget_limit_usd,
            companies=args.companies,
            local_prices=not args.market_snapshot,
        )
        result = {
            k: result[k]
            for k in (
                "input_tokens_estimate",
                "input_waterline",
                "authorized_usd",
                "reserved_usd_peak",
                "maximum_calls",
                "estimated_uncached_cost_usd",
            )
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not args.execute or result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
