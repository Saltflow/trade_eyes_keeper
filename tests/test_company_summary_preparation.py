"""Offline packaging checks; synthetic transport data, no financial claims."""

import socket
from pathlib import Path

import markdown
import pytest

from scripts import prepare_company_summary as prep

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Network attempted during summary preparation")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def frozen(tmp_path, raw=None):
    record = {"code": "TEST", "year": 2023, "pages": 2, "sha256": "pdf-original"}
    failure = {
        "source": "TEST_2023",
        "block": "notes",
        "raw_available": raw is not None,
    }
    folder = tmp_path / "stage1/TEST_2023/blocks/notes"
    parts = [
        {"type": "text", "text": '{"expected_year":2023}'},
        {"type": "text", "text": "X" * 100 + "\n\n短扫描页"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,TEST"}},
        {"type": "text", "text": "[block:notes]\nold task"},
    ]
    prep.write(folder / "request.json", {"messages": [{}, {"content": parts}]})
    prep.write(
        folder / "receipt.json",
        {"error_type": "ValueError" if raw else "ConnectionError"},
    )
    if raw is not None:
        prep.write(folder / "content.txt", raw)
    return record, [failure], folder


@pytest.mark.parametrize(
    "raw", ['{"k":"CAS","k":"CAS"}', '{\r\n"value":"5–8%","tail":']
)
def test_raw_is_copied_without_json_repair_or_losing_keys(tmp_path, raw):
    record, failures, folder = frozen(tmp_path, raw)
    before = prep.snapshot([tmp_path])
    text, images, info = prep.frozen_material(tmp_path, record, failures)
    assert raw in text
    assert "原异常类型：ValueError" in text
    assert text.count("[PDF物理页") == 2
    assert images[0]["label"] == "TEST_2023／PDF物理页 2／冻结图片"
    assert info["failures"][0]["raw_sha256"] == prep.sha(folder / "content.txt")
    assert prep.snapshot([tmp_path]) == before


def test_no_response_remains_execution_gap(tmp_path):
    record, failures, _ = frozen(tmp_path)
    text, _, info = prep.frozen_material(tmp_path, record, failures)
    assert "无模型正文；不能解释为年报未披露" in text
    assert info["failures"][0]["error_type"] == "ConnectionError"
    assert "raw_sha256" not in info["failures"][0]


def test_unknown_page_mapping_does_not_guess(tmp_path):
    record, failures, _ = frozen(tmp_path)
    record["pages"] = 3
    with pytest.raises(ValueError, match="page_mapping_unknown"):
        prep.frozen_material(tmp_path, record, failures)


def test_request_is_text_and_budget_reserves_full_dependency(tmp_path):
    config = prep.load(ROOT / "config/company_summary_contract.json")
    images = [
        {
            "label": "source page",
            "part": {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,TEST"},
            },
        }
    ]
    job = prep.save_job(
        tmp_path,
        "sample",
        "summary",
        "unchanged raw",
        images,
        "system",
        [],
        ["a", "b"],
        config,
        len,
    )
    request = prep.load(tmp_path / "sample/request.template.json")
    assert request["response_format"] == {"type": "text"}
    assert request["messages"][1]["content"][0]["text"] == "unchanged raw"
    assert request["messages"][1]["content"][1]["text"] == "source page"
    assert job["dependency_token_reserve"] == 2 * config["recovery_max_tokens"]
    assert job["image_token_bound"] == 1024
    assert job["input_token_bound"] == job["text_tokens"] + 1024 + 131072 + 4096
    with pytest.raises(FileExistsError):
        prep.save_job(
            tmp_path, "sample", "summary", "overwrite", [], "s", [], [], config, len
        )
    assert prep.load(tmp_path / "sample/request.template.json") == request


def test_example_keeps_text_percentages_missing_and_renders_tables(tmp_path):
    config = prep.load(ROOT / "config/company_summary_contract.json")
    row = {
        "identity_code": "CAP_ADJUSTED_ROE",
        "path": ["扣非ROE"],
        "unit": "%",
        "currency": None,
        "statistic": {"kind": "period_metric"},
        "values": {"a": "5–8%"},
        "missing_reasons": {"b": "not_extracted"},
    }
    table = {
        "title": "资本指标",
        "scope": {"kind": "group", "entity": "示例集团"},
        "accounting_basis": "原准则",
        "columns": {
            "a": {
                "label": "FY2023",
                "period": {"kind": "duration", "end": "2023-12-31"},
            },
            "b": {
                "label": "FY2022",
                "period": {"kind": "duration", "end": "2022-12-31"},
            },
        },
        "rows": [row],
    }
    prep.write(tmp_path / "stage1/TEST_2023/data.json", {"tables": [table]})
    template = (ROOT / config["prompts"]["example"]).read_text(encoding="utf8")
    text = prep.example(
        tmp_path,
        [{"code": "TEST", "company": "示例", "year": 2023}],
        "TEST",
        config,
        template,
    )
    assert "5–8%" in text and "原提取缺失：not_extracted" in text
    assert "不是模型实际摘要" in text
    html = markdown.markdown(text, extensions=["tables"])
    assert html.count("<table>") >= 5
    for i, q in enumerate(config["questions"], 1):
        assert f"## Q{i} {q}" in text


def test_questions_match_retained_goal_and_no_paid_client():
    config = prep.load(ROOT / "config/company_summary_contract.json")
    original = (ROOT / "docs/design/company_summary_question_origins.md").read_text(
        encoding="utf8"
    )
    for i, q in enumerate(config["questions"], 1):
        assert f"{i}. {q}" in original
    source = (ROOT / "scripts/prepare_company_summary.py").read_text(encoding="utf8")
    assert "import requests" not in source and "OpenAI(" not in source
    assert config["status"] == "offline_preparation_only"
