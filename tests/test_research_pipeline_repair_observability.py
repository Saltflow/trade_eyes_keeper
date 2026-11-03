"""Fault injection through real store/files and restricted repair boundaries."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.research_pipeline.common import load
from src.research_pipeline.models import ResearchSpec
from src.research_pipeline.observability import (
    FeishuOriginSender,
    ProgressPublisher,
    bounded_log_tail,
    progress_snapshot,
    redact,
)
from src.research_pipeline.repair import RepairManager
from src.research_pipeline.store import Store
from src.research_pipeline.tools import ResearchTools, research_tool_schema


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    package = repo / "src/research_pipeline"
    package.mkdir(parents=True)
    (repo / "src/__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sample.py").write_text("VALUE = 0\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/test_research_pipeline_generated_case.py").write_text(
        "from src.research_pipeline.sample import VALUE\n"
        "def test_real_bug():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "config").mkdir()
    (repo / "config/.env").write_text("API_KEY=do-not-copy-secret", encoding="utf-8")
    output = tmp_path / "run"
    output.mkdir()
    spec = ResearchSpec(
        companies=("a_share:600348",),
        output=output,
        repo=repo,
        store_root=tmp_path / "shared",
        budget_limit_usd="5",
        paid=True,
        origin={"channel": "feishu", "chat": "chat", "sender": "owner"},
    )
    store = Store(spec.store_root)
    store.create_run(spec.to_dict(), "run")
    return repo, output, spec, store


def test_redaction_and_page_failure_do_not_stop_core_event(setup, monkeypatch):
    _, output, _, store = setup
    import src.research_pipeline.observability as module

    real = module.atomic_write

    def locked(path, value, **kwargs):
        if Path(path).suffix == ".html":
            raise PermissionError("fixture HTML locked")
        return real(path, value, **kwargs)

    monkeypatch.setattr(module, "atomic_write", locked)
    publisher = ProgressPublisher(store, "run", output)
    publisher.event(
        "company_complete", company="a_share:600348", api_key="secret-value"
    )
    publisher.event("company_complete", company="us:XOM")
    events = store.events("run")
    assert sum(e["event"] == "company_complete" for e in events) == 2
    assert any(e["event"] == "presentation_failed" for e in events)
    assert events[1]["api_key"] == "[REDACTED]"
    assert load(output / "progress.json")["run_id"] == "run"
    assert (
        redact("Authorization: Bearer sk-abcdef1234567")
        == "Authorization: Bearer [REDACTED]"
    )


def test_uncertain_notification_is_not_repeated_and_cli_never_guesses_chat(setup):
    _, output, spec, store = setup
    calls = []

    def failed(origin, payload):
        calls.append(payload)
        raise TimeoutError("fixture send uncertain")

    publisher = ProgressPublisher(store, "run", output, sender=failed)
    assert publisher.notify("budget_stop", {"cause": "same"})["state"] == "unknown"
    assert publisher.notify("budget_stop", {"cause": "same"})["state"] == "unknown"
    assert len(calls) == 1
    spec.origin = {}
    store.create_run(spec.to_dict(), "cli")
    assert ProgressPublisher(store, "cli", output, sender=failed).notify(
        "run_started", {}
    ) == {"state": "local_only"}
    assert len(calls) == 1


def test_slow_notification_never_blocks_core_events_and_intent_is_durable(setup):
    import threading
    import time

    _, output, _, store = setup
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(origin, payload):
        entered.set()
        assert release.wait(5)
        calls.append(payload["event"])
        return {"state": "sent"}

    publisher = ProgressPublisher(store, "run", output, sender=slow)
    started = time.monotonic()
    publisher.event("run_started", phase="starting")
    assert time.monotonic() - started < 0.5
    assert entered.wait(2)
    assert any(
        load(path)["state"] == "unknown"
        for path in (output / "notifications").glob("*.json")
    )
    store.update_run("run", state="complete")
    started = time.monotonic()
    publisher.event("run_finished", phase="finished")
    assert time.monotonic() - started < 0.5
    assert store.get_run("run")["state"] == "complete"
    assert any(
        load(path)["state"] == "queued"
        for path in (output / "notifications").glob("*.json")
    )
    release.set()
    publisher.close()
    assert calls == ["run_started", "run_finished"]


def test_emergency_ledger_outage_alerts_once_without_any_store_access(
    setup, monkeypatch
):
    import sqlite3
    import time

    repo, output, spec, store = setup

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is permanently readonly")

    for method in ("get_run", "update_run", "event", "budget"):
        monkeypatch.setattr(store, method, unavailable)
    messages = []

    def send(chat, text):
        assert load(output / "emergency/ledger_unavailable.json")["state"] == "unknown"
        messages.append((chat, text))
        return {"outcome": "sent"}

    app = SimpleNamespace(
        gate=SimpleNamespace(is_allowed=lambda chat: chat == "chat"),
        send_personal_message=send,
    )
    sender = FeishuOriginSender(repo, store, app)
    first = ProgressPublisher(store, "run", output, sender)
    second = ProgressPublisher(store, "run", output, sender)
    started = time.monotonic()
    first.emergency("SQLite readonly api_key=do-not-expose-secret", spec=spec)
    assert time.monotonic() - started < 0.5
    second.emergency("a different failure from the host", spec=spec)
    first.close()
    second.close()
    assert len(messages) == 1 and messages[0][0] == "chat"
    assert "do-not-expose-secret" not in messages[0][1]
    marker = load(output / "emergency/ledger_unavailable.json")
    assert marker["state"] == "sent"
    assert "do-not-expose-secret" not in json.dumps(marker)
    progress = load(output / "progress.json")
    assert progress["state"] == "ledger_unavailable"
    assert progress["active"] == "unknown" and progress["requests_not_restarted"]
    assert "ledger_unavailable" in (output / "index.html").read_text("utf-8")
    trusted = {"id": "run", "spec": {"origin": spec.origin}}
    assert (
        sender.emergency(dict(spec.origin, chat="other"), marker["payload"], trusted)[
            "state"
        ]
        == "failed"
    )
    denied = {"channel": "feishu", "chat": "other", "sender": "owner"}
    assert (
        sender.emergency(
            denied, marker["payload"], {"id": "run", "spec": {"origin": denied}}
        )["state"]
        == "failed"
    )
    assert len(messages) == 1


def test_emergency_cli_is_local_and_output_failure_uses_redacted_stderr(
    setup, monkeypatch, capsys
):
    import src.research_pipeline.observability as module

    _, output, spec, store = setup
    spec.origin = {}
    sender = SimpleNamespace(emergency=lambda *_: pytest.fail("CLI must not send"))
    publisher = ProgressPublisher(store, "run", output, sender)
    assert publisher.emergency("ledger readonly", spec=spec)["state"] == "local_only"
    assert load(output / "emergency/ledger_unavailable.json")["state"] == "local_only"
    with pytest.raises(ValueError, match="trusted_run_spec"):
        publisher.emergency("bad caller", spec=spec.to_dict())

    def locked(*args, **kwargs):
        raise PermissionError("disk readonly password=must-not-leak")

    monkeypatch.setattr(module, "atomic_write", locked)
    # Use a separate path with no existing durable intent.
    spec.output = output / "another"
    publisher = ProgressPublisher(store, "other", spec.output, sender)
    result = publisher.emergency("ledger unavailable token=must-not-leak", spec=spec)
    assert result["state"] == "local_error" and result["outbound_not_attempted"]
    stderr = capsys.readouterr().err
    assert "must-not-leak" not in stderr and "local_error" in stderr


def test_page_is_escaped_and_state_log_has_scope(setup):
    _, output, _, store = setup
    store.update_run(
        "run", error="<script>alert('x')</script>", state="awaiting_approval"
    )
    publisher = ProgressPublisher(store, "run", output)
    publisher.publish()
    page = (output / "index.html").read_text("utf-8")
    assert "&lt;script&gt;" in page and "<script>alert" not in page
    assert "setTimeout" in page and "awaiting_approval" in page
    assert bounded_log_tail(store, "run", limit=10)["run_id"] == "run"
    with pytest.raises(ValueError):
        bounded_log_tail(store, "run", limit=201)


def test_progress_distinguishes_queued_intent_from_received_stream(setup):
    _, output, _, store = setup
    store.update_run("run", state="running")
    store.claim_task("pending", "run", output / "pending", phase="summary")
    store.import_attempt(
        "streaming",
        {"producer_run": "run", "status": "sending", "phase": "peer"},
    )
    store.event(
        "run",
        "stream_progress",
        task_key="streaming",
        received_bytes=1234,
        phase="peer",
        last_byte_at="2026-11-01T00:00:01Z",
    )
    value = progress_snapshot(store, "run")
    assert value["activity"]["queued"] == 1
    assert value["activity"]["sending_intent"] == 1
    assert value["stream_activity"][0]["received_bytes"] == 1234
    assert value["last_stream_at"] == "2026-11-01T00:00:01Z"


def test_repair_proposal_keeps_live_source_secret_and_protected_files(setup):
    repo, _, _, store = setup
    manager = RepairManager(store, "run", repo)
    incident = manager.diagnose("missing_path", {"api_key": "hidden"})
    same = manager.diagnose(
        "missing_path", {"events": [{"at": "different", "seq": 999}]}
    )
    assert same["id"] == incident["id"]
    candidate = manager.propose(
        incident["id"], {"src/research_pipeline/sample.py": "VALUE = 1\n"}
    )
    assert (repo / "src/research_pipeline/sample.py").read_text() == "VALUE = 0\n"
    assert not (Path(candidate["workspace"]) / "config/.env").exists()
    assert "VALUE = 1" in candidate["diff"]
    for target in [
        "../escape.py",
        "config/prompts/research_final_v6.md",
        "config/.env",
        "tests/test_any.py",
        "data/request.json",
        "src/research_pipeline/store.py",
        "src/research_pipeline/repair.py",
    ]:
        with pytest.raises(ValueError):
            manager.propose(incident["id"], {target: "bad"})
    with pytest.raises(ValueError, match="financial_or_budget"):
        manager.propose(
            incident["id"],
            {"config/research_pipeline.json": '{"budget_limit_usd":999}'},
        )
    denied = manager.diagnose("HTTPStatus429 source_blocked")
    with pytest.raises(ValueError, match="diagnostic_only"):
        manager.propose(denied["id"], {"src/research_pipeline/sample.py": "VALUE = 1"})


def test_readonly_validation_config_is_bound_but_private_yaml_is_rejected(setup):
    repo, _, _, store = setup
    (repo / "config/__init__.py").write_text("# public validation loader\n", "utf-8")
    rules = repo / "config/alerts.yaml"
    rules.write_text("version: 1\nanchors: []\n", "utf-8")
    manager = RepairManager(store, "run", repo)
    incident = manager.diagnose("validation public asset regression")
    candidate = manager.propose(
        incident["id"], {"src/research_pipeline/sample.py": "VALUE = 1\n"}
    )
    workspace = Path(candidate["workspace"])
    assert "config/__init__.py" in candidate["baseline"]
    assert "config/alerts.yaml" in candidate["baseline"]
    assert (workspace / "config/alerts.yaml").read_bytes() == rules.read_bytes()
    assert not (workspace / "config/.env").exists()
    with pytest.raises(ValueError, match="allowlist"):
        manager.propose(incident["id"], {"config/alerts.yaml": "version: 2\n"})
    rules.write_text("password: do-not-copy\n", "utf-8")
    with pytest.raises(ValueError, match="not_public_contract"):
        manager.propose(
            incident["id"], {"src/research_pipeline/sample.py": "VALUE = 2\n"}
        )


def test_frozen_runtime_contains_actual_repair_tests_and_keeps_secrets_out(setup):
    from src.research_pipeline.engine import _freeze_runtime

    repo, output, _, store = setup
    for name in (
        "fundamental_data_v2.json",
        "company_summary_contract.json",
        "research_report.json",
        "research_tariff_calendar.json",
    ):
        (repo / "config" / name).write_text("{}", "utf-8")
    runtime = _freeze_runtime(repo, output)
    store.update_run("run", runtime_repo=str(runtime))
    manager = RepairManager(store, "run", repo)
    result = manager.read_source("tests/test_research_pipeline_generated_case.py")
    assert "assert VALUE == 1" in result["text"]
    assert not (runtime / "config/.env").exists()
    original = repo / "tests/test_research_pipeline_generated_case.py"
    original.write_text("# newer live test must not replace frozen evidence\n", "utf-8")
    assert (
        "assert VALUE == 1"
        in manager.read_source("tests/test_research_pipeline_generated_case.py")["text"]
    )


@pytest.mark.skipif(os.name != "posix", reason="Linux container permission regression")
def test_candidate_public_modes_allow_nonroot_without_relaxing_live_tree(setup):
    repo, _, _, store = setup
    original = repo / "src/research_pipeline/sample.py"
    original.chmod(0o600)
    manager = RepairManager(store, "run", repo)
    incident = manager.diagnose("candidate nonroot read regression")
    previous = os.umask(0o077)
    try:
        candidate = manager.propose(
            incident["id"],
            {"src/research_pipeline/nested/sample.py": "VALUE = 1\n"},
        )
    finally:
        os.umask(previous)
    workspace = Path(candidate["workspace"])
    assert original.stat().st_mode & 0o777 == 0o600
    assert not (workspace / "config/.env").exists()
    for path in [workspace, *workspace.rglob("*")]:
        assert path.stat().st_mode & 0o777 == (0o755 if path.is_dir() else 0o644)


def _trusted_runner(workspace, tests, results):
    env = {
        **os.environ,
        "PYTHONPATH": str(workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    command = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        *tests,
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(results / "tmp"),
        "--confcutdir",
        str(workspace),
    ]
    result = subprocess.run(
        command,
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
        check=False,
    )
    return {
        "status": "passed" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
        "command": command,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def test_actual_red_to_green_validation_then_owner_bound_revision(setup, tmp_path):
    repo, _, _, store = setup
    baseline = _trusted_runner(
        repo, ["tests/test_research_pipeline_generated_case.py"], tmp_path / "baseline"
    )
    assert baseline["exit_code"] != 0
    manager = RepairManager(
        store, "run", repo, runner=_trusted_runner, allow_test_receipts=True
    )
    incident = manager.diagnose("fixture calculation bug")
    candidate = manager.propose(
        incident["id"], {"src/research_pipeline/sample.py": "VALUE = 1\n"}
    )
    with pytest.raises(ValueError, match="tested_candidate"):
        manager.confirmation_payload(candidate["id"], "owner")
    receipt = manager.test(candidate["id"])
    assert receipt["exit_code"] == 0 and receipt["status"] == "passed"
    binding = manager.confirmation_payload(candidate["id"], "owner")
    with pytest.raises(PermissionError):
        manager.confirm_apply(candidate["id"], binding, "other-user")
    bad = dict(binding, candidate_hash="changed")
    with pytest.raises(PermissionError):
        manager.confirm_apply(candidate["id"], bad, "owner")
    revision = manager.confirm_apply(candidate["id"], binding, "owner")
    assert (
        Path(revision["runtime_repo"]) / "src/research_pipeline/sample.py"
    ).read_text() == "VALUE = 1\n"
    assert (repo / "src/research_pipeline/sample.py").read_text() == "VALUE = 0\n"
    assert store.budget("run")["known_usage_usd"] == "0"


def test_missing_sandbox_is_not_passed_and_source_drift_expires_approval(
    setup, monkeypatch
):
    repo, _, _, store = setup
    manager = RepairManager(store, "run", repo)
    incident = manager.diagnose("fixture")
    candidate = manager.propose(
        incident["id"], {"src/research_pipeline/sample.py": "VALUE = 1\n"}
    )
    monkeypatch.setattr("src.research_pipeline.repair.shutil.which", lambda _: None)
    result = manager.test(candidate["id"])
    assert result["status"] == "test_unavailable"
    with pytest.raises(ValueError):
        manager.confirmation_payload(candidate["id"], "owner")
    manager.runner = lambda *args: {"status": "passed", "exit_code": 0}
    manager.allow_test_receipts = True
    manager.test(candidate["id"])
    binding = manager.confirmation_payload(candidate["id"], "owner")
    (repo / "src/research_pipeline/sample.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="baseline_changed"):
        manager.confirm_apply(candidate["id"], binding, "owner")


def test_candidate_tampering_and_fake_model_test_claim_do_not_authorize(setup):
    repo, _, _, store = setup
    manager = RepairManager(store, "run", repo)
    incident = manager.diagnose("fixture")
    candidate = manager.propose(
        incident["id"], {"src/research_pipeline/sample.py": "VALUE = 1\n"}
    )
    (Path(candidate["workspace"]) / "src/research_pipeline/sample.py").write_text(
        "VALUE = 99\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="candidate_changed"):
        manager.test(candidate["id"])
    assert store.get_run("run").get("execution_revision") is None


def test_tools_require_original_chat_and_sender_and_hide_apply(setup):
    repo, _, _, store = setup
    tools = ResearchTools(repo, store)
    names = {t["function"]["name"] for t in research_tool_schema()}
    assert not any("apply" in name for name in names)
    for chat, sender in [("other-chat", "owner"), ("chat", "other-user")]:
        with pytest.raises(PermissionError):
            tools.call("research_pipeline_status", {"run_id": "run"}, chat, sender)
    assert (
        tools.call("research_pipeline_status", {"run_id": "run"}, "chat", "owner")[
            "run_id"
        ]
        == "run"
    )
    assert (
        tools.call("research_pipeline_tasks", {}, "chat", "owner")["runs"][0]["id"]
        == "run"
    )
    assert tools.call("research_pipeline_tasks", {}, "chat", "other")["runs"] == []
    with pytest.raises(ValueError):
        tools.call(
            "research_pipeline_logs",
            {"run_id": "run", "path": "config/.env"},
            "chat",
            "owner",
        )


def test_bounded_pi_loop_uses_existing_harness_and_never_applies(setup):
    repo, _, _, store = setup
    manager = RepairManager(store, "run", repo, executor=object())
    incident = manager.diagnose("fixture")
    rounds = []

    class Harness:
        def __init__(self, client, settings, root, stop):
            assert settings.harness_backend == "pi"
            assert (
                settings.max_tool_rounds == 12
                and settings.conversation_timeout_seconds == 600
            )

        def run(self, messages, tools, invoke, *args):
            assert not any("apply" in t["function"]["name"] for t in tools)
            rounds.append(messages)
            return "没有通过实际测试，不能声称修复成功。"

    result = manager.run_agent(incident["id"], harness_factory=Harness)
    assert result["state"] == "needs_manual" and len(rounds) == 2
    manager.run_agent(incident["id"], harness_factory=Harness)
    assert len(rounds) == 2 and not store.get_run("run").get("execution_revision")


def test_pi_audit_model_round_cannot_collide_with_repair_round_or_owner(setup):
    repo, _, _, store = setup
    manager = RepairManager(store, "run", repo, executor=object())
    incident = manager.diagnose("real harness round metadata regression")

    class Harness:
        def __init__(self, *args):
            pass

        def run(self, messages, tools, invoke, audit, *args):
            audit(
                "model_request",
                {"round": 7, "incident_id": "untrusted", "run_id": "untrusted"},
            )
            return "No candidate generated in this logging-only regression."

    manager.run_agent(incident["id"], harness_factory=Harness)
    records = [
        event
        for event in store.events("run")
        if event["event"] == "repair_model_request"
    ]
    assert len(records) == 2
    assert [record["round"] for record in records] == [1, 2]
    assert all(record["model_round"] == 7 for record in records)
    assert all(record["incident_id"] == incident["id"] for record in records)
    assert all(record["run_id"] == "run" for record in records)


def test_feishu_sender_checks_allowlist_and_delivers_actual_files(setup):
    from src.research_pipeline.common import atomic_write, file_sha

    repo, output, _, store = setup
    (output / "research/assets").mkdir(parents=True)
    (output / "research/最终报告.md").write_text("# 完整报告", encoding="utf-8")
    (output / "delivery").mkdir()
    (output / "delivery/accounting.json").write_text(
        '{"usage_usd":"1.23"}', encoding="utf-8"
    )
    (output / "source_coverage.json").write_text('{"available":44}', encoding="utf-8")
    (output / "delivery/private.json").write_text(
        '{"api_key":"secret"}', encoding="utf-8"
    )
    model_id = "f" * 64
    folder = output / "delivery/investigation/study"
    # Explicit synthetic business data: token counts must not be redacted or
    # reformatted because this would break input hashes and reproducibility.
    input_name = f"models/{model_id}/input/inputs.json"
    code_name = f"models/{model_id}/input/model.py"
    atomic_write(folder / input_name, {"token": 123, "fixture": True})
    atomic_write(folder / code_name, "# Synthetic model fixture\n")
    atomic_write(folder / "execution/request.json", {"private_diagnostic": True})
    atomic_write(folder / "execution/content.md", "private diagnostic fixture")
    atomic_write(
        folder / "bundle.json",
        {
            "files": {
                name: file_sha(folder / name)
                for name in (input_name, code_name, "execution/request.json")
            },
            "selected_model": model_id,
            "model_status": "passed",
            "error": "",
            "financial_validation": "not_independently_verified",
        },
    )
    atomic_write(
        output / "delivery/manifest.json",
        {
            "artifacts": [
                {
                    "kind": "investigation",
                    "bundle_path": str(folder / "bundle.json"),
                    "metadata": {"bundle_sha256": file_sha(folder / "bundle.json")},
                }
            ]
        },
    )
    messages, files = [], []
    app = SimpleNamespace(
        gate=SimpleNamespace(is_allowed=lambda chat: chat == "chat"),
        send_personal_message=lambda chat, text: (
            messages.append((chat, text)) or {"outcome": "sent"}
        ),
        send_file=lambda chat, path: files.append((chat, path.name)) or (True, "ok"),
    )
    sender = FeishuOriginSender(repo, store, app)
    origin = {"channel": "feishu", "chat": "chat", "sender": "owner"}
    result = sender(
        origin, {"run_id": "run", "event": "run_finished", "status": "partial"}
    )
    assert result["state"] == "sent" and messages and files
    assert any(name == "research_public_bundle.zip" for _, name in files)
    import zipfile

    with zipfile.ZipFile(output / "delivery/research_public_bundle.zip") as bundle:
        assert "delivery/accounting.json" in bundle.namelist()
        assert "source_coverage.json" in bundle.namelist()
        assert "delivery/private.json" not in bundle.namelist()
        prefix = "delivery/investigation/study/"
        assert prefix + code_name in bundle.namelist()
        assert bundle.read(prefix + input_name) == (folder / input_name).read_bytes()
        public_model = json.loads(bundle.read(prefix + "reproducibility.json"))
        assert public_model["files"][input_name] == file_sha(folder / input_name)
        assert prefix + "execution/request.json" not in bundle.namelist()
        assert prefix + "execution/content.md" not in bundle.namelist()
    sender(origin, {"run_id": "run", "event": "run_finished", "status": "partial"})
    assert len(files) == len(set(files))
    assert (
        sender(dict(origin, chat="other"), {"run_id": "run", "event": "run_finished"})[
            "state"
        ]
        == "failed"
    )


def test_repair_model_turn_uses_shared_executor_and_deadline(setup):
    import time

    from src.research_pipeline.repair import _BudgetedRepairClient

    repo, output, _, store = setup
    capture = output / "model_capture"
    capture.mkdir()
    (capture / "content.md").write_text(
        json.dumps({"content": "只诊断，不应用", "tool_calls": []}), encoding="utf-8"
    )
    calls = []

    class Executor:
        def call(self, key, request, folder, phase, **kwargs):
            calls.append((key, request, phase, kwargs))
            return {"status": "complete", "capture_dir": str(capture)}

    manager = RepairManager(store, "run", repo, executor=Executor())
    incident = manager.diagnose("fixture")
    client = _BudgetedRepairClient(manager, incident["id"], 1)
    deadline = time.monotonic() + 10
    result = client.complete(
        [{"role": "user", "content": "诊断"}], [], deadline=deadline
    )
    assert result["content"] == "只诊断，不应用"
    assert calls[0][2] == "repair_model" and calls[0][3] == {
        "repair": True,
        "deadline": deadline,
    }
    assert calls[0][1]["model"] == "deepseek-flash"
    with pytest.raises(RuntimeError, match="time_limit"):
        client.complete([], [], deadline=time.monotonic() - 1)
    assert len(calls) == 1


def test_feishu_confirmation_dispatch_remains_human_owner_bound(setup, monkeypatch):
    import threading

    from src.interactive.assistant.service import FeishuAssistant
    from src.interactive.assistant.store import ProposalStore

    repo, _, _, _ = setup
    assistant = FeishuAssistant.__new__(FeishuAssistant)
    assistant.root = repo
    assistant.store = ProposalStore(repo / "actions.sqlite3")
    assistant._operation_lock = threading.RLock()
    assistant._stop = threading.Event()
    assistant.redact = str
    replies, applied = [], []
    assistant._reply = lambda chat, text: replies.append((chat, text)) or True

    class Tools:
        def __init__(self, root):
            assert root == repo

        def apply_confirmation(self, payload, chat, sender):
            applied.append((payload, chat, sender))
            return {"run_id": "new-run", "state": "created"}

    monkeypatch.setattr("src.research_pipeline.tools.ResearchTools", Tools)
    proposal = assistant.store.create(
        "research_pipeline", "chat", "owner", {"operation": "fixture"}, 900
    )
    assistant.store.activate(proposal["id"])
    with pytest.raises(ValueError):
        assistant._control("other-chat", "owner", "确认", proposal["id"])
    assert applied == []
    assistant._control("chat", "owner", "确认", proposal["id"])
    assert len(applied) == 1
    assert assistant.store.get(proposal["id"], "chat", "owner")["status"] == "succeeded"
    with pytest.raises(ValueError):
        assistant._control("chat", "owner", "确认", proposal["id"])
    assert len(applied) == 1
