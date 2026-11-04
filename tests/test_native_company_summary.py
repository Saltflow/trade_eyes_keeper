"""Verify the native summary transport without model or network access."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_native_company_summary as native


def frozen_plan(tmp_path, input_bound=10):
    source = tmp_path / "source"
    templates = source / "templates"
    templates.mkdir(parents=True)
    template = templates / "X_summary.json"
    template.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "contract"},
                    {"role": "user", "content": "input"},
                ]
            }
        ),
        encoding="utf8",
    )
    plan = source / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "jobs": [
                    {"id": "X_summary", "kind": "summary", "input_bound": input_bound}
                ]
            }
        ),
        encoding="utf8",
    )
    frozen = source / "freeze.json"
    files = {
        str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [plan, template]
    }
    frozen.write_text(json.dumps({"files": files}), encoding="utf8")
    (source / "freeze.sha256").write_text(
        hashlib.sha256(frozen.read_bytes()).hexdigest(), encoding="utf8"
    )
    return source


def test_native_receipt_keeps_frozen_prompt_and_no_paid_api(tmp_path, monkeypatch):
    source = frozen_plan(tmp_path)
    output = tmp_path / "out"
    monkeypatch.setattr(native.shutil, "which", lambda _: "codex")

    def call(command, input, stdout, stderr, **kwargs):
        assert b"contract" in input and b"input" in input
        Path(command[command.index("-o") + 1]).write_text("summary", encoding="utf8")
        stdout.write(b'{"type":"turn.completed","usage":{"input_tokens":10}}\n')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(native.subprocess, "run", call)
    result = native.run(source, output, "gpt-6.1-terra", "high")
    assert result["status"] == "received"
    assert result["external_paid_api_calls"] == 0
    assert result["usage"]["input_tokens"] == 10
    assert (output / "content.md").read_text("utf8") == "summary"
    assert (output / "request.template.json").read_bytes() == (
        source / "templates/X_summary.json"
    ).read_bytes()
    with pytest.raises(FileExistsError):
        native.run(source, output, "gpt-6.1-terra", "high")


def test_native_rejects_oversized_context_before_call(tmp_path, monkeypatch):
    source = frozen_plan(tmp_path, input_bound=400001)
    monkeypatch.setattr(
        native.subprocess, "run", lambda *a, **k: pytest.fail("must not call")
    )
    with pytest.raises(ValueError, match="native_input_exceeds_safe_waterline"):
        native.run(source, tmp_path / "out", "gpt-6.1-terra", "high")


def test_native_rejects_oversized_character_input_before_call(tmp_path, monkeypatch):
    source = frozen_plan(tmp_path)
    template = source / "templates/X_summary.json"
    template.write_text(
        json.dumps({"messages": [{"role": "user", "content": "x" * 1048577}]}),
        encoding="utf8",
    )
    frozen = source / "freeze.json"
    data = json.loads(frozen.read_bytes())
    data["files"][str(template.resolve())] = hashlib.sha256(
        template.read_bytes()
    ).hexdigest()
    frozen.write_text(json.dumps(data), encoding="utf8")
    (source / "freeze.sha256").write_text(
        hashlib.sha256(frozen.read_bytes()).hexdigest(), encoding="utf8"
    )
    monkeypatch.setattr(
        native.subprocess, "run", lambda *a, **k: pytest.fail("must not call")
    )
    with pytest.raises(ValueError, match="native_input_exceeds_character_limit"):
        native.run(source, tmp_path / "out", "gpt-6.1-terra", "high")
