"""Prepare bounded summary inputs and examples offline; no API execution path."""

import argparse
import hashlib
import json
import socket
from pathlib import Path

from tokenizers import Tokenizer


def load(path):
    return json.loads(path.read_text(encoding="utf8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, indent=2)
    )
    with path.open("xb") as stream:
        stream.write(data.encode("utf8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(paths):
    files = set()
    for path in paths:
        files.update(
            p for p in path.rglob("*") if p.is_file()
        ) if path.is_dir() else files.add(path)
    return {
        str(p.resolve()): sha(p) for p in sorted(files) if "__pycache__" not in p.parts
    }


def questions(config):
    return "\n".join(f"## Q{i} {q}" for i, q in enumerate(config["questions"], 1))


def frozen_material(source, record, failures):
    """Read the frozen metadata/PDF/task layout; never repair model JSON."""
    key = f"{record['code']}_{record['year']}"
    blocks = source / "stage1" / key / "blocks"
    request_path = blocks / failures[0]["block"] / "request.json"
    parts = load(request_path)["messages"][1]["content"]
    texts = [part["text"] for part in parts if part["type"] == "text"]
    if len(texts) != 3 or not texts[-1].startswith("[block:"):
        raise ValueError(f"unsupported_frozen_request_layout: {request_path}")
    pages = texts[1].split("\n\n")
    images = [part for part in parts if part["type"] == "image_url"]
    thin = [i for i, page in enumerate(pages, 1) if len(page.strip()) < 80]
    if len(pages) != record["pages"] or len(thin) != len(images):
        raise ValueError(f"frozen_pdf_page_mapping_unknown: {key}")
    images = [
        {"label": f"{key}／PDF物理页 {page}／冻结图片", "part": image}
        for page, image in zip(thin, images)
    ]
    text = f"# 冻结原年报全文：{key}，PDF共{len(pages)}页\n\n"
    text += "\n\n".join(f"[PDF物理页 {i}]\n{p}" for i, p in enumerate(pages, 1))
    ledger = []
    for item in failures:
        block_dir = blocks / item["block"]
        raw = block_dir / "content.txt"
        receipt = load(block_dir / "receipt.json")
        row = {
            **item,
            "error_type": receipt.get("error_type"),
            "request_sha256": sha(block_dir / "request.json"),
        }
        text += f"\n\n## 原失败记录：{key}/{item['block']}\n\n原异常类型：{receipt.get('error_type')}；HTTP状态：{receipt.get('http_status')}；结束原因：{receipt.get('finish_reason')}。这是旧执行记录，不是原文披露状态。"
        if raw.exists():
            row.update(raw_sha256=sha(raw), raw_bytes=raw.stat().st_size)
            text += (
                f"\n\n# 旧模型raw：{key}/{item['block']}，不是原年报\n\n"
                + raw.read_bytes().decode("utf8")
            )
        else:
            text += f"\n\n# 旧执行缺口：{key}/{item['block']}\n\n无模型正文；不能解释为年报未披露。"
        ledger.append(row)
    return (
        text,
        images,
        {
            "source": key,
            "pdf_sha256": record["sha256"],
            "request": str(request_path),
            "pdf_pages": len(pages),
            "image_pages": thin,
            "failures": ledger,
        },
    )


def example(source, records, code, config, template):
    """Copy selected frozen rows for illustration only; no alignment or inference."""
    record = max(records, key=lambda r: r["year"])
    key = f"{code}_{record['year']}"
    data = load(source / "stage1" / key / "data.json")
    lines = [
        f"# {record['company']}：离线排版样例",
        "这不是模型实际摘要、不是完整财报面板，也不是核验结论。以下仅复制最新输入年度的原值/原缺失状态，不换算、不对齐比较期。",
        "",
    ]
    for table in data["tables"]:
        if table.get("scope", {}).get("kind") != "group":
            continue
        rows = [
            r
            for r in table.get("rows", [])
            if r.get("identity_code") in config["example_metric_codes"]
        ]
        if not rows:
            continue
        columns = table["columns"]
        labels = []
        for column in columns.values():
            p = column.get("period") or {}
            adjustment = column.get("adjustment") or {}
            labels.append(
                f"{column.get('label')} [{p.get('start')}—{p.get('end')}; {p.get('kind')}; 调整={adjustment.get('status')} {adjustment.get('label') or ''}]"
            )
        lines.extend(
            [
                f"## 真实原值片段：{key}／{table['title']}",
                f"主体：{table['scope'].get('entity')}；会计基础：{table.get('accounting_basis')}。原报告期和原列版本见表头，None表示原字段空值。",
                "",
                "| 项目（单位／币种／统计方法） | " + " | ".join(labels) + " |",
                "|---|" + "---|" * len(labels),
            ]
        )
        for row in rows:
            label = "／".join(row.get("path", []))
            scope = f"{row.get('unit')}／{row.get('currency')}／{row.get('statistic', {}).get('kind')}"
            if row.get("period_overrides"):
                scope += f"／原期间例外={row['period_overrides']}"
            values = []
            for column in columns:
                value = row.get("values", {}).get(column)
                reason = row.get("missing_reasons", {}).get(column)
                values.append(
                    str(value) if value is not None else f"原提取缺失：{reason}"
                )
            lines.append(f"| {label}（{scope}） | " + " | ".join(values) + " |")
        lines.append("")
    lines.extend(
        [
            "---",
            "以下七问仅示范排版，方括号内容待模型实际回答，不构造研究结论。",
            template,
        ]
    )
    return "\n".join(lines)


def prepare(source, stage2, output, repo):
    source, stage2, output, repo = (p.resolve() for p in (source, stage2, output, repo))
    if output.exists() or any(
        output.is_relative_to(p) or p.is_relative_to(output) for p in (source, stage2)
    ):
        raise ValueError("output_must_be_new_and_separate")
    config_path = repo / "config/company_summary_contract.json"
    config = load(config_path)
    prompts = {
        k: (repo / v).read_text(encoding="utf8") for k, v in config["prompts"].items()
    }
    protected = [
        source,
        stage2,
        repo / "src/fundamental_data",
        repo / "config/fundamental_data_v2.json",
        repo / "scripts/build_fundamental_data_v2.py",
        repo / "scripts/build_financial_context.py",
        repo / "tests/fixtures/fundamental_data_v2_contract",
    ]
    before = snapshot(protected)
    write(output / "冻结输入哈希.json", before)
    tokenizer_path = repo / "data/fundamental_quant/tokenizer/tokenizer.json"
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    count = lambda text: len(tokenizer.encode(text).ids)
    manifest, release = load(source / "sources.json"), load(stage2 / "summary.json")
    jobs, samples = [], []
    for company in release["companies"]:
        code = company["company_code"]
        records = [r for r in manifest["records"] if str(r["code"]) == code]
        header = (
            f"# 本次材料：{records[0]['company']}（{code}）\n实际年报窗口：{min(r['year'] for r in records)}—{max(r['year'] for r in records)}；现价快照/其他公司资料：未提供。\n\n"
            + questions(config)
        )
        units, failure_details = [], []
        for record in records:
            key = f"{code}_{record['year']}"
            failures = [f for f in company["failures"] if f["source"] == key]
            if failures:
                text, images, info = frozen_material(source, record, failures)
                units.append((text, images, info))
                failure_details.extend(info["failures"])
        groups, pending = [], []
        for unit in units:
            candidate = pending + [unit]
            text = header + "\n\n" + "\n\n".join(u[0] for u in candidate)
            total = (
                count(prompts["recovery"])
                + count(text)
                + sum(len(u[1]) for u in candidate) * config["image_token_reserve_each"]
                + config["message_token_reserve"]
            )
            if total > config["input_waterline"] and pending:
                groups.append(pending)
                pending = [unit]
            else:
                pending = candidate
        if pending:
            groups.append(pending)
        dependencies = []
        for index, group in enumerate(groups, 1):
            job_id = f"{code}_recovery_{index}"
            dependencies.append(job_id)
            text = header + "\n\n" + "\n\n".join(u[0] for u in group)
            images = [part for u in group for part in u[1]]
            jobs.append(
                save_job(
                    output,
                    job_id,
                    "recovery",
                    text,
                    images,
                    prompts["recovery"],
                    [u[2] for u in group],
                    [],
                    config,
                    count,
                )
            )
        body = (stage2 / code / "模型正文.md").read_text(encoding="utf8")
        text = (
            header
            + "\n\n# 排版范例（不是事实）\n\n"
            + prompts["example"]
            + "\n\n# 冻结第二阶段完整正文\n\n"
            + body
        )
        for dependency in dependencies:
            text += f"\n\n# 本轮恢复附录：{dependency}\n\n恢复执行状态：{{{{RECOVERY_STATUS:{dependency}}}}}\n\n{{{{RECOVERY_CONTENT:{dependency}}}}}"
        text += "\n\n# 原失败清单（逐项交代恢复采用与限制）\n\n" + "\n".join(
            f"- {f['source']}/{f['block']}：原异常类型={f['error_type']}；{'有raw候选' if f['raw_available'] else '无模型正文，执行缺口'}"
            for f in failure_details
        )
        jobs.append(
            save_job(
                output,
                f"{code}_summary",
                "summary",
                text,
                [],
                prompts["summary"],
                [],
                dependencies,
                config,
                count,
            )
        )
        sample = example(source, records, code, config, prompts["example"])
        write(output / code / "排版样例.md", sample)
        samples.append(
            {
                "code": code,
                "example_tokens": count(sample),
                "body_tokens": count(body),
                "failed_blocks": len(company["failures"]),
                "recovery_calls": len(dependencies),
            }
        )
    report = {
        "status": "offline_preparation_only",
        "paid_api_calls": 0,
        "tokenizer_sha256": sha(tokenizer_path),
        "config_sha256": sha(config_path),
        "companies": samples,
        "planned_calls": len(jobs),
        "jobs": jobs,
    }
    total_input = sum(j["input_token_bound"] for j in jobs)
    total_output = sum(j["max_tokens"] for j in jobs)
    report.update(input_token_bound=total_input, output_token_bound=total_output)
    report["usd_bounds"] = {
        name: round(
            (total_input * price["input"] + total_output * price["output"]) / 1000000, 6
        )
        for name, price in config["usd_per_million_cache_miss"].items()
    }
    after = snapshot(protected)
    report["protected_files"] = len(before)
    report["changed_protected_files"] = [
        p for p in set(before) | set(after) if before.get(p) != after.get(p)
    ]
    report["over_waterline_jobs"] = [
        j["id"] for j in jobs if j["input_token_bound"] > config["input_waterline"]
    ]
    report["ready_for_paid_execution"] = False
    write(output / "调用计划.json", report)
    rows = [
        "# 离线调用预算与准备状态",
        "实际付费API调用：0。本目录仅为准备，不是摘要成功产物。恢复结果尚不存在；summary输入含显式变量，不能直接发送。",
        "",
        "| 调用 | 输入文本估计 | 图片上界 | 恢复结果预留 | 输入总上界 | 输出额度（含thinking） |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    rows.extend(
        f"| {j['id']} | {j['text_tokens']} | {j['image_token_bound']} | {j['dependency_token_reserve']} | {j['input_token_bound']} | {j['max_tokens']} |"
        for j in jobs
    )
    rows.extend(
        [
            "",
            f"合计计划{len(jobs)}次（不自动追加）；输入规划上界{total_input:,}，输出额度合计{total_output:,}。",
            f"未缓存全额、输出额度全用尽：非峰时US${report['usd_bounds']['off_peak']:.3f}；峰时US${report['usd_bounds']['peak']:.3f}。这不是本轮授权或真实账单。",
            "价格核对2026-10-24：https://api-docs.deepseek.com/quick_start/pricing/ ；图片每张1024 tokens上界：https://api-docs.deepseek.com/guides/vision/ 。本地tokenizer估计非服务商准确账单，每请求另留4096 framing余量，汇率不参与。",
            "",
            "summary变量绑定规则：只拼接对应已计划recovery的完整可见content，不解析JSON、不选择性过滤、不传reasoning_content；无content则填执行缺口并保留失败记录。有partial则完整带入并注明中断。禁止把预算预留的tokens当实际恢复长度。",
            "发送前按真正绑定后的全部消息重新计数/计算预算并冻结；不自动调小、不裁尾、不删材料。超50万则停止发送并显式重规划，旧/局部产物保留。6万摘要目标超限或局部格式错误只标注，不丢数据、不自动续写。",
            f"受保护文件{len(before)}，变化{len(report['changed_protected_files'])}；超水位计划{report['over_waterline_jobs']}。实际模型质量、恢复率、摘要保真度及实际压缩率均未验收。",
        ]
    )
    write(output / "预算与验收.md", "\n".join(rows))
    return report


def save_job(
    output, job_id, kind, text, images, system, sources, dependencies, config, count
):
    folder = output / job_id
    content = [{"type": "text", "text": text}]
    for image in images:
        content.extend([{"type": "text", "text": image["label"]}, image["part"]])
    request = {
        "model": config["model"],
        "thinking": config["thinking"],
        "reasoning_effort": config["reasoning_effort"],
        "max_tokens": config[f"{kind}_max_tokens"],
        "response_format": {"type": "text"},
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    }
    write(folder / "模型输入.md", text)
    write(folder / "request.template.json", request)
    text_tokens = count(system) + sum(
        count(p["text"]) for p in content if p["type"] == "text"
    )
    reserve = len(dependencies) * config["recovery_max_tokens"]
    item = {
        "id": job_id,
        "kind": kind,
        "text_tokens": text_tokens,
        "image_count": len(images),
        "image_token_bound": len(images) * config["image_token_reserve_each"],
        "dependency_token_reserve": reserve,
        "max_tokens": request["max_tokens"],
        "dependencies": dependencies,
        "sources": sources,
        "request_bytes": (folder / "request.template.json").stat().st_size,
        "request_sha256": sha(folder / "request.template.json"),
    }
    item["input_token_bound"] = (
        text_tokens
        + item["image_token_bound"]
        + reserve
        + config["message_token_reserve"]
    )
    write(folder / "plan.json", item)
    return item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--stage2", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("offline_only")
    )
    report = prepare(
        args.source, args.stage2, args.output, Path(__file__).resolve().parents[1]
    )
    print(
        json.dumps({k: v for k, v in report.items() if k != "jobs"}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
