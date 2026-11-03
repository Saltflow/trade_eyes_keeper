"""Offline IO boundaries: no live model, public network or host code execution."""

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from src.data_hub import public_web
from src.research_pipeline.common import atomic_write, file_sha
from src.research_pipeline.investigation import (
    ResearchWorkspace,
    model_charts,
    reproducibility_files,
    verify_bundle,
)
from src.research_pipeline.model_sandbox import ModelSandbox
from tests.research_investigation_support import sandbox_result
from tests.test_research_pipeline_investigation import save
from tests.test_research_pipeline_investigation import (
    workspace as workspace,  # noqa: PLC0414 - pytest fixture export
)


@pytest.mark.parametrize(
    "outcome", ["computed", "invalid_output", "timeout", "cancelled", "unknown"]
)
def test_container_controls_results_and_cleanup(tmp_path, monkeypatch, outcome):
    inputs, output = tmp_path / "input", tmp_path / "output"
    inputs.mkdir()
    (inputs / "model.py").write_text("# synthetic fixture", encoding="utf-8")
    image_id = "sha256:" + "a" * 64
    commands, cancelled = [], [False]
    monkeypatch.setattr(
        "src.research_pipeline.model_sandbox.shutil.which", lambda n: "docker"
    )

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "image":
            return SimpleNamespace(returncode=0, stdout=image_id)
        if outcome == "unknown":
            raise subprocess.TimeoutExpired(command, 15)
        return SimpleNamespace(returncode=0)

    class Process:
        returncode = None

        def __init__(self, command, **kwargs):
            commands.append(command)
            assert kwargs["shell"] is False
            raw = {"checks": [{"name": "fixture", "passed": True}], "eps": 4}
            if outcome == "invalid_output":
                raw["checks"][0]["passed"] = "true"
            atomic_write(output / "files/results.json", raw)
            if outcome in {"computed", "invalid_output"}:
                self.returncode = 0
            else:
                cancelled[0] = outcome != "timeout"

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.returncode = -1

    monkeypatch.setattr("src.research_pipeline.model_sandbox.subprocess.run", run)
    monkeypatch.setattr("src.research_pipeline.model_sandbox.subprocess.Popen", Process)
    # Advance the deadline only after launch, testing cleanup rather than preflight.
    clocks = iter([0, 0, 0, 200])
    monkeypatch.setattr(
        "src.research_pipeline.model_sandbox.time.monotonic", lambda: next(clocks, 200)
    )
    receipt = ModelSandbox().run(
        inputs,
        output,
        cancel=SimpleNamespace(is_set=lambda: cancelled[0]),
        deadline=100,
    )
    assert receipt["status"] == outcome
    command = next(c for c in commands if c[1] == "run")
    for flag, value in (
        ("--network", "none"),
        ("--pull", "never"),
        ("--user", "65534:65534"),
        ("--cap-drop", "ALL"),
    ):
        assert command[command.index(flag) + 1] == value
    assert "--read-only" in command and "no-new-privileges" in command
    mounts = [command[i + 1] for i, s in enumerate(command) if s == "--mount"]
    assert len(mounts) == 2 and all(".env" not in m for m in mounts)
    assert mounts[0].endswith("dst=/input,readonly")
    assert image_id in command and "-I" in command
    if outcome == "computed":
        assert receipt["files"]["results.json"] == file_sha(
            output / "files/results.json"
        )
        assert receipt["financial_validation"] == "not_independently_verified"
    if outcome in {"timeout", "cancelled", "unknown"}:
        assert any(c[1:3] == ["rm", "-f"] for c in commands)


def test_redirect_private_address_is_blocked_before_connect(workspace, monkeypatch):
    calls = []

    def resolve(url):
        calls.append(url)
        if "internal" in url:
            raise ValueError("nonpublic_source_address")
        return public_web.urlsplit(url), "93.184.216.34"

    class Connection:
        def __init__(self, host, ip, timeout):
            assert ip == "93.184.216.34"

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return SimpleNamespace(
                status=302, getheader=lambda n: "https://internal/secret"
            )

        def close(self):
            pass

    monkeypatch.setattr(public_web, "public_address", resolve)
    monkeypatch.setattr(public_web, "_PinnedHTTPS", Connection)
    with pytest.raises(ValueError, match="nonpublic"):
        public_web.fetch_public(
            "https://example.org/report",
            workspace.authority,
            deadline=time.monotonic() + 10,
            cancel=workspace.cancel,
        )
    assert calls == ["https://example.org/report", "https://internal/secret"]


def test_tool_replay_preserves_original_catalog_and_text(workspace, tmp_path):
    before = workspace.invoke("find_sources", {})
    sid = before["results"][0]["source_id"]
    read = workspace.invoke("read_source", {"source_id": sid})
    additional = tmp_path / "supplement.md"
    additional.write_text("new public evidence", encoding="utf-8")
    workspace.register(additional, "supplement")
    resumed = ResearchWorkspace(workspace.pipeline, workspace.key)
    assert resumed.invoke("find_sources", {}) == before
    assert resumed.invoke("read_source", {"source_id": sid}) == read
    assert resumed.invoke("find_sources", {})["source_count"] == 2
    assert read["text_sha256"] == file_sha(workspace.root / "text" / (sid + ".txt"))


