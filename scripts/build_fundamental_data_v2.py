"""Fixed-plan extraction, mechanical history and strictly offline replay."""

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.fundamental_data.v2_history import assemble
from src.fundamental_data.v2_schema import (
    combine_blocks,
    parse,
    peer_input,
    validate,
)
from src.fundamental_data.v2_transport import (
    call,
    event,
    implementation_files,
    payload,
    prepare_replay,
    prepare_request,
    read_pdf,
    save,
)
from src.fundamental_data.v2_view import publish


def planned_jobs(config, profiles):
    names = list(config["base_blocks"])
    for profile in profiles:
        for name in config["industry_profiles"][profile]["blocks"]:
            if name not in names:
                names.append(name)
    return names


def budget(config, blocks):
    inputs = sum(block["input_tokens_estimate"] for block in blocks.values())
    outputs = sum(block["max_output_tokens"] for block in blocks.values())
    pricing = config["pricing"]
    estimated = (
        Decimal(inputs) * Decimal(str(pricing["input_cache_miss_per_million"]))
        + Decimal(outputs) * Decimal(str(pricing["output_per_million"]))
    ) / Decimal(1000000)
    return {
        "input_tokens_estimate": inputs,
        "max_output_tokens": outputs,
        "calls": len(blocks),
        "currency": pricing["currency"],
        "peak_uncached_cost_estimate": float(estimated.quantize(Decimal("0.000001"))),
        "pricing": pricing,
        "is_provider_billing_guarantee": False,
        "cache_assumption": "all_input_uncached",
    }


def freeze_run(repo, output, sources, jobs, names, report_id, max_calls=None):
    hashes = {
        str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [*implementation_files(repo), *sources]
    }
    if (output / "call_plan.json").is_file():
        hashes[str((output / "call_plan.json").resolve())] = hashlib.sha256(
            (output / "call_plan.json").read_bytes()
        ).hexdigest()
    for _, folder, _ in jobs:
        hashes.update(parse((folder / "freeze.json").read_text("utf8"))["files"])
        request = folder / "request.json"
        hashes[str(request.resolve())] = hashlib.sha256(
            request.read_bytes()
        ).hexdigest()
    frozen = {
        "files": hashes,
        "max_calls": len(names) if max_calls is None else max_calls,
        "automatic_retries": 0,
        "blocks": names,
        "report_id": report_id,
    }
    save(output / "freeze.json", frozen)
    for path in implementation_files(repo):
        save(output / "frozen_code" / path.relative_to(repo), path.read_bytes())


def receive(repo, folder, name, request, config, replay_source=None):
    data = call(repo, folder, request, parse, replay_source=replay_source)
    return data, []


