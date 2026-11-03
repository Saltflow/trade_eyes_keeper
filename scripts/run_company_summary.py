"""Freeze and execute the approved eight-call summary plan, once, into new files."""

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import bleach
import markdown
import requests
from dotenv import dotenv_values
from tokenizers import Tokenizer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.model_gateway.adapters import perform_authorized
from src.model_gateway.integrity import implementation_files as gateway_files
from src.model_gateway.transport import (
    ENDPOINT,
    Stream,
    cost,
    estimate,
    load,
    now,
    reserve,
    save,
    sha,
    verify,
)
from src.platform_composition import authority_for

VARIABLE = re.compile(r"\{\{RECOVERY_(CONTENT|STATUS):([A-Za-z0-9_]+)\}\}")
__all__ = ["Stream", "cost", "datetime", "timezone"]


def perform(folder, job, request, plan, key, post=None):
    repo = Path(__file__).resolve().parents[1]
    plan = {"endpoint": ENDPOINT, **plan}
    # Injected transport is an isolated acceptance, never a production namespace.
    authority = authority_for(
        repo,
        explicit_root=(Path(folder).resolve() / ".model_authority") if post else None,
        isolated=post is not None,
    )
    return perform_authorized(
        folder,
        job,
        request,
        plan,
        key,
        post=post,
        authority=authority,
        authorization="summary:" + str(Path(folder).resolve()),
        producer="summary:" + str(Path(folder).resolve()),
        logical_intent=job.get("logical_intent")
        or (
            "financial_legacy_stream:"
            + job.get("kind", "fixed_stage")
            + ":"
            + hashlib.sha256(
                json.dumps(
                    request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
        ),
        clock=datetime,
    )


def bind(request, dependencies, folder):
    """One substitution pass on original template; inserted model text is opaque."""
    bound = copy.deepcopy(request)
    records = {}
    for dependency in dependencies:
        root = folder / "jobs" / dependency
        receipt = load(root / "receipt.json")
        content = (root / "content.md").read_bytes().decode("utf8")
        records[dependency] = {
            "CONTENT": content
            if content
            else "本次恢复无可见正文，执行缺口仍在；不能解释为原文未披露。",
            "STATUS": f"状态={receipt['status']}；HTTP={receipt.get('http_status')}；finish_reason={receipt.get('finish_reason')}；流闭合={receipt.get('done')}；传输注记={receipt.get('transport_notes')}；异常={receipt.get('error_type')}；原可见片段完整保留。",
        }
    for message in bound["messages"]:
        parts = message["content"]
        if isinstance(parts, str):
            continue
        for part in parts:
            if part["type"] == "text":
                original = part["text"]
                if any(m.group(2) not in records for m in VARIABLE.finditer(original)):
                    raise ValueError("unknown_dependency_variable")
                part["text"] = VARIABLE.sub(
                    lambda m: records[m.group(2)][m.group(1)], original
                )
    return bound


def prepare(repo, package, folder, maximum):
    if folder.exists() or package in folder.parents or folder in package.parents:
        raise ValueError("execution_output_must_be_new_and_separate")
    config = load(repo / "config/company_summary_contract.json")
    previous = load(package / "调用计划.json")
    hashes = load(package / "冻结输入哈希.json")
    verify(hashes)
    verify(
        {
            str((repo / p).resolve()): digest
            for p, digest in load(package / "准备文件哈希.json").items()
        }
    )
    tokenizer_path = repo / "data/fundamental_quant/tokenizer/tokenizer.json"
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    plan = {
        "authorized_usd": str(maximum),
        "authorization": "用户授权现有三公司三年，预算上限US$2；原第一/二阶段及旧结果不动；调用前冻结；不重试、不追加；调用后不修改代码/prompt/计划/正文。",
        "endpoint": ENDPOINT,
        "input_cost_safety_factor": "1.25",
        "peak_rates": config["usd_per_million_cache_miss"]["peak"],
        "execution_rates": {
            **config["usd_per_million_cache_miss"]["off_peak"],
            "cached_input": 0.003,
        },
        "pricing_utc_dates": ["2026-10-24", "2026-10-25"],
        "price_checked_at": now(),
        "price_source": config["price_source"],
        "tokenizer": str(tokenizer_path),
        "input_waterline": config["input_waterline"],
        "config": config,
        "source_package": str(package),
        "phases": [],
        "jobs": [],
        "max_parallel": 3,
        "binding": "One pass on original template; only RECOVERY_CONTENT and RECOVERY_STATUS; complete visible raw content; no model JSON parse; no truncation; preserve failure/partial; no reasoning in next prompt.",
        "transport_overlay": {"stream_options": {"include_usage": True}},
        "artifact_policy": "Only new run request/log/response/receipt/Markdown/HTML/report artifacts may be generated by frozen executor. No post-call code, prompt, plan, old outputs or model text edits.",
    }
    for item in previous["jobs"]:
        template = package / item["id"] / "request.template.json"
        if (
            sha(template) != item["request_sha256"]
            or template.stat().st_size != item["request_bytes"]
        ):
            raise ValueError("approved_request_template_changed")
        request = load(template)
        request["stream_options"] = {"include_usage": True}
        input_bound = (
            estimate(request, tokenizer, config) + item["dependency_token_reserve"]
        )
        if input_bound > config["input_waterline"]:
            raise ValueError("planned_input_exceeds_waterline")
        job = {
            "id": item["id"],
            "kind": item["kind"],
            "dependencies": item["dependencies"],
            "sources": item["sources"],
            "input_bound": input_bound,
            "max_tokens": request["max_tokens"],
            "reserved_usd_peak": str(reserve(input_bound, request["max_tokens"], plan)),
        }
        save(folder / "templates" / (job["id"] + ".json"), request)
        plan["jobs"].append(job)
    plan["phases"] = [
        [j["id"] for j in plan["jobs"] if j["kind"] == kind]
        for kind in ("recovery", "summary")
    ]
    plan["reserved_usd_peak"] = str(
        sum(Decimal(j["reserved_usd_peak"]) for j in plan["jobs"])
    )
    if Decimal(plan["reserved_usd_peak"]) > maximum:
        raise ValueError("planned_reservation_exceeds_authorized_budget")
    save(folder / "plan.json", plan)
    for base in (package, folder / "templates"):
        hashes.update(
            {str(p.resolve()): sha(p) for p in base.rglob("*") if p.is_file()}
        )
    canonical = [
        repo / "scripts/run_company_summary.py",
        repo / "tests/test_company_summary_execution.py",
        repo / "config/company_summary_contract.json",
        repo / "config/prompts/company_summary.md",
        repo / "config/prompts/company_summary_recovery.md",
        repo / "docs/design/company_summary_example.md",
        repo / "docs/design/company_summary_contract.md",
        repo / "scripts/prepare_company_summary.py",
        tokenizer_path,
        folder / "plan.json",
        *gateway_files(),
    ]
    hashes.update({str(p.resolve()): sha(p) for p in canonical})
    save(
        folder / "freeze.json",
        {
            "created_at": now(),
            "files": hashes,
            "python": sys.version,
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
            "maximum_calls": len(plan["jobs"]),
            "automatic_retries": 0,
        },
    )
    save(folder / "freeze.sha256", sha(folder / "freeze.json"))
    print(
        json.dumps(
            {
                "folder": str(folder),
                "calls": len(plan["jobs"]),
                "peak_reservation_with_25pct_input_margin": plan["reserved_usd_peak"],
                "authorized_usd": str(maximum),
                "frozen_files": len(hashes),
            },
            ensure_ascii=False,
        )
    )


def html(text):
    body = bleach.clean(
        markdown.markdown(text, extensions=["tables", "fenced_code"]),
        tags=[
            "h1",
            "h2",
            "h3",
            "h4",
            "p",
            "strong",
            "em",
            "ul",
            "ol",
            "li",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
            "br",
            "pre",
            "code",
            "hr",
            "blockquote",
            "a",
        ],
        attributes={"a": ["href"]},
        strip=True,
    )
    return (
        '<!doctype html><meta charset="utf-8"><style>body{font:16px/1.7 "Microsoft YaHei",sans-serif;max-width:1250px;margin:30px auto;padding:0 24px}table{border-collapse:collapse;display:block;overflow:auto}th,td{border:1px solid #ccc;padding:7px 10px;min-width:90px}th{background:#edf2f8}pre{white-space:pre-wrap}h2{margin-top:36px}</style>'
        + body
    )


def report(folder, plan):
    receipts, summaries, recoveries = [], [], []
    tokenizer = Tokenizer.from_file(plan["tokenizer"])
    for job in plan["jobs"]:
        root = folder / "jobs" / job["id"]
        receipt = (
            load(root / "receipt.json")
            if (root / "receipt.json").exists()
            else {
                "id": job["id"],
                "status": "interrupted_receipt_missing"
                if (root / "attempt.json").exists()
                else "not_sent",
                "usage": None,
                "cost": {"usd": None, "kind": "not_sent"},
                "reserved_usd_peak": job["reserved_usd_peak"],
            }
        )
        receipts.append(receipt)
        text = (
            (root / "content.md").read_bytes().decode("utf8")
            if (root / "content.md").exists()
            else "本次无可见正文；执行缺口。"
        )
        target = summaries if job["kind"] == "summary" else recoveries
        target.append(
            f"# {job['id']}\n\n状态：{receipt['status']}；finish_reason：{receipt.get('finish_reason')}。以下是模型完整可见正文，未人工修补。\n\n"
            + text
        )
        if job["kind"] == "summary":
            save(folder / job["id"].split("_")[0] / "完整摘要.md", text)
            save(folder / job["id"].split("_")[0] / "完整摘要.html", html(text))
    verify(load(folder / "freeze.json")["files"])
    sent = [
        r for r in receipts if (folder / "jobs" / r["id"] / "attempt.json").exists()
    ]
    known = sum(Decimal(r["cost"]["usd"]) for r in sent if r["cost"]["usd"] is not None)
    unknown = sum(
        Decimal(r["reserved_usd_peak"]) for r in sent if r["cost"]["usd"] is None
    )
    combined = "\n\n---\n\n".join(summaries)

    def usage_number(receipt, key, detail=None):
        usage = receipt.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        if detail:
            usage = usage.get(detail)
            usage = usage if isinstance(usage, dict) else {}
        value = usage.get(key)
        return value if type(value) is int and value >= 0 else 0

    summary = {
        "finished_at": now(),
        "requests_sent": len(sent),
        "planned_calls": len(plan["jobs"]),
        "automatic_retries": 0,
        "known_usage_cost_usd": str(known),
        "unknown_usage_reservation_usd": str(unknown),
        "cost_scope": "Returned DeepSeek usage at published weekend rates; not provider-console debit and not Codex usage.",
        "prompt_tokens_known": sum(usage_number(r, "prompt_tokens") for r in sent),
        "completion_tokens_known": sum(
            usage_number(r, "completion_tokens") for r in sent
        ),
        "reasoning_tokens_known": sum(
            usage_number(r, "reasoning_tokens", "completion_tokens_details")
            for r in sent
        ),
        "all_usage_known": all(r["cost"]["usd"] is not None for r in sent),
        "all_costs_precise_from_usage": all(
            r["cost"]["kind"] == "usage_at_published_rates" for r in sent
        ),
        "protected_files_unchanged": True,
        "summary_tokens_local": len(tokenizer.encode(combined).ids),
        "receipts": receipts,
    }
    save(folder / "执行结果.json", summary)
    for name, text in (
        ("完整摘要", combined),
        ("完整恢复附录", "\n\n---\n\n".join(recoveries)),
    ):
        save(folder / (name + ".md"), text)
        save(folder / (name + ".html"), html(text))
    lines = [
        "# 本批执行报告",
        "完整摘要和完整恢复附录均为冻结执行器生成，正文未经人工修补；局部/中断照常保留。",
        "",
        "| 调用 | 状态 | 输入tokens | 输出tokens（含thinking） | reasoning | 可见正文本地tokens | usage计价USD |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in receipts:
        u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
        details = u.get("completion_tokens_details")
        details = details if isinstance(details, dict) else {}
        lines.append(
            f"| {r['id']} | {r['status']} | {u.get('prompt_tokens', '未知')} | {u.get('completion_tokens', '未知')} | {details.get('reasoning_tokens', '未知')} | {r.get('visible_tokens_local', '未知')} | {r['cost']['usd'] or '未知'} |"
        )
    lines += [
        "",
        f"已发{len(sent)}/{len(plan['jobs'])}次，自动重试0；已知usage计价US${known}，未知usage仍保留US${unknown}，授权上限US${plan['authorized_usd']}。",
        "价格依据：https://api-docs.deepseek.com/quick_start/pricing/；按本次冻结的费率估算，completion已含reasoning，不重复加计。服务商账户实际扣费未查询。",
        "",
        "缺口补齐与未解决项：完整恢复附录逐块保存模型自己的采用/未定位/限制说明，完整摘要还保留其最终采用判断；这些是模型声明，不是人工核验正确性的证明。原始失败状态和旧数据不变。",
        "",
        f"原来源/代码/prompt/计划全部冻结哈希复查通过。完整摘要合计本地token估计{summary['summary_tokens_local']}；单公司超过60k目标仍保留，不自动续调或裁尾。",
    ]
    save(folder / "执行报告.md", "\n".join(lines))
    save(folder / "执行报告.html", html("\n".join(lines)))
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k != "receipts"}, ensure_ascii=False
        ),
        flush=True,
    )