def test_late_transport_audit_does_not_invalidate_frozen_research(
    workspace, monkeypatch
):
    monkeypatch.setattr(ModelSandbox, "run", sandbox_result)
    model = save(workspace)
    workspace.run_model(model)
    events = workspace.root / "execution/request/jobs/request/events.jsonl"
    atomic_write(events, '{"state":"sending"}\n')
    workspace.freeze("Deadline reached before selection", "deadline_inflight")
    bundle = workspace.root / "bundle.json"
    frozen_hash = file_sha(bundle)

    # The request worker finishes after the research deadline. No old manifest
    # is rewritten, and the unselected model must remain unselected.
    with events.open("a", encoding="utf-8") as stream:
        stream.write('{"state":"complete"}\n')
    atomic_write(events.parent / "receipt.json", {"status": "complete"})
    manifest = verify_bundle(bundle)
    assert manifest["selected_model"] is None
    assert manifest["error"] == "deadline_inflight"
    assert file_sha(bundle) == frozen_hash
    assert all(not p.startswith("execution/") for p in manifest["files"])
    _, public = reproducibility_files(bundle)
    result = f"models/{model}/output/files/results.json"
    assert result in public["files"]

    # Excluding mutable transport logs must not weaken calculation integrity.
    atomic_write(workspace.root / result, {"profit": 999, "checks": []})
    with pytest.raises(ValueError, match="investigation_integrity_changed"):
        verify_bundle(bundle)


def test_search_provider_selection_and_primary_link_extraction(workspace, monkeypatch):
    query = "issuer annual report & solvency"
    assert public_web.urlsplit(public_web.search_url(query)).hostname == "cn.bing.com"
    assert public_web.parse_qs(
        public_web.urlsplit(public_web.search_url(query)).query
    ) == {"q": [query]}
    assert "duckduckgo.com" in public_web.search_url(query, "duckduckgo")
    with pytest.raises(ValueError, match="unsupported_search_provider"):
        public_web.search_url(query, "uncontrolled-host")
    raw = b"""<li class="b_algo"><h2><a href="https://issuer.example/report">Annual</a></h2>
        <div class="b_caption"><p>Discovery only</p></div></li>
        <li class="b_algo"><h2><a href="/search?q=other">Internal</a></h2></li>
        <li class="b_algo"><h2><a href="http://issuer.example/plain">Insecure</a></h2></li>"""
    rows = public_web.search_results(raw)
    assert rows == [
        {
            "title": "Annual",
            "url": "https://issuer.example/report",
            "snippet": "Discovery only",
            "status": "discovery_only_not_primary_evidence",
        }
    ]
    assert public_web.search_results(b"<html>No results</html>") == []
    calls = []

    def unavailable(url, purpose, refresh):
        calls.append(url)
        raise TimeoutError("search transport unavailable")

    monkeypatch.setattr(workspace, "fetch_source", unavailable)
    with pytest.raises(TimeoutError, match="search transport unavailable"):
        workspace.search_public(query)
    assert calls == [public_web.search_url(query)]  # No hidden fallback or retry.


def test_computed_chart_values_and_source_conflicts_are_preserved(
    workspace, monkeypatch, tmp_path
):
    def calculate(self, inputs, output, **kwargs):
        receipt = sandbox_result(self, inputs, output, **kwargs)
        path = output / "files/results.json"
        value = json.loads(path.read_text("utf-8"))
        value["charts"] = [
            {
                "id": "fixture_eps",
                "type": "bar",
                "title": "Synthetic EPS",
                "unit": "fixture",
                "scope": "fixture",
                "period_basis": "E",
                "version": "fixture",
                "source": "calculated",
                "x": [2031],
                "series": [{"name": "normal", "values": [value["eps"]]}],
            }
        ]
        atomic_write(path, value)
        return {**receipt, "files": {"results.json": file_sha(path)}}

    monkeypatch.setattr(ModelSandbox, "run", calculate)
    conflicting = tmp_path / "conflicting.md"
    conflicting.write_text(
        "SYNTHETIC TEST ONLY\nshares=3 (different rights basis)", encoding="utf-8"
    )
    workspace.register(conflicting, "conflicting evidence")
    model = save(workspace)
    workspace.run_model(model)
    workspace.select_model(model)
    workspace.freeze("Conflicting shares remain conditional; no silent overwrite.")
    charts = model_charts(
        {
            "metadata": {
                "bundle": str(workspace.root / "bundle.json"),
                "selected_model": model,
            }
        }
    )
    assert "8.0" in charts and "fixture_eps" in charts
    assert len(workspace.sources) == 2
    from scripts.research_render import render

    (tmp_path / "views").mkdir()
    result = render(tmp_path / "views", charts)
    assert result["chart_blocks"] == 1 and not result["chart_issues"]
