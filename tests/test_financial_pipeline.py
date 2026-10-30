"""Offline acceptance of command routing and the summary/metadata handoff."""

import json
import socket
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import financial_pipeline as cli
from scripts import run_company_summary as runner
from src.fundamental_data.handoff import (
    load,
    read_stage3_input,
    save,
    sha,
    stage3_input,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Unexpected network call")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def materials(tmp_path):
    source, context, summaries = [
        tmp_path / s for s in ("source", "context", "summaries")
    ]
    save(
        source / "sources.json",
        {
            "records": [
                {"code": "600039", "year": 2023},
                {"code": "600820", "year": 2024},
            ]
        },
    )
    for code, year in (("600039", 2023), ("600820", 2024)):
        save(
            source / "stage1" / f"{code}_{year}" / "data.json",
            {
                "company": {"code": code},
                "report_period": f"FY{year}",
                "reports": [],
                "metadata": [{"businesses": ["original text"], "unit": "人民币百万元"}],
                "tables": [
                    {
                        "topic": "segments_geographies",
                        "scope": "group",
                        "rows": [{"value": "5–8%", "missing": None}],
                    },
                    {"topic": "not_peer_structure", "rows": [{"value": 123}]},
                ],
                "policies": [{"topic": "reporting_basis", "text": "unchanged"}],
                "issues": ["context_uncertain"],
            },
        )
        save(
            context / code / "模型正文.md",
            f"# {code}\n原数及口径；{{{{RECOVERY_CONTENT:opaque}}}}",
        )
        save(
            summaries / code / "完整摘要.md",
            f"# {code}\r\n局部摘要、保留缺口。".encode(),
        )
        save(
            summaries / "jobs" / f"{code}_summary" / "receipt.json",
            {"status": "partial", "finish_reason": "length", "usage": None},
        )
    return source, context, summaries


def test_extract_delegates_parameters_without_calling_model(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kw: calls.append((args, kw)) or SimpleNamespace(returncode=2),
    )
    assert (
        cli.main(
            [
                "extract",
                "--source",
                str(tmp_path / "report.pdf"),
                "--output",
                str(tmp_path / "out"),
                "--company",
                "600039",
                "--year",
                "2025",
                "--profile",
                "construction",
            ]
        )
        == 2
    )
    args = calls[0][0]
    assert "--paid" not in args
    assert args[args.index("--year") + 1] == "2025"
    assert args[args.index("--profile") + 1] == "construction"
    assert args[4].endswith("build_fundamental_data_v2.py")


@pytest.mark.parametrize("budget", ["0", "-1", "NaN", "Infinity", "bad"])
def test_invalid_budget_rejected(budget):
    with pytest.raises(SystemExit):
        cli.main(
            [
                "summary-prepare",
                "--source",
                "unused",
                "--metadata",
                "unused",
                "--output",
                "unused",
                "--budget-limit-usd",
                budget,
            ]
        )


def test_context_delegates_existing_implementation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0),
    )
    cli.main(
        [
            "context",
            "--source",
            str(tmp_path / "source"),
            "--output",
            str(tmp_path / "new"),
            "--baseline",
            str(tmp_path / "old"),
        ]
    )
    assert calls[0][4].endswith("build_financial_context.py")
    assert "--source-run" in calls[0] and "--baseline" in calls[0]


def test_summary_plan_is_best_effort_and_frozen(materials, tmp_path):
    source, context, _ = materials
    out = tmp_path / "prepared"
    result = cli.prepare_summary(context, out, source, cli.REPO, Decimal(2))
    plan = load(out / "plan.json")
    assert result["model_calls"] == 0 and result["planned_calls"] == 2
    assert plan["max_parallel"] == 1
    assert all(j["kind"] == "summary" and j["dependencies"] == [] for j in plan["jobs"])
    assert not (out / "jobs").exists()
    assert not (out / "execution_started.json").exists()
    frozen = load(out / "freeze.json")
    runner.verify(frozen["files"])
    assert frozen["automatic_retries"] == 0
    request = load(out / "templates/600039_summary.json")
    bound = runner.bind(request, [], out)
    assert bound == request  # Existing text/percentages/placeholders remain opaque.
    assert "RECOVERY_CONTENT:opaque" in request["messages"][1]["content"]
    assert "# 600820" not in request["messages"][1]["content"]
    assert request["messages"][0]["content"] == (
        cli.REPO / "config/prompts/company_summary.md"
    ).read_text("utf8")


def test_summary_run_requires_explicit_paid_and_rejects_recovery(tmp_path, monkeypatch):
    def denied(*args):
        pytest.fail("Execution should not be reached")

    monkeypatch.setattr(runner, "execute", denied)
    with pytest.raises(SystemExit):
        cli.main(["summary-run", "--source", str(tmp_path)])
    save(tmp_path / "plan.json", {"jobs": [{"kind": "recovery", "dependencies": []}]})
    with pytest.raises(ValueError, match="summary_only"):
        cli.main(["summary-run", "--source", str(tmp_path), "--paid"])


