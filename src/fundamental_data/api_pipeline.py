"""Single-command paid data demonstration; frozen before the first HTTP call."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import InvalidOperation
from pathlib import Path

from dotenv import dotenv_values

from .api_client import Client
from .api_format import financial_csv, reading, validate
from .api_io import check_seal, prepare, seal, sha, write_once


def run(source, output, *, paid=False, max_chars=20000):
    if max_chars <= 0:
        raise ValueError("max_chars_must_be_positive")
    repo = Path(__file__).resolve().parents[2]
    protocol = repo / "config/fundamental_data_api.md"
    settings = {
        "model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com/v1/chat/completions",
        "tokenizer": str(repo / "data/fundamental_quant/tokenizer/tokenizer.json"),
        "max_output_tokens": 32768,
        "timeout_seconds": 1800,
        "max_usd": 5.0,
        "workers": 3,
        "automatic_retries": 0,
        "automatic_repairs": 0,
        "max_chars": max_chars,
        "protocol": protocol.read_text(encoding="utf8"),
        "pricing_basis": "2026-10-23 official Flash peak USD/M: miss .3, hit .006, output 1.2",
    }
    manifest, documents = prepare(source, output)
    settings["max_calls"] = (
        len(documents) + len(manifest.get("expected_companies", documents)) + 1
    )
    hashes = seal(source, output, documents, settings)
    key = os.getenv("DEEPSEEK_API_KEY") or dotenv_values(repo / "config/.env").get(
        "DEEPSEEK_API_KEY"
    )
    client = Client(output, settings, hashes, key or "")
    sources = {d["document_id"]: d for d in documents}
    requests = {}
    for doc_id, doc in sources.items():
        content = [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "document_id": doc_id,
                        "expected_identity": {
                            k: doc.get(k)
                            for k in ("issuer_id", "company_name", "period_end")
                        },
                        "pages": doc["pages"],
                    },
                    ensure_ascii=False,
                ),
            }
        ]
        for image in doc["images"]:
            content += [
                {"type": "text", "text": f"原PDF物理页{image['page']}图像"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + image["base64"]},
                },
            ]
        requests[doc_id] = content
    preflight = {
        "pdfs": len(documents),
        "pages": sum(len(d["pages"]) for d in documents),
        "input_tokens_estimate": sum(
            client.meter(client.payload("extract", c)) for c in requests.values()
        ),
        "max_calls": settings["max_calls"],
        "max_usd": settings["max_usd"],
        "paid": paid,
    }
    write_once(output / "preflight.json", preflight)
    if not paid:
        return preflight
    if not key:
        raise ValueError("DEEPSEEK_API_KEY_missing")
    results, checks = {}, {}

    def execute(
        stage,
        name,
        content,
        allowed_sources,
        expected=None,
        records=None,
        upstream=False,
    ):
        data, _receipt = client.call(stage, name, content)
        try:
            issues = (
                ["no_parseable_model_result"]
                if data is None
                else validate(stage, data, allowed_sources, expected)
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
            InvalidOperation,
            RecursionError,
        ) as error:
            issues = ["schema_unusable_output:" + type(error).__name__]
        if upstream:
            issues.append("upstream_data_requires_review")
        if data is not None:
            write_once(output / stage / name / "data.json", data)
            if isinstance(data, dict) and not any(
                i.startswith("schema_") for i in issues
            ):
                text = reading(
                    stage, data, {name: data} if stage == "extract" else records
                )
                if stage == "history" and len(text) > max_chars:
                    issues.append("history_context_exceeds_character_limit")
                write_once(output / stage / name / "candidate.md", text.encode())
        write_once(
            output / stage / name / "check.json",
            {"passed": not issues, "issues": issues},
        )
        return (
            None
            if data is None
            or not isinstance(data, dict)
            or any(i.startswith("schema_") for i in issues)
            else data
        ), issues

    def extract(item):
        doc_id, doc = item
        return doc_id, execute("extract", doc_id, requests[doc_id], {doc_id: doc}, doc)

    with ThreadPoolExecutor(max_workers=settings["workers"]) as pool:
        for doc_id, (data, issues) in pool.map(extract, sources.items()):
            checks[doc_id] = issues
            if data is not None:
                results[doc_id] = data
    issuers = sorted(
        manifest.get("expected_companies") or {r["issuer_id"] for r in results.values()}
    )
    for issuer in issuers:
        records = {k: r for k, r in results.items() if r["issuer_id"] == issuer}
        expected_docs = [
            k for k, d in sources.items() if d.get("issuer_id") == issuer
        ] or list(records)
        payload = {
            "records": records,
            "input_checks": {k: checks[k] for k in records},
            "missing_documents": sorted(set(expected_docs) - set(records)),
        }
        _, checks["history_" + issuer] = execute(
            "history",
            issuer.replace(":", "_"),
            json.dumps(payload, ensure_ascii=False),
            {k: sources[k] for k in expected_docs},
            records=records,
            upstream=bool(
                payload["missing_documents"] or any(checks[k] for k in records)
            ),
        )
    metadata_input = {"companies": issuers, "records": results, "input_checks": checks}
    _, checks["metadata"] = execute(
        "metadata",
        "companies",
        json.dumps(metadata_input, ensure_ascii=False),
        sources,
        issuers,
        upstream=any(checks.values()),
    )
    write_once(output / "financials.csv", financial_csv(results).encode())
    check_seal(hashes)
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls_missing_usage": 0}
    for receipt in client.receipts:
        reported = receipt.get("usage") or {}
        if not isinstance(reported, dict):
            reported = {}
        if all(
            type(reported.get(k)) is int for k in ("prompt_tokens", "completion_tokens")
        ):
            for key in ("prompt_tokens", "completion_tokens"):
                usage[key] += reported[key]
        else:
            usage["calls_missing_usage"] += 1
    usage["known_usage_peak_cost_upper_usd"] = (
        usage["prompt_tokens"] * 0.3 + usage["completion_tokens"] * 1.2
    ) / 1000000
    summary = {
        "status": "checks_passed_semantic_review_pending"
        if not any(checks.values())
        else "needs_review",
        "checks": checks,
        "calls": client.receipts,
        "usage_totals": usage,
        "preflight": preflight,
        "manual_modifications": 0,
        "automatic_retries": 0,
        "automatic_repairs": 0,
    }
    write_once(output / "summary.json", summary)
    rows = [
        "# 三公司三年财报：首次API数据结果",
        "",
        "代码/输入/提示词已冻结；原始响应不修改，不重试、不修复。",
        "",
        f"状态：{summary['status']}；请求{len(client.receipts)}次；提取{len(results)}/{len(documents)}份。",
        f"API已知输入{usage['prompt_tokens']}、输出{usage['completion_tokens']} token；{usage['calls_missing_usage']}次缺usage。按峰时全部输入无缓存估算的已知费用上限${usage['known_usage_peak_cost_upper_usd']:.6f}；不是控制台对账金额。",
        "",
        "[财务CSV](financials.csv) · [完整校验及usage](summary.json) · [冻结清单](freeze.json)",
        "",
    ]
    rows += [
        f"- [{d.get('company_name', k)} {d.get('period_end', '')}](extract/{k}/{'candidate.md' if (output / 'extract' / k / 'candidate.md').exists() else 'receipt.json'})：{checks[k] or '来源/格式检查通过，语义待验收'}"
        for k, d in sources.items()
    ]
    rows += [
        f"- [{issuer} 三年整合](history/{issuer.replace(':', '_')}/candidate.md)"
        for issuer in issuers
    ]
    rows += [
        "- [公司/同行元数据](metadata/companies/candidate.md)",
        "",
        "部分上游检查不通过时，下游仍仅为带问题标记的候选整理，不代表事实已验证。",
        "",
        "## 原始PDF与来源版本",
    ]
    rows += [
        f"- {d.get('company_name', k)} {d.get('period_end', '')}：{len(d['pages'])}页；{k}；[原PDF](<{Path(d['path']).as_posix()}>)"
        for k, d in sources.items()
    ]
    write_once(output / "README.md", "\n".join(rows).encode())
    write_once(
        output / "output_hashes.json",
        {
            str(p.relative_to(output)): sha(p)
            for p in sorted(output.rglob("*"))
            if p.is_file()
        },
    )
    return {
        "status": summary["status"],
        "calls": len(client.receipts),
        "output": str(output.resolve()),
    }
