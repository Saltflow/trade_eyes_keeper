"""Deterministic three-round handoff, per-request freezing and final presentation."""

import base64
import json
import re

from tokenizers import Tokenizer

from scripts import research_render as presentation
from scripts import run_company_summary as io


def images(folder, plan, index):
    if index <= 0:
        return {}
    previous = plan["jobs"][index - 1]["id"]
    notes = io.load(folder / "views" / previous / "呈现注记.json")
    failed = {f"chart_{issue['chart']}.png" for issue in notes["issues"]}
    return {
        f"assets/{previous}_{p.name}": p
        for p in sorted((folder / "views" / previous / "assets").glob("*.png"))
        if p.name not in failed
    }


def request(folder, plan, index):
    job = plan["jobs"][index]
    if index == 0:
        value = io.load(folder / "request.json")
        protected = {}
    else:
        previous = plan["jobs"][index - 1]["id"]
        raw = folder / "jobs" / previous / "content.md"
        notes = folder / "views" / previous / "呈现注记.json"
        picture_files = images(folder, plan, index)
        dependencies = [
            raw,
            notes,
            folder / "jobs" / previous / "receipt.json",
            *picture_files.values(),
        ]
        protected = {str(p.resolve()): io.sha(p) for p in dependencies}
        text = (folder / "模型输入.md").read_bytes().decode("utf8")
        text += "\n\n# 上一轮原始稿件（待本轮审查）\n\n" + raw.read_bytes().decode(
            "utf8"
        )
        text += "\n\n# 绘图注记\n" + notes.read_text(encoding="utf8")
        text += "\n\n# 本轮实际图片清单（最终插图须使用这些地址）\n" + "\n".join(
            picture_files
        )
        if not picture_files:
            text += "\n没有实际生成的图片；不能声称完成视觉审查。"
        parts = [{"type": "text", "text": text}]
        for label, path in picture_files.items():
            parts.append({"type": "text", "text": "图片：" + label})
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(path.read_bytes()).decode("ascii"),
                        "detail": "original",
                    },
                }
            )
        system = (
            (folder / "共通约定.md").read_text(encoding="utf8")
            + "\n\n"
            + (folder / "prompts" / f"{job['id']}.md").read_text(encoding="utf8")
        )
        value = dict(
            plan["config"]["request"],
            max_tokens=job["max_tokens"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": parts},
            ],
        )
        io.verify(protected)
    count = io.estimate(value, Tokenizer.from_file(plan["tokenizer"]), plan["config"])
    if count > plan["input_waterline"]:
        raise ValueError("input_over_waterline_no_truncation")
    encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf8")
    if len(encoded) > plan["config"]["maximum_request_bytes"]:
        raise ValueError("request_exceeds_provider_body_limit_no_truncation")
    target = folder / "requests" / f"{job['id']}.json"
    io.save(target, encoded)
    protected[str(target.resolve())] = io.sha(target)
    record = {
        "files": protected,
        "root_freeze_sha256": io.sha(folder / "freeze.json"),
        "input_tokens_estimate": count,
        "image_count": len(images(folder, plan, index)),
    }
    frozen = target.with_suffix(".freeze.json")
    io.save(frozen, record)
    io.save(frozen.with_suffix(".sha256"), io.sha(frozen))
    return value, record


def present_round(folder, job, receipt):
    path = folder / "views" / job["id"]
    path.mkdir(parents=True)
    raw = (folder / "jobs" / job["id"] / "content.md").read_bytes().decode("utf8")
    return presentation.render(path, raw, receipt["status"])


def final(folder, plan, index, status):
    raw = (
        (folder / "jobs" / plan["jobs"][index]["id"] / "content.md").read_bytes()
        if index >= 0
        else b""
    )
    io.save(folder / "正文.md", raw)
    body = raw.decode("utf8")
    fallback = status != "complete" and 0 <= index < 2
    pictures = images(folder, plan, index + 1 if fallback else index)
    if fallback:
        name = plan["jobs"][index]["id"]
        body = (folder / "views" / name / "最终报告.md").read_bytes().decode("utf8")
        for label, path in pictures.items():
            body = body.replace("assets/" + path.name, label)
    for name, path in pictures.items():
        io.save(folder / name, path.read_bytes())
    if index > 0 or fallback:
        previous = plan["jobs"][index if fallback else index - 1]["id"]
        io.save(
            folder / "图表数据.yaml",
            (folder / "views" / previous / "图表数据.yaml").read_bytes(),
        )
    references = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", body)
    missing = [p for p in references if p not in pictures]
    new_charts = len(presentation.CHART.findall(body))
    notes = {
        "status": status,
        "available_images": list(pictures),
        "missing_image_references": missing,
        "unreviewed_chart_blocks": new_charts,
        "intermediate_draft_delivery": fallback,
    }
    if status != "complete" or missing or new_charts:
        body = (
            "> 呈现注记："
            + json.dumps(notes, ensure_ascii=False)
            + "。已有原文保留；新增图表块不自动绘制。\n\n"
            + body
        )
    io.save(folder / "最终报告.md", body)
    io.save(folder / "最终报告.html", presentation.html(body))
    io.save(folder / "呈现注记.json", notes)
    return notes
