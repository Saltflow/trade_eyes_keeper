"""Canonical financial CLI; frozen extraction/context, explicit summary execution."""

from __future__ import annotations

import argparse
import importlib.metadata
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fundamental_data.handoff import (
    company_code,
    load,
    metadata_records,
    read_stage3_input,
    save,
    separate,
    sha,
    stage3_input,
)

REPO = Path(__file__).resolve().parents[1]


def context_source(sources: list, output: Path, baseline: Path | None = None) -> Path:
    """Mechanically collect declared runs; no financial alignment or repair here."""
    protected = sources + ([baseline] if baseline else [])
    separate(output, protected)
    if len(sources) == 1:
        return sources[0]
    adapter = output.with_name(output.name + "_inputs")
    separate(adapter, protected + [output])
    rows, seen = [], set()
    for source in sources:
        for record in load(source / "sources.json")["records"]:
            name = f"{company_code(str(record['code']))}_{int(record['year'])}"
            if name in seen:
                raise ValueError(f"duplicate_declared_company_year: {name}")
            seen.add(name)
            rows.append((record, source / "stage1" / name))
    for record, origin in rows:
        name = f"{record['code']}_{record['year']}"
        for path in [
            origin / "data.json",
            origin / "阅读.md",
            *origin.glob("blocks/*/content.txt"),
        ]:
            target = adapter / "stage1" / name / path.relative_to(origin)
            save(target, path.read_bytes())
    save(
        adapter / "sources.json",
        {
            "records": [
                {**record, "stage1_source": str(origin.resolve())}
                for record, origin in rows
            ]
        },
    )
    return adapter