def execute(repo, folder):
    if sha(folder / "freeze.json") != (folder / "freeze.sha256").read_text():
        raise ValueError("freeze_manifest_changed")
    frozen, plan = load(folder / "freeze.json"), load(folder / "plan.json")
    verify(frozen["files"])
    key = os.getenv("DEEPSEEK_API_KEY") or dotenv_values(repo / "config/.env").get(
        "DEEPSEEK_API_KEY"
    )
    if not key:
        raise ValueError("missing_api_key")
    save(
        folder / "execution_started.json",
        {
            "at": now(),
            "authorized_usd": plan["authorized_usd"],
            "maximum_calls": len(plan["jobs"]),
        },
    )
    tokenizer = Tokenizer.from_file(plan["tokenizer"])
    completed = []
    try:
        for phase in plan["phases"]:
            jobs = [j for j in plan["jobs"] if j["id"] in phase]
            remaining = [
                j for j in plan["jobs"] if j["id"] not in {r["id"] for r in completed}
            ]
            accounted = sum(
                Decimal(r["cost"]["usd"] or r["reserved_usd_peak"]) for r in completed
            )
            if accounted + sum(
                Decimal(j["reserved_usd_peak"]) for j in remaining
            ) > Decimal(plan["authorized_usd"]):
                raise ValueError("remaining_budget_insufficient")
            prepared = []
            for job in jobs:
                request = bind(
                    load(folder / "templates" / (job["id"] + ".json")),
                    job["dependencies"],
                    folder,
                )
                actual_input = estimate(request, tokenizer, plan["config"])
                if actual_input > plan["input_waterline"] or reserve(
                    actual_input, request["max_tokens"], plan
                ) > Decimal(job["reserved_usd_peak"]):
                    raise ValueError("bound_request_exceeds_frozen_capacity_or_budget")
                prepared.append((job, request))
            with ThreadPoolExecutor(max_workers=plan["max_parallel"]) as pool:
                futures = [
                    pool.submit(perform, folder, job, request, plan, key)
                    for job, request in prepared
                ]
                completed.extend(future.result() for future in futures)
    except (
        requests.RequestException,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        IndexError,
        AttributeError,
        RuntimeError,
    ) as error:
        save(
            folder / "execution_error.json",
            {
                "at": now(),
                "error_type": type(error).__name__,
                "message": str(error)
                if isinstance(error, ValueError)
                else "executor stopped; no retry; see preserved per-job artifacts",
            },
        )
    report(folder, plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "execute"])
    parser.add_argument("--package", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-usd", type=Decimal, default=Decimal(2))
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if args.mode == "prepare":
        prepare(repo, args.package.resolve(), args.output.resolve(), args.budget_usd)
    else:
        execute(repo, args.output.resolve())


if __name__ == "__main__":
    main()