def test_handoff_keeps_raw_summary_metadata_and_partial(materials, tmp_path):
    source, _, summaries = materials
    out = tmp_path / "handoff"
    protected = {
        str(p): sha(p)
        for root in (source, summaries)
        for p in root.rglob("*")
        if p.is_file()
    }
    result = stage3_input(summaries, source, out, cli.REPO)
    assert result["model_calls"] == 0
    assert (
        "max_input_tokens" not in result["selection"]
    )  # Old peers quota is unrelated.
    assert all(c["summary_status"] == "partial" for c in result["companies"])
    assert (out / "600039/完整摘要.md").read_bytes() == (
        summaries / "600039/完整摘要.md"
    ).read_bytes()
    data = read_stage3_input(out)
    assert "局部摘要、保留缺口" in data["body"] and "5–8%" in data["body"]
    assert data["metadata"][0]["issues"] == ["context_uncertain"]
    assert (
        data["metadata"][0]["structure_tables"]
        == load(source / "stage1/600039_2023/data.json")["tables"][:1]
    )
    assert all(sha(Path(p)) == h for p, h in protected.items())
    with pytest.raises(FileExistsError):
        stage3_input(summaries, source, out, cli.REPO)


def test_company_filter_unknown_identity_and_file_parameters(materials, tmp_path):
    source, _, summaries = materials
    path = source / "stage1/600039_2023/data.json"
    raw = load(path)
    raw["company"] = {"code": None, "name": None}
    path.write_text(json.dumps(raw))
    out = tmp_path / "selected"
    stage3_input(summaries, source, out, cli.REPO, ["600039"])
    records = read_stage3_input(out)["metadata"]
    assert len(records) == 1 and records[0]["company"]["code"] is None
    assert (
        records[0]["declared_code"] == "600039"
    )  # Manifest identity, no model repair.
    out = tmp_path / "single"
    cli.main(
        [
            "stage3-input",
            "--source",
            str(summaries / "600039/完整摘要.md"),
            "--metadata",
            str(path),
            "--company",
            "600039",
            "--output",
            str(out),
        ]
    )
    assert "身份未定元数据" in read_stage3_input(out)["body"]


def test_output_overlap_and_handoff_tamper_rejected(materials, tmp_path):
    source, _, summaries = materials
    with pytest.raises(ValueError, match="separate"):
        stage3_input(summaries, source, source / "new", cli.REPO)
    out = tmp_path / "handoff"
    stage3_input(summaries, source, out, cli.REPO)
    (out / "模型输入.md").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        read_stage3_input(out)


def test_extract_output_is_direct_context_input(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0),
    )
    output = tmp_path / "new_run"
    cli.main(
        [
            "extract",
            "--source",
            str(tmp_path / "report.pdf"),
            "--output",
            str(output),
            "--company",
            "600039",
            "--year",
            "2025",
            "--profile",
            "construction",
        ]
    )
    record = load(output / "sources.json")["records"][0]
    assert record["code"] == "600039" and record["year"] == 2025
    assert calls[0][calls[0].index("--output") + 1] == str(
        output / "stage1/600039_2025"
    )
    cli.main(
        ["context", "--source", str(output), "--output", str(tmp_path / "context")]
    )
    assert calls[1][calls[1].index("--source-run") + 1] == str(output)


def test_multiple_runs_are_collected_without_financial_changes(materials, tmp_path):
    source, _, _ = materials
    other = tmp_path / "other"
    save(other / "sources.json", {"records": [{"code": "000568", "year": 2025}]})
    original = source / "stage1/600039_2023/data.json"
    save(other / "stage1/000568_2025/data.json", original.read_bytes())
    save(other / "stage1/000568_2025/阅读.md", "original reading")
    for row in load(source / "sources.json")["records"]:
        save(
            source / "stage1" / f"{row['code']}_{row['year']}" / "阅读.md",
            "raw reading",
        )
    path = cli.context_source([source, other], tmp_path / "new_context")
    assert len(load(path / "sources.json")["records"]) == 3
    assert (path / "stage1/000568_2025/data.json").read_bytes() == original.read_bytes()
    assert load(path / "sources.json")["records"][0]["year"] == 2023
    with pytest.raises(ValueError, match="duplicate_declared"):
        cli.context_source([source, source], tmp_path / "duplicate")


def test_collection_cannot_write_inside_baseline(materials, tmp_path):
    source, context, _ = materials
    before = {str(p): sha(p) for p in context.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="separate"):
        cli.context_source([source, source], context / "new", context)
    assert not (context / "new_inputs").exists()
    assert before == {str(p): sha(p) for p in context.rglob("*") if p.is_file()}


def test_stage3_package_remains_readable_without_original_directories(
    materials, tmp_path
):
    source, _, summaries = materials
    out = tmp_path / "portable"
    stage3_input(summaries, source, out, cli.REPO)
    expected = read_stage3_input(out)
    source.rename(tmp_path / "source_unavailable")
    summaries.rename(tmp_path / "summary_unavailable")
    assert read_stage3_input(out) == expected


@pytest.mark.parametrize(
    "status,expected", [("complete", 0), ("partial", 2), ("failed", 2)]
)
def test_summary_run_reports_transport_result_without_content_gate(
    tmp_path, monkeypatch, status, expected
):
    save(tmp_path / "plan.json", {"jobs": [{"kind": "summary", "dependencies": []}]})
    monkeypatch.setattr(
        runner,
        "execute",
        lambda repo, folder: save(
            folder / "执行结果.json", {"receipts": [{"status": status}]}
        ),
    )
    assert cli.main(["summary-run", "--source", str(tmp_path), "--paid"]) == expected