def extract(repo, args, config, sources, summary):
    replay = args.stage == "replay"
    if replay:
        source_summary = parse((sources[0] / "summary.json").read_text("utf8"))
        names = list(source_summary.get("blocks", {}))
        if not names:
            names = sorted(
                path.name for path in (sources[0] / "blocks").iterdir() if path.is_dir()
            )
        # A missing capture is an error before making any output requests.
        for name in names:
            for file in ("request.json", "response.body", "receipt.json"):
                if not (sources[0] / "blocks" / name / file).is_file():
                    raise FileNotFoundError(str(sources[0] / "blocks" / name / file))
        report_id = (
            source_summary.get("report_id")
            or "recording_"
            + hashlib.sha256(
                b"".join(
                    (sources[0] / "blocks" / name / "response.body").read_bytes()
                    for name in names
                )
            ).hexdigest()
        )
        summary.update(
            source_run=str(sources[0].resolve()),
            pdf_pages=source_summary.get("pdf_pages"),
            replayed_responses=0,
        )
        content = []
    else:
        contract_names = planned_jobs(config, args.profile)
        names = list(contract_names)
        if args.block:
            if args.block not in names:
                raise ValueError("block_not_in_selected_profile")
            names = [args.block]
        summary["execution_plan"] = {
            "ownership_blocks": contract_names,
            "execution_blocks": names,
        }
        content, summary["pdf_pages"] = read_pdf(sources[0])
        report_id = hashlib.sha256(sources[0].read_bytes()).hexdigest()
        content.insert(
            0,
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "expected_company": args.company,
                        "expected_year": args.year,
                        "profiles": args.profile,
                        "frozen_job_ownership": {
                            name: {
                                "topics": config["jobs"][name]["topics"],
                                "modules": config["jobs"][name]["modules"],
                            }
                            for name in contract_names
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        )
    summary.update(
        blocks={},
        input_tokens_estimate=0,
        max_calls=0 if replay else len(names),
        report_id=report_id,
        profile_ids=args.profile,
    )
    jobs = []
    for name in names:
        folder = args.output / "blocks" / name
        if replay:
            request, info = prepare_replay(repo, folder, sources[0] / "blocks" / name)
        else:
            request = payload(config, name, content)
            info = prepare_request(repo, folder, request, sources)
        jobs.append((name, folder, request))
        summary["blocks"][name] = {
            "label": config["extract_blocks"].get(name, name),
            "status": "prepared",
            **info,
        }
        summary["input_tokens_estimate"] += info["input_tokens_estimate"]
    if not replay:
        summary["budget"] = budget(config, summary["blocks"])
        summary["budget"]["planning_limit_usd"] = args.budget_limit_usd
        save(args.output / "call_plan.json", summary)
    freeze_run(
        repo,
        args.output,
        [] if replay else sources,
        jobs,
        names,
        report_id,
        max_calls=0 if replay else len(names),
    )
    if (
        not replay
        and args.budget_limit_usd is not None
        and (summary["budget"]["peak_uncached_cost_estimate"] > args.budget_limit_usd)
    ):
        raise ValueError("frozen_plan_exceeds_budget_limit")
    if not args.paid and not replay:
        return None
    parts, incomplete = {}, {}
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "calls_missing_usage": 0}
    for name, folder, request in jobs:
        print(json.dumps({"block": name, "status": "started"}), flush=True)
        if replay:
            summary["replayed_responses"] += 1
        else:
            summary["model_calls"] += 1
        data, errors = receive(
            repo,
            folder,
            name,
            request,
            config,
            replay_source=sources[0] / "blocks" / name if replay else None,
        )
        receipt = parse((folder / "receipt.json").read_text("utf8"))
        usage = receipt.get("usage") or {}
        if not replay:
            if all(
                type(usage.get(key)) is int
                for key in ("prompt_tokens", "completion_tokens")
            ):
                for key in ("prompt_tokens", "completion_tokens"):
                    totals[key] += usage[key]
            else:
                totals["calls_missing_usage"] += 1
        if receipt.get("completion_note"):
            status = "truncated"
            incomplete[name] = {
                "run_status": status,
                "parsed_content": data,
                "completion_note": receipt["completion_note"],
            }
        elif data is None:
            status = "failed"
            incomplete[name] = {
                "run_status": status,
                "receipt": receipt,
                "raw_files": ["response.body", "content.txt"],
            }
        else:
            status = "received"
        if data is not None:
            parts[name] = data
        summary["blocks"][name].update(status=status, format_notes=errors)
        if data is not None:
            publish(
                folder,
                data,
                config["labels"],
                context={"_raw_only": not isinstance(data, dict)},
            )
        print(json.dumps({"block": name, "status": status}), flush=True)
    result = combine_blocks(config, parts, args.company, args.year, report_id=report_id)
    result["unstructured_blocks"].update(incomplete)
    for name in incomplete:
        result["block_status"][name] = incomplete[name]["run_status"]
    summary["usage_totals"] = totals
    summary["status"] = "partial" if incomplete else "completed"
    summary["retained_tables"] = len(result["tables"])
    summary["unstructured_blocks"] = list(result["unstructured_blocks"])
    event(
        args.output,
        "blocks_assembled",
        retained_blocks=len(parts),
        model_calls=summary["model_calls"],
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=["extract", "replay", "history", "peers", "audit"]
    )
    parser.add_argument("--source", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--block")
    parser.add_argument("--company")
    parser.add_argument("--year", type=int)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--budget-limit-usd", type=float)
    parser.add_argument("--paid", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    config = parse((repo / "config/fundamental_data_v2.json").read_text("utf8"))
    if args.stage == "extract" and (len(args.source) != 1 or not args.profile):
        parser.error("extract requires one PDF and explicit --profile selection")
    if any(profile not in config["industry_profiles"] for profile in args.profile):
        parser.error("unknown industry profile")
    args.profile = list(dict.fromkeys(args.profile))
    if args.stage in {"history", "replay", "audit"} and args.paid:
        parser.error("history/replay/audit are offline and do not use --paid")
    if args.budget_limit_usd is not None and not (
        0 < args.budget_limit_usd < float("inf")
    ):
        parser.error("budget limit must be a finite positive USD amount")
    if args.paid and args.budget_limit_usd is None:
        parser.error("--paid requires a reviewed --budget-limit-usd")
    if args.stage == "replay":
        if len(args.source) != 1 or not (args.source[0] / "blocks").is_dir():
            parser.error("replay requires one recorded extraction directory")
        sources = args.source
    else:
        sources = [p / "data.json" if p.is_dir() else p for p in args.source]
    for source in args.source:
        protected = (
            source
            if source.is_dir()
            else (source.parent if source.name == "data.json" else None)
        )
        if protected is not None and args.output.resolve().is_relative_to(
            protected.resolve()
        ):
            parser.error("output must be outside the immutable source directory")
    if args.stage != "replay" and any(not p.is_file() for p in sources):
        parser.error("input file not found")
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {"stage": args.stage, "model_calls": 0, "status": "prepared"}
    event(args.output, "run_started", stage=args.stage, paid=args.paid)
    data = None
    if args.stage in {"extract", "replay"}:
        data = extract(repo, args, config, sources, summary)
    else:
        records = [parse(p.read_text("utf8")) for p in sources]
        if args.stage == "audit":
            reports = []
            for source, record in zip(sources, records):
                stage = args.block or (
                    "history" if record.get("stage") == "history" else "extract"
                )
                reports.append(
                    {
                        "source": str(source.resolve()),
                        "schema_version": record.get("schema_version"),
                        "stage": stage,
                        "issues": validate(config, stage, record),
                    }
                )
            save(args.output / "audit.json", {"reports": reports})
            summary.update(
                status="reported",
                issues=sum(len(report["issues"]) for report in reports),
            )
        elif args.stage == "history":
            data = assemble(records, config)
            summary["status"] = "completed"
        else:
            projection = peer_input(records, config)
            request = payload(
                config, "peers", json.dumps(projection, ensure_ascii=False)
            )
            info = prepare_request(repo, args.output, request, sources)
            summary.update(info)
            summary["budget"] = budget(config, {"peers": info})
            if info["input_tokens_estimate"] > config["peer_input"]["max_input_tokens"]:
                raise ValueError("peer_context_exceeds_reviewed_limit_no_truncation")
            if args.budget_limit_usd is not None and (
                summary["budget"]["peak_uncached_cost_estimate"] > args.budget_limit_usd
            ):
                raise ValueError("peer_plan_exceeds_budget_limit")
            save(args.output / "call_plan.json", summary)
            if args.paid:
                summary["model_calls"] = 1
                data, _errors = receive(repo, args.output, "peers", request, config)
                receipt = parse((args.output / "receipt.json").read_text("utf8"))
                if receipt.get("completion_note"):
                    save(args.output / "incomplete_parsed.json", data)
                summary["status"] = (
                    "incomplete"
                    if data is None or receipt.get("completion_note")
                    else "completed"
                )
                if isinstance(data, dict):
                    data["companies"] = projection["companies"]
    if data is not None:
        publish(args.output, data, config["labels"])
        summary["retained_tables"] = (
            len(data.get("tables", [])) if isinstance(data, dict) else 0
        )
        summary["statement_rows"] = {
            role: sum(
                len(table["rows"])
                for table in (data.get("tables", []) if isinstance(data, dict) else [])
                if isinstance(table, dict)
                and table.get("role") == role
                and isinstance(table.get("rows"), list)
            )
            for role in ("BS", "IS", "CFS")
        }
        summary["capital_rows"] = sum(
            len(table["rows"])
            for table in (data.get("tables", []) if isinstance(data, dict) else [])
            if isinstance(table, dict)
            and table.get("role") == "capital"
            and isinstance(table.get("rows"), list)
        )
    save(args.output / "summary.json", summary)
    event(
        args.output,
        "run_finished",
        status=summary["status"],
        model_calls=summary["model_calls"],
    )
    save(
        args.output / "output_hashes.json",
        {
            str(p.relative_to(args.output)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(args.output.rglob("*"))
            if p.is_file()
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary["status"] in {"partial", "incomplete", "needs_attention"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
