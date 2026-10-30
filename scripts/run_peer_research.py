"""Freeze one approved peer-research request; reuse the recorded SSE transport."""

import argparse
import importlib.metadata
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import run_company_summary as transport
from src.fundamental_data.handoff import separate

REPO = Path(__file__).resolve().parents[1]


def prepare(repo: Path, source: Path, output: Path, maximum: Decimal) -> dict:
    separate(output, [source])
    if not maximum.is_finite() or maximum <= 0:
        raise ValueError("budget_must_be_finite_positive")
    previous = transport.load(source / "调用规划.json")
    transport.verify(previous["source_hashes"])
    protected = transport.load(source / "前置阶段保护.json")
    transport.verify(protected)
    artifacts = transport.load(source / "准备产物哈希.json")
    transport.verify({str(source / p): h for p, h in artifacts.items()})
    raw = (source / "request.preview.json").read_bytes()
    request = json.loads(raw)
    if request["model"] != "deepseek-flash":
        raise ValueError("frozen_pricing_requires_deepseek_flash")
    if json.dumps(request, ensure_ascii=False, indent=2).encode("utf8") != raw:
        raise ValueError("transport_serialization_would_change_approved_request")
    config = transport.load(repo / "config/company_summary_contract.json")
    from tokenizers import Tokenizer

    tokenizer_path = repo / "data/fundamental_quant/tokenizer/tokenizer.json"
    estimate = transport.estimate(
        request, Tokenizer.from_file(str(tokenizer_path)), config
    )
    if estimate > config["input_waterline"]:
        raise ValueError("input_over_waterline_no_truncation")
    plan = {
        "kind": "one_peer_research_request_v1",
        "authorized_usd": str(maximum),
        "maximum_calls": 1,
        "automatic_retries": 0,
        "automatic_additional_requests": False,
        "source_package": str(source),
        "request_sha256": transport.sha(source / "request.preview.json"),
        "endpoint": transport.ENDPOINT,
        "input_tokens_estimate": estimate,
        "input_waterline": config["input_waterline"],
        "model_input_capacity_bound": 1048576,
        "peak_rates": {"input": 0.30, "output": 1.20},
        "execution_rates": {"input": 0.15, "output": 0.60, "cached_input": 0.003},
        "pricing_utc_dates": ["2026-10-24", "2026-10-25"],
        "pricing_checked_at": transport.now(),
        "price_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "rate_scope": "Verified 2026-10-24: weekend off-peak; peak bound for budget. Not provider-console debit.",
        "tokenizer": str(tokenizer_path),
        "budget_method": "Peak uncached rate on 1M conservatively treated as 1,048,576 input tokens plus frozen output quota; no cache savings assumed.",
    }
    reservation = (
        Decimal(plan["model_input_capacity_bound"]) * Decimal("0.30")
        + Decimal(request["max_tokens"]) * Decimal("1.20")
    ) / 1000000
    if reservation > maximum:
        raise ValueError("full_capacity_reservation_exceeds_budget")
    job = {
        "id": "peer_research",
        "kind": "peer_research",
        "dependencies": [],
        "max_tokens": request["max_tokens"],
        "reserved_usd_peak": str(reservation),
    }
    plan.update(job=job, reserved_usd_peak=str(reservation))
    transport.save(output / "request.json", raw)
    transport.save(output / "plan.json", plan)
    transport.save(
        output / "pricing.md",
        "# 冻结计价依据\n\n"
        + "2026-10-24公开价格核对：deepseek-flash周末为非峰时；缓存命中/未命中输入/输出每百万tokens分别US$0.003/0.15/0.60。峰时未命中输入/输出US$0.30/1.20。\n\n"
        + plan["price_source"]
        + "\n\nthinking已含completion，不重复计费。无usage时费用未知，保留全额本次请求预算；不查询账户扣费。\n",
    )
    files = {**protected, **previous["source_hashes"]}
    for base in (source, output):
        files.update(
            {str(p.resolve()): transport.sha(p) for p in base.rglob("*") if p.is_file()}
        )
    for path in (
        Path(__file__),
        repo / "scripts/run_company_summary.py",
        repo / "src/__init__.py",
        repo / "src/fundamental_data/handoff.py",
        repo / "tests/test_peer_research_execution.py",
        repo / "docs/development/peer_research_paid_20261024.md",
    ):
        files[str(path.resolve())] = transport.sha(path)
    transport.save(
        output / "freeze.json",
        {
            "files": files,
            "maximum_calls": 1,
            "automatic_retries": 0,
            "created_at": transport.now(),
            "python": sys.version,
            "dependencies": {
                n: importlib.metadata.version(n)
                for n in (
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
    transport.save(output / "freeze.sha256", transport.sha(output / "freeze.json"))
    return plan


def execute(repo: Path, folder: Path, post=None, key=None) -> dict:
    if transport.sha(folder / "freeze.json") != (folder / "freeze.sha256").read_text():
        raise ValueError("freeze_manifest_changed")
    transport.verify(transport.load(folder / "freeze.json")["files"])
    plan = transport.load(folder / "plan.json")
    request = transport.load(folder / "request.json")
    if (
        plan["maximum_calls"] != 1
        or plan["job"]["dependencies"]
        or transport.sha(folder / "request.json") != plan["request_sha256"]
    ):
        raise ValueError("approved_single_request_changed")
    if datetime.now(timezone.utc).date().isoformat() not in plan["pricing_utc_dates"]:
        raise ValueError("outside_frozen_pricing_dates")
    if Decimal(plan["reserved_usd_peak"]) > Decimal(plan["authorized_usd"]):
        raise ValueError("reservation_exceeds_budget")
    if key is None:
        from dotenv import dotenv_values

        key = os.getenv("DEEPSEEK_API_KEY") or dotenv_values(repo / "config/.env").get(
            "DEEPSEEK_API_KEY"
        )
    if not key:
        raise ValueError("missing_api_key")
    transport.save(
        folder / "execution_started.json",
        {"at": transport.now(), "maximum_calls": 1, "automatic_retries": 0},
    )
    receipt = transport.perform(folder, plan["job"], request, plan, key, post=post)
    job = folder / "jobs/peer_research"
    content = (job / "content.md").read_bytes()
    transport.save(folder / "完整分析.md", content)
    transport.save(folder / "完整分析.html", transport.html(content.decode("utf8")))
    usage = receipt.get("usage") if isinstance(receipt.get("usage"), dict) else {}
    detail = usage.get("completion_tokens_details")
    detail = detail if isinstance(detail, dict) else {}
    result = {
        "status": receipt["status"],
        "requests_sent": 1,
        "automatic_retries": 0,
        "finish_reason": receipt.get("finish_reason"),
        "input_tokens_actual": usage.get("prompt_tokens"),
        "output_tokens_actual_including_thinking": usage.get("completion_tokens"),
        "reasoning_tokens_actual": detail.get("reasoning_tokens"),
        "visible_tokens_local": receipt["visible_tokens_local"],
        "cost": receipt["cost"],
        "authorized_usd": plan["authorized_usd"],
        "unknown_usage_reservation_usd": plan["reserved_usd_peak"]
        if receipt["cost"]["usd"] is None
        else "0",
        "request_bytes_match_approved": transport.sha(job / "request.json")
        == plan["request_sha256"],
        "analysis_bytes_match_response": (folder / "完整分析.md").read_bytes()
        == content,
    }
    transport.verify(transport.load(folder / "freeze.json")["files"])
    result["frozen_files_unchanged"] = True
    transport.save(folder / "执行结果.json", result)
    report = (
        "# 第三阶段执行记录\n\n一次请求，无重试或自动追加；正文是完整原始可见回答，未修补、未裁尾。\n\n"
        + "[完整分析](完整分析.html) · [Markdown](完整分析.md)\n\n"
        + f"状态：{result['status']}；finish_reason：{result['finish_reason']}。\n\n"
        + f"API输入：{result['input_tokens_actual']}；输出（含thinking）：{result['output_tokens_actual_including_thinking']}；其中reasoning：{result['reasoning_tokens_actual']}；可见正文按本地tokenizer：{result['visible_tokens_local']}。\n\n"
        + f"usage计价USD：{receipt['cost']['usd']}；计价性质：{receipt['cost']['kind']}；未知usage预留：{result['unknown_usage_reservation_usd']}；授权上限：{plan['authorized_usd']}。\n\n"
        + "按冻结的官方周末单价计算，completion已含thinking，不重复计费；这是返回usage计价，不是服务商账户扣费查询，也不含Codex会话费用。缺usage不按0处理。\n\n"
        + f"计价来源：{plan['price_source']}\n\n"
        + "调用前冻结文件复查通过；原请求字节一致、完整正文原字节保留。完成状态不证明财务事实、同行判断或投资分析正确。\n"
    )
    transport.save(folder / "执行记录.md", report)
    transport.save(folder / "执行记录.html", transport.html(report))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--budget-limit-usd", type=Decimal, required=True)
    p = sub.add_parser("execute")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--paid", action="store_true", required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        plan = prepare(
            REPO, args.source.resolve(), args.output.resolve(), args.budget_limit_usd
        )
        print(
            json.dumps(
                {
                    k: plan[k]
                    for k in (
                        "maximum_calls",
                        "input_tokens_estimate",
                        "reserved_usd_peak",
                        "authorized_usd",
                    )
                },
                ensure_ascii=False,
            )
        )
        return 0
    result = execute(REPO, args.source.resolve())
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
