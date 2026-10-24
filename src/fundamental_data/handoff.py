"""File-only handoff for existing summaries and original peer metadata."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml


def load(path: Path):
    return json.loads(path.read_bytes())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(value, (str, bytes)):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    with path.open("xb") as stream:
        stream.write(value.encode("utf8") if isinstance(value, str) else value)


def company_code(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError(f"invalid_company_identifier: {value}")
    return value


def separate(output: Path, sources: list[Path]) -> None:
    for source in sources:
        base = source.resolve()
        if output == base or output in base.parents or base in output.parents:
            raise ValueError(f"output_must_be_separate_from_input: {base}")
    if output.exists():
        raise FileExistsError(f"output_must_be_new: {output}")


def metadata_records(source: Path, config: dict, companies=()) -> tuple[list, list]:
    """Copy declared annual snapshots, retaining unknown identity and all issues."""
    manifest = source / "sources.json"
    if source.is_dir() and manifest.is_file():
        files = [
            (r["code"], source / "stage1" / f"{r['code']}_{r['year']}" / "data.json", r)
            for r in load(manifest)["records"]
        ]
        protected = [manifest]
    else:
        path = source / "data.json" if source.is_dir() else source
        data = load(path)
        files, protected = [((data.get("company") or {}).get("code"), path, None)], []
    settings = config["peer_input"]
    records = []
    for code, path, source_record in files:
        code = str(code) if code is not None else None
        if companies and code is not None and code not in companies:
            continue
        data = load(path)
        records.append(
            {
                "source": str(path.resolve()),
                "declared_code": code,
                "source_record": source_record,
                "company": data.get("company"),
                "reports": data.get("reports"),
                "report_period": data.get("report_period"),
                "metadata": data.get("metadata", []),
                "structure_tables": [
                    t
                    for t in data.get("tables", [])
                    if t.get("topic") in settings["structure_topics"]
                ],
                "policy_context": [
                    p
                    for p in data.get("policies", [])
                    if p.get("topic") in settings["policy_topics"]
                ],
                "issues": data.get("issues", []),
            }
        )
        protected.append(path)
    return records, protected


def summary_files(source: Path, companies=()) -> list[tuple[str, Path]]:
    if source.is_file():
        if len(companies) != 1:
            raise ValueError("single_summary_file_requires_one_company")
        return [(company_code(companies[0]), source)]
    found = {p.parent.name: p for p in source.glob("*/完整摘要.md")}
    codes = list(companies) if companies else sorted(found)
    if not codes:
        raise ValueError("no_company_summaries_found")
    return [(company_code(code), found[code]) for code in codes]


def stage3_input(
    source: Path, metadata: Path, output: Path, repo: Path, companies=()
) -> dict:
    separate(output, [source, metadata])
    summaries = summary_files(source, companies)
    config_path = repo / "config/fundamental_data_v2.json"
    config = load(config_path)
    records, inputs = metadata_records(metadata, config, [c for c, _ in summaries])
    inputs += [path for _, path in summaries] + [config_path]
    for code, path in summaries:
        receipt = path.parent.parent / "jobs" / f"{code}_summary" / "receipt.json"
        if receipt.is_file():
            inputs.append(receipt)
    hashes = {str(p.resolve()): sha(p) for p in inputs}
    parts, entries = [], []
    for code, path in summaries:
        raw = path.read_bytes()
        target = output / code / "完整摘要.md"
        save(target, raw)
        selected = [r for r in records if r["declared_code"] == code]
        receipt = path.parent.parent / "jobs" / f"{code}_summary" / "receipt.json"
        status = load(receipt) if receipt.is_file() else {"status": "not_provided"}
        save(output / code / "摘要状态.json", status)
        text = yaml.safe_dump(selected, allow_unicode=True, sort_keys=False)
        save(output / code / "元数据.md", text)
        parts.append(
            f"# 公司 {code}\n\n{raw.decode('utf8')}\n\n"
            f"## 原始年度同行元数据与口径\n\n{text}"
        )
        entries.append(
            {
                "company": code,
                "summary": f"{code}/完整摘要.md",
                "summary_status": status.get("status"),
                "metadata_records": len(selected),
            }
        )
    unassigned = [r for r in records if r["declared_code"] is None]
    if unassigned:
        parts.append(
            "# 身份未定元数据：未分配给任何公司\n\n"
            + yaml.safe_dump(unassigned, allow_unicode=True, sort_keys=False)
        )
    save(output / "元数据.json", records)
    save(output / "模型输入.md", "\n\n---\n\n".join(parts))
    result = {
        "kind": "stage3_input_v1",
        "model_calls": 0,
        "purpose": "仅交接输入；同行资格、经济可比性及跨公司分析尚未执行。",
        "selection": {
            key: config["peer_input"][key]
            for key in ("structure_topics", "policy_topics")
        },
        "companies": entries,
        "source_hashes": hashes,
        "files": {
            str(p.relative_to(output)): sha(p) for p in output.rglob("*") if p.is_file()
        },
    }
    if any(sha(Path(p)) != digest for p, digest in hashes.items()):
        raise ValueError("source_changed_during_handoff")
    save(output / "manifest.json", result)
    return result


def read_stage3_input(source: Path) -> dict:
    """The future stage-three consumer needs only this package directory."""
    manifest = load(source / "manifest.json")
    for name, digest in manifest["files"].items():
        path = (source / name).resolve()
        if not path.is_relative_to(source.resolve()) or sha(path) != digest:
            raise ValueError(f"handoff_file_changed: {name}")
    return {
        "manifest": manifest,
        "body": (source / "模型输入.md").read_bytes().decode("utf8"),
        "metadata": load(source / "元数据.json"),
    }