def positive(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except ArithmeticError as error:
        raise argparse.ArgumentTypeError("invalid budget") from error
    if not number.is_finite() or number <= 0:
        raise argparse.ArgumentTypeError("budget must be finite and positive")
    return number


def prepare_summary(
    source: Path,
    output: Path,
    metadata: Path,
    repo: Path,
    maximum: Decimal,
    companies=(),
    appendices=(),
) -> dict:
    """Prepare exactly one best-effort request per company; never add recovery."""
    from tokenizers import Tokenizer

    from scripts import run_company_summary as runner

    separate(output, [source, metadata, *appendices])
    config_path = repo / "config/company_summary_contract.json"
    config = load(config_path)
    metadata_config = repo / "config/fundamental_data_v2.json"
    records, protected = metadata_records(metadata, load(metadata_config), companies)
    codes = (
        list(companies)
        if companies
        else sorted(p.parent.name for p in source.glob("*/模型正文.md"))
    )
    if not codes or len(codes) != len(set(codes)):
        raise ValueError("companies_must_be_nonempty_and_unique")
    codes = [company_code(code) for code in codes]
    if appendices and (len(codes) != 1 or len(companies) != 1):
        raise ValueError("appendices_require_one_explicit_company_per_plan")
    tokenizer_path = repo / "data/fundamental_quant/tokenizer/tokenizer.json"
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    prompt_path = repo / config["prompts"]["summary"]
    example_path = repo / config["prompts"]["example"]
    plan = {
        "authorized_usd": str(maximum),
        "authorization": "Explicit CLI budget; no retries or recovery calls.",
        "endpoint": runner.ENDPOINT,
        "input_cost_safety_factor": "1.25",
        "peak_rates": config["usd_per_million_cache_miss"]["peak"],
        # Preserve the frozen executor's verified weekend pricing, separately
        # from the peak budget reservation. The executor enforces pricing dates.
        "execution_rates": config.get(
            "execution_rates",
            {
                **config["usd_per_million_cache_miss"]["off_peak"],
                "cached_input": 0.003,
            },
        ),
        "pricing_utc_dates": [config["price_checked_date"]],
        "price_source": config["price_source"],
        "tokenizer": str(tokenizer_path),
        "input_waterline": config["input_waterline"],
        "config": config,
        "source_package": str(source),
        "max_parallel": 1,
        "jobs": [],
        "phases": [],
    }
    requests = []
    for code in codes:
        body_path = source / code / "模型正文.md"
        text = f"# 公司 {code}\n\n" + "\n".join(
            f"## Q{i} {q}" for i, q in enumerate(config["questions"], 1)
        )
        text += "\n\n# 排版示例（非本公司事实）\n\n" + example_path.read_text("utf8")
        text += "\n\n# 完整第二阶段正文\n\n" + body_path.read_bytes().decode("utf8")
        text += "\n\n# 已有可选附录\n\n不安排恢复或补提取；没有附录也直接摘要。"
        for appendix in appendices:
            text += f"\n\n## {appendix.name}\n\n" + appendix.read_bytes().decode("utf8")
        request = {
            "model": config["model"],
            "thinking": config["thinking"],
            "reasoning_effort": config["reasoning_effort"],
            "max_tokens": config["summary_max_tokens"],
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "text"},
            "messages": [
                {"role": "system", "content": prompt_path.read_text("utf8")},
                {"role": "user", "content": text},
            ],
        }
        count = runner.estimate(request, tokenizer, config)
        if count > config["input_waterline"]:
            raise ValueError(f"input_exceeds_waterline_no_truncation: {code}")
        job = {
            "id": f"{code}_summary",
            "kind": "summary",
            "dependencies": [],
            "sources": [str(body_path)],
            "input_bound": count,
            "max_tokens": request["max_tokens"],
            "reserved_usd_peak": str(
                runner.reserve(count, request["max_tokens"], plan)
            ),
        }
        plan["jobs"].append(job)
        plan["phases"].append([job["id"]])
        requests.append((job["id"], request))
        protected.append(body_path)
    plan["reserved_usd_peak"] = str(
        sum(Decimal(j["reserved_usd_peak"]) for j in plan["jobs"])
    )
    if Decimal(plan["reserved_usd_peak"]) > maximum:
        raise ValueError("planned_reservation_exceeds_budget")
    protected += [
        config_path,
        metadata_config,
        prompt_path,
        example_path,
        tokenizer_path,
        Path(__file__),
        repo / "src/fundamental_data/handoff.py",
        repo / "scripts/run_company_summary.py",
        *appendices,
    ]
    hashes = {str(p.resolve()): sha(p) for p in protected}
    save(output / "元数据.json", records)
    for name, request in requests:
        save(output / "templates" / f"{name}.json", request)
    save(output / "plan.json", plan)
    hashes.update({str(p.resolve()): sha(p) for p in output.rglob("*") if p.is_file()})
    save(
        output / "freeze.json",
        {
            "files": hashes,
            "maximum_calls": len(requests),
            "automatic_retries": 0,
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in (
                    "requests",
                    "urllib3",
                    "python-dotenv",
                    "tokenizers",
                    "Markdown",
                    "bleach",
                )
            },
        },
    )
    save(output / "freeze.sha256", sha(output / "freeze.json"))
    return {
        "model_calls": 0,
        "planned_calls": len(requests),
        "reserved_usd_peak": plan["reserved_usd_peak"],
        "output": str(output),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="stage", required=True)
    for name in ("extract", "context", "summary-prepare", "stage3-input"):
        command = commands.add_parser(name)
        command.add_argument(
            "--source",
            type=Path,
            required=True,
            **({"nargs": "+"} if name == "context" else {}),
        )
        command.add_argument("--output", type=Path, required=True)
        if name in {"summary-prepare", "stage3-input"}:
            command.add_argument("--metadata", type=Path, required=True)
            command.add_argument("--company", action="append", default=[])
        if name in {"extract", "summary-prepare"}:
            command.add_argument(
                "--budget-limit-usd", type=positive, required=name == "summary-prepare"
            )
        if name == "extract":
            command.add_argument("--company", required=True)
            command.add_argument("--year", type=int, required=True)
            command.add_argument("--profile", action="append", required=True)
            command.add_argument("--paid", action="store_true")
            command.add_argument("--block")
        if name == "context":
            command.add_argument("--baseline", type=Path)
        if name == "summary-prepare":
            command.add_argument("--appendix", type=Path, action="append", default=[])
    command = commands.add_parser("summary-run")
    command.add_argument("--source", type=Path, required=True)
    command.add_argument("--paid", action="store_true", required=True)
    command = commands.add_parser("stage3-read")
    command.add_argument("--source", type=Path, required=True)
    args = parser.parse_args(argv)
    sources = [p.resolve() for p in args.source] if args.stage == "context" else []
    source = sources[0] if sources else args.source.resolve()
    if args.stage in {"extract", "context"}:
        if args.stage == "extract":
            if args.paid and args.budget_limit_usd is None:
                parser.error("--paid requires --budget-limit-usd")
            output = args.output.resolve()
            separate(output, [source])
            code = company_code(args.company)
            record = {
                "code": code,
                "year": args.year,
                "pdf": str(source),
                "profile": args.profile,
            }
            save(output / "sources.json", {"records": [record]})
            forwarded = [
                "extract",
                "--source",
                str(source),
                "--output",
                str(output / "stage1" / f"{code}_{args.year}"),
                "--company",
                args.company,
                "--year",
                str(args.year),
            ]
            for profile in args.profile:
                forwarded += ["--profile", profile]
            if args.budget_limit_usd is not None:
                forwarded += ["--budget-limit-usd", str(args.budget_limit_usd)]
            if args.paid:
                forwarded.append("--paid")
            if args.block:
                forwarded += ["--block", args.block]
            script = "build_fundamental_data_v2.py"
        else:
            source = context_source(
                sources,
                args.output.resolve(),
                args.baseline.resolve() if args.baseline else None,
            )
            forwarded = [
                "--source-run",
                str(source),
                "--output",
                str(args.output.resolve()),
            ]
            if args.baseline:
                forwarded += ["--baseline", str(args.baseline.resolve())]
            script = "build_financial_context.py"
        return subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                str(REPO / "scripts" / script),
                *forwarded,
            ],
            cwd=REPO,
            check=False,
        ).returncode
    if args.stage == "summary-prepare":
        result = prepare_summary(
            source,
            args.output.resolve(),
            args.metadata.resolve(),
            REPO,
            args.budget_limit_usd,
            args.company,
            args.appendix,
        )
    elif args.stage == "summary-run":
        from scripts import run_company_summary as runner

        plan = load(source / "plan.json")
        if not plan["jobs"] or any(
            j["kind"] != "summary" or j["dependencies"] for j in plan["jobs"]
        ):
            raise ValueError("canonical_entry_requires_summary_only_plan")
        runner.execute(REPO, source)
        receipts = load(source / "执行结果.json")["receipts"]
        return (
            0
            if len(receipts) == len(plan["jobs"])
            and all(r["status"] == "complete" for r in receipts)
            else 2
        )
    elif args.stage == "stage3-input":
        result = stage3_input(
            source, args.metadata.resolve(), args.output.resolve(), REPO, args.company
        )
    else:
        result = read_stage3_input(source)
        sys.stdout.buffer.write(result["body"].encode("utf8"))
        return 0
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
