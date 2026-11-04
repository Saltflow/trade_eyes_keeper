"""Run a frozen summary template through Codex native stdin, retaining provenance."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.model_gateway.transport import load, sha, verify


def now():
    return datetime.now(timezone.utc).isoformat()


def run(source: Path, output: Path, model: str, effort: str) -> dict:
    source, output = source.resolve(), output.resolve()
    if output == source or output in source.parents or source in output.parents:
        raise ValueError("output_must_be_separate_from_source")
    if output.exists():
        raise FileExistsError("output_must_be_new")
    frozen_path = source / "freeze.json"
    if sha(frozen_path) != (source / "freeze.sha256").read_text("utf8"):
        raise ValueError("freeze_manifest_changed")
    frozen, plan = load(frozen_path), load(source / "plan.json")
    verify(frozen["files"])
    if len(plan["jobs"]) != 1 or plan["jobs"][0]["kind"] != "summary":
        raise ValueError("one_frozen_summary_job_required")
    job = plan["jobs"][0]
    if job["input_bound"] > 400000:
        raise ValueError("native_input_exceeds_safe_waterline")
    template = source / "templates" / (job["id"] + ".json")
    request = load(template)
    sections = [
        (
            "执行下列冻结的公司摘要合同。所有材料已经包含在输入中；不要调用工具、"
            "联网、运行代码或委托。按原合同输出 Markdown，完整保留不确定性和缺口。"
            "若输入只是年度片段，请明确片段范围，不得把其他年份当作已提供。"
        )
    ]
    for message in request["messages"]:
        content = message["content"]
        if not isinstance(content, str):
            raise TypeError("text_only_summary_template_required")
        sections.extend(["\n[原始 " + message["role"] + " 合同]\n", content])
    prompt = "\n\n".join(sections).encode("utf8")
    if len(prompt.decode("utf8")) > 1048576:
        raise ValueError("native_input_exceeds_character_limit")
    executable = shutil.which("codex")
    if not executable:
        raise FileNotFoundError("codex_cli_not_found")
    output.mkdir(parents=True)
    (output / "request.template.json").write_bytes(template.read_bytes())
    (output / "native-prompt.txt").write_bytes(prompt)
    command = [
        executable,
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-C",
        str(output),
        "-o",
        str(output / "content.md"),
        "--",
        "-",
    ]
    started = now()
    error = None
    with (output / "native-events.jsonl").open("xb") as events, (
        output / "native-stderr.txt"
    ).open("xb") as stderr:
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                stdout=events,
                stderr=stderr,
                timeout=1800,
                check=False,
            )
            code = completed.returncode
        except subprocess.TimeoutExpired:
            code, error = None, "TimeoutExpired"
    content = output / "content.md"
    if not content.exists():
        content.write_bytes(b"")
    usage = None
    for line in (
        (output / "native-events.jsonl")
        .read_text("utf8", errors="replace")
        .splitlines()
    ):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
    integrity = True
    try:
        verify(frozen["files"])
    except ValueError:
        integrity = False
    result = {
        "job": job["id"],
        "model": model,
        "reasoning_effort": effort,
        "started_at": started,
        "finished_at": now(),
        "returncode": code,
        "status": "received"
        if code == 0 and content.stat().st_size and integrity
        else "failed",
        "error_type": error,
        "usage": usage,
        "automatic_retries": 0,
        "external_paid_api_calls": 0,
        "native_output_limit": "Codex CLI defaults",
        "source_freeze": str(frozen_path),
        "source_freeze_sha256": sha(frozen_path),
        "template_sha256": sha(template),
        "native_prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "runner_sha256": sha(Path(__file__).resolve()),
        "content_sha256": sha(content),
        "source_integrity_unchanged": integrity,
        "scope_note": "Native model differs from DeepSeek; stream completion does not certify factual completeness.",
    }
    (output / "receipt.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6.1-terra")
    parser.add_argument("--effort", default="high")
    args = parser.parse_args()
    result = run(args.source, args.output, args.model, args.effort)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "received" else 2


if __name__ == "__main__":
    raise SystemExit(main())
