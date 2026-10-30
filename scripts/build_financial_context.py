"""Build stage-two model text from an existing batch; strictly offline, new output only."""

import argparse
import hashlib
import json
import socket
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import bleach
import markdown
from tokenizers import Tokenizer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.fundamental_data.model_context import assemble, audit, reading


def load(path):
    return json.loads(path.read_text(encoding="utf8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(
            value.encode("utf8")
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, indent=2).encode("utf8")
        )


def preview(text):
    body = bleach.clean(
        markdown.markdown(text, extensions=["tables", "fenced_code"]),
        tags=[
            "h1",
            "h2",
            "h3",
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
            "a",
        ],
        attributes={"a": ["href"]},
        strip=True,
    )
    return (
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<style>body{font:15px/1.65 "Microsoft YaHei",sans-serif;max-width:1400px;margin:24px auto;padding:0 20px;color:#233041}table{border-collapse:collapse;display:block;overflow:auto;margin:16px 0}td,th{border:1px solid #ddd;padding:6px 10px;min-width:110px}th{background:#edf2f8}td:first-child{min-width:220px}h3{margin-top:32px}pre{white-space:pre-wrap}a{color:#065ca3}</style>'
        + body
    )


def denied(*args, **kwargs):
    raise RuntimeError("stage_two_network_forbidden")


def build(source_run, output, repo, baseline=None):
    output = output.resolve()
    source_run = source_run.resolve()
    if output.is_relative_to(source_run) or source_run.is_relative_to(output):
        raise ValueError("output_must_be_separate_from_source_run")
    baseline = baseline.resolve() if baseline else None
    if baseline and (
        output.is_relative_to(baseline) or baseline.is_relative_to(output)
    ):
        raise ValueError("output_must_be_separate_from_baseline")
    if output.exists() and any(
        p.name != "before_hashes.json" for p in output.iterdir()
    ):
        raise FileExistsError("output_must_be_new")
    output.mkdir(parents=True, exist_ok=True)
    labels = load(repo / "config/fundamental_data_v2.json")["labels"]
    tokenizer = Tokenizer.from_file(
        str(repo / "data/fundamental_quant/tokenizer/tokenizer.json")
    )
    tokens = lambda text: len(tokenizer.encode(text).ids)
    manifest = load(source_run / "sources.json")
    companies = {}
    for item in manifest["records"]:
        source = str(item["code"]) + "_" + str(item["year"])
        path = source_run / "stage1" / source / "data.json"
        companies.setdefault(str(item["code"]), []).append((source, load(path)))
    reports, combined, raw_parts = [], [], []
    for code, entries in companies.items():
        entries.sort(key=lambda entry: entry[0])
        folder = output / code
        folder.mkdir(exist_ok=False)
        plan = assemble(entries)
        text, cells, narrative = reading(entries, plan, labels)
        retention = audit(entries, plan, cells, text)
        write(folder / "模型正文.md", text)
        write(folder / "阅读.html", preview(text))
        write(
            folder / "正文位置.json",
            {"cells": cells, "definitions": plan["definitions"]},
        )
        write(
            folder / "数据保留.json",
            {
                **retention,
                "source_map": [
                    {
                        "source": m["source"],
                        "target": [
                            m["target"][0],
                            m["target"][1],
                            list(plan["sections"][m["target"][0]]["columns"]).index(
                                m["target"][2]
                            ),
                            m["target"][3],
                        ],
                    }
                    for m in plan["mappings"]
                ],
            },
        )
        sections = [
            {
                "section": i,
                "title": s["table"].get("title")
                if isinstance(s["table"], dict)
                else None,
                "role": s["table"].get("role")
                if isinstance(s["table"], dict)
                else None,
                "aligned": s["aligned"],
                "reason": s["reason"],
                "sources": [[source, ti] for source, ti, _ in s["inputs"]],
                "rows": len(s["rows"]),
                "columns": len(s["columns"]),
                "cross_year_rows": sum(
                    len(
                        {
                            o[0]
                            for variants in r.get("cells", {}).values()
                            for v in variants
                            for o in v["origins"]
                        }
                    )
                    > 1
                    for r in s["rows"]
                ),
                "conflict_cells": sum(
                    len(v) > 1 for r in s["rows"] for v in r.get("cells", {}).values()
                ),
            }
            for i, s in enumerate(plan["sections"])
        ]
        write(folder / "对齐目录.json", sections)
        failures = []
        company_raw = []
        for source, record in entries:
            for block, content in record.get("unstructured_blocks", {}).items():
                if not isinstance(content, dict) or content.get("run_status") not in {
                    "failed",
                    "truncated",
                }:
                    continue
                raw = source_run / "stage1" / source / "blocks" / block / "content.txt"
                item = {
                    "source": source,
                    "block": block,
                    "status": content["run_status"],
                    "raw_available": raw.is_file(),
                }
                if raw.is_file():
                    value = raw.read_bytes()
                    name = source + "_" + block + ".txt"
                    with (folder / name).open("xb") as stream:
                        stream.write(value)
                    body = value.decode("utf8")
                    item.update(
                        file=name,
                        tokens=tokens(body),
                        sha256=hashlib.sha256(value).hexdigest(),
                    )
                    company_raw.append(
                        "\n===== "
                        + source
                        + " / "
                        + block
                        + " 原始未解析返回 =====\n"
                        + body
                    )
                failures.append(item)
        raw_text = "\n".join(company_raw)
        write(folder / "未解析附件.txt", raw_text)
        old_folder = source_run / "stage2" / code
        old_reading = old_folder / "阅读.md"
        old_data = (
            load(old_folder / "data.json")
            if (old_folder / "data.json").is_file()
            else None
        )
        before = (
            old_reading.read_text(encoding="utf8")
            if old_reading.is_file()
            else "\n".join(
                (source_run / "stage1" / source / "阅读.md").read_text(encoding="utf8")
                for source, _ in entries
            )
        )
        old_tables = old_data.get("tables", []) if old_data else []
        previous = load(baseline / code / "summary.json") if baseline else None
        if baseline:
            before = (baseline / code / "模型正文.md").read_text(encoding="utf8")
        old_cross_year_rows = sum(
            len(
                {
                    version.get("report_id")
                    for entry in row.get("source_fields", [])
                    for version in entry.get("reported_in", [])
                    if isinstance(version, dict) and version.get("report_id")
                }
            )
            > 1
            for table in old_tables
            if isinstance(table, dict)
            for row in table.get("rows", [])
            if isinstance(row, dict)
        )
        grids = [line for line in text.splitlines() if line.startswith("|---")]
        report = {
            "company_code": code,
            "inputs": [s for s, _ in entries],
            "baseline": "上一版第二阶段模型正文"
            if baseline
            else "原第二阶段正文"
            if old_data
            else "原第二阶段失败；三份第一阶段正文拼接作参考",
            "before_tokens": tokens(before),
            "after_tokens": tokens(text),
            "first_stage_reading_tokens": sum(
                tokens(
                    (source_run / "stage1" / s / "阅读.md").read_text(encoding="utf8")
                )
                for s, _ in entries
            ),
            "raw_attachment_tokens": tokens(raw_text),
            "full_input_tokens": tokens(text + "\n" + raw_text),
            "failures": failures,
            "retention": retention,
            "narrative": narrative,
            "input_tables": sum(len(d.get("tables", [])) for _, d in entries),
            "output_sections": len(sections),
            "cross_year_groups": sum(
                len({s[0] for s in section["sources"]}) > 1 for section in sections
            ),
            "cross_year_rows": sum(s["cross_year_rows"] for s in sections),
            "independent_sections": sum(
                not s["aligned"] or len({i[0] for i in s["sources"]}) < 2
                for s in sections
            ),
            "before_cross_year_rows": previous["cross_year_rows"]
            if previous
            else old_cross_year_rows
            if old_data
            else None,
            "independent_reasons": dict(
                Counter(s["reason"] for s in sections if s["reason"])
            ),
            "conflict_cells": sum(s["conflict_cells"] for s in sections),
            "before_max_columns": previous["after_max_columns"]
            if previous
            else max(
                (len(t.get("columns", {})) for t in old_tables if isinstance(t, dict)),
                default=None,
            ),
            "after_max_columns": max(
                (line.count("---") - 1 for line in grids), default=0
            ),
            "alignment_unchanged": (sections == load(baseline / code / "对齐目录.json"))
            if baseline
            else None,
            "shared_source_cells": narrative["formatting"]["shared_source_cells"],
            "displayed_positions": retention["displayed_positions"],
            "source_company_unknown": [
                s for s, d in entries if not any((d.get("company") or {}).values())
            ],
        }
        report["reduction_percent"] = round(
            100 * (1 - report["after_tokens"] / report["before_tokens"]), 2
        )
        write(folder / "summary.json", report)
        combined.append("# 公司 " + code + "\n\n" + text)
        raw_parts.append(raw_text)
        reports.append(report)
        print(
            json.dumps(
                {
                    "company": code,
                    "before_tokens": report["before_tokens"],
                    "after_tokens": report["after_tokens"],
                    "input_cells": retention["input_cells"],
                    "passed": retention["passed"],
                }
            ),
            flush=True,
        )
    body = "\n\n".join(combined)
    raw = "\n\n".join(raw_parts)
    write(output / "模型输入.md", body)
    write(output / "完整正文.html", preview(body))
    write(output / "未解析附件.txt", raw)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_calls": 0,
        "network": "blocked",
        "source_run": str(source_run),
        "baseline": str(baseline) if baseline else None,
        "companies": reports,
        "body_tokens": tokens(body),
        "raw_attachment_tokens": tokens(raw),
        "full_input_tokens": tokens(body + "\n" + raw),
        "retention_passed": all(r["retention"]["passed"] for r in reports),
    }
    baseline = output / "before_hashes.json"
    if baseline.is_file():
        frozen = load(baseline)
        summary["protected_files"] = len(frozen["files"])
        summary["changed_files"] = [
            p
            for p, digest in frozen["files"].items()
            if not Path(p).is_file()
            or hashlib.sha256(Path(p).read_bytes()).hexdigest() != digest
        ]
        old_names = {
            str(Path(p).resolve())
            for p in frozen["files"]
            if Path(p).is_relative_to(source_run)
        }
        summary["new_files_in_old_run"] = [
            str(p)
            for p in source_run.rglob("*")
            if p.is_file() and str(p.resolve()) not in old_names
        ]
        if summary["changed_files"] or summary["new_files_in_old_run"]:
            raise ValueError("immutable_input_changed")
    write(output / "summary.json", summary)
    lines = [
        "# 第二阶段离线整理对比",
        "",
        "第一阶段及旧产物不动；新增API调用0。token按同一本地tokenizer计算实际模型正文。",
        "",
        "[完整模型正文](模型输入.md) · [完整阅读预览](完整正文.html)",
        "",
        "| 公司 | 对照口径 | 原正文token | 新正文token | 减少 | 输入单元格 | 保留 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for r in reports:
        lines.append(
            f"| {r['company_code']} | {r['baseline']} | {r['before_tokens']:,} | {r['after_tokens']:,} | {r['reduction_percent']}% | {r['retention']['input_cells']:,} | 全部 |"
        )
    lines += [
        "",
        f"三家公司模型正文：{summary['body_tokens']:,} tokens；未解析附件：{summary['raw_attachment_tokens']:,}；一起输入：{summary['full_input_tokens']:,}。",
        "",
        "未解析附件保持原始返回，可能仍为非法JSON，未修复、未补请求。正文并不含这些附件；连接失败没有正文可恢复。",
        "",
        "保留验收只证明已有单元格的原值、缺失原因和有效列口径未被整理丢弃，不证明原财报提取完整或数值准确。",
        "",
    ]
    for r in reports:
        code = r["company_code"]
        lines += [
            f"## {code}",
            "",
            f"[模型正文]({code}/模型正文.md) · [阅读预览]({code}/阅读.html) · [未解析附件]({code}/未解析附件.txt)",
            "",
            f"原始表{r['input_tables']}张；输出{r['output_sections']}组，其中跨年{r['cross_year_groups']}组、跨年行{r['cross_year_rows']}行；独立保留{r['independent_sections']}组。",
            "",
            f"最大展示数据列（不含项目列）：原{r['before_max_columns']}，新{r['after_max_columns']}。原跨年行{r['before_cross_year_rows']}，新跨年行{r['cross_year_rows']}；对齐目录不变={r['alignment_unchanged']}。同口径共显额外来源单元格{r['shared_source_cells']}，仍有各原始来源映射。",
            "",
            f"政策{r['narrative']['policies']['input']}条→{r['narrative']['policies']['versions']}个精确文本及口径版本；近似改写不做语义去重。",
            "",
        ]
        if r["source_company_unknown"]:
            lines += [
                "原记录公司身份未定、独立保留："
                + "、".join(r["source_company_unknown"]),
                "",
            ]
    write(output / "对比.md", "\n".join(lines))
    write(output / "index.html", preview("\n".join(lines)))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args(argv)
    # Even an accidental future network dependency cannot send a request here.
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    result = build(
        args.source_run, args.output, Path(__file__).resolve().parents[1], args.baseline
    )
    if not result["retention_passed"]:
        raise SystemExit("retention_failed_outputs_preserved")


if __name__ == "__main__":
    main()
