"""Executable boundaries for portable libraries and explicit local authorities.

These tests exercise the public contracts and actual import graph. They do not
perform downloads or use a real model, account, server, or project data cache.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.platform_composition import authority_for
from src.shared_contracts import AuthorityRef

ROOT = Path(__file__).resolve().parents[1]
PORTABLE_PACKAGES = (
    "shared_contracts",
    "task_runtime",
    "data_hub",
    "model_gateway",
    "agent_runtime",
)
APPLICATION_PREFIXES = (
    "scripts",
    "src.core",
    "src.platform_composition",
    "src.research_pipeline",
    "src.interactive",
    "src.fundamental_data",
    "src.fundamental_quant",
    "src.instruments",
    "src.data",
)


def imported_modules(path: Path) -> list[tuple[int, str]]:
    """Resolve ordinary Python imports, including nested function imports."""
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    package = parts[:-1]
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                prefix = ".".join(base)
                module = prefix + ("." + node.module if node.module else "")
            else:
                module = node.module or ""
            imports.append((node.lineno, module))
            imports.extend(
                (node.lineno, module + "." + alias.name) for alias in node.names
            )
    return imports


@pytest.mark.parametrize("package", PORTABLE_PACKAGES)
def test_portable_libraries_do_not_import_the_application(package):
    directory = ROOT / "src" / package
    files = sorted(directory.rglob("*.py"))
    assert files, f"portable package is not implemented: {package}"
    violations = []
    for path in files:
        for line, module in imported_modules(path):
            if any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in APPLICATION_PREFIXES
            ):
                violations.append(
                    f"{path.relative_to(ROOT).as_posix()}:{line} -> {module}"
                )
    assert not violations, "application dependency leaked into library:\n" + "\n".join(
        violations
    )


def test_old_execution_import_resolves_to_the_same_runtime_module():
    """Compatibility must not leave another independently patched runtime."""
    portable = importlib.import_module("src.task_runtime.execution")
    compatibility = importlib.import_module("src.core.execution")
    assert compatibility is portable
    assert compatibility.ExecutionStore is portable.ExecutionStore
    assert compatibility.ExecutionRuntime is portable.ExecutionRuntime


def test_a_second_project_cannot_raise_the_persisted_lane_limit(tmp_path):
    """Sharing a database is insufficient if a new claimant can pass 20 vs 1."""
    from src.task_runtime.execution import (
        ExecutionRuntime,
        ExecutionStore,
        JobSpec,
        ModuleSpec,
        RunResult,
    )

    database = tmp_path / "authority" / "execution.sqlite3"
    first_store = ExecutionStore(database)
    second_store = ExecutionStore(database)
    first = ExecutionRuntime(first_store, {"shared-model": 1})
    second = ExecutionRuntime(second_store, {"shared-model": 20})
    try:
        for runtime, name in ((first, "first"), (second, "second")):
            job = JobSpec(
                name + ".invoke",
                lambda payload, context: RunResult(),
                lane="shared-model",
            )
            runtime.register(ModuleSpec(name, (job,)))
            runtime.submit(job.id, {}, request_id=name)
        active = first_store.claim(
            first.owner_id, "shared-model", ["first.invoke"], lane_capacity=1
        )
        assert active is not None
        assert (
            second_store.claim(
                second.owner_id,
                "shared-model",
                ["second.invoke"],
                lane_capacity=20,
            )
            is None
        )
    finally:
        first.stop(timeout=1)
        second.stop(timeout=1)
        first_store.close_idle_connections()
        second_store.close_idle_connections()


def test_two_project_roots_share_only_when_the_authority_is_explicit(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TRADE_EYES_AUTHORITY_ROOT", raising=False)
    first = tmp_path / "project-a"
    second = tmp_path / "project-b"
    assert authority_for(first).authority_id != authority_for(second).authority_id
    shared = tmp_path / "authority with spaces"
    configured = {"storage": {"authority_root": str(shared)}}
    a = authority_for(first, configured)
    b = authority_for(second, configured)
    assert a.authority_id == b.authority_id
    assert a.execution_db == b.execution_db
    assert a.requests_db == b.requests_db
    assert a.models_db == b.models_db
    assert a.materials_root == b.materials_root


def test_authority_selection_is_host_composition_not_working_directory(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    deployment = tmp_path / "deployment"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.setenv("TRADE_EYES_AUTHORITY_ROOT", str(deployment))
    before = authority_for(project)
    monkeypatch.chdir(unrelated)
    after = authority_for(project)
    assert before == after
    assert after.root == deployment


def test_explicit_authority_cannot_be_overridden_by_project_or_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TRADE_EYES_AUTHORITY_ROOT", str(tmp_path / "environment"))
    explicit = tmp_path / "explicit"
    selected = authority_for(
        tmp_path / "project",
        {"storage": {"authority_root": str(tmp_path / "configuration")}},
        explicit_root=explicit,
    )
    assert selected.root == explicit


def test_scoped_materials_do_not_split_shared_request_and_account_capacity(tmp_path):
    public = AuthorityRef(tmp_path / "deployment", scope="public")
    private = AuthorityRef(tmp_path / "deployment", scope="private-project-a")
    assert public.materials_root != private.materials_root
    assert public.authority_id == private.authority_id
    assert public.requests_db == private.requests_db
    assert public.execution_db == private.execution_db
    assert public.models_db == private.models_db
    # This is logical separation within one trusted host, not an OS ACL claim.
    assert private.to_dict()["scope"] == "private-project-a"


def test_authority_contract_is_a_value_and_does_not_open_live_storage(tmp_path):
    directory = tmp_path / "not-yet-opened"
    authority = AuthorityRef(directory)
    assert authority.to_dict()["authority_id"] == authority.authority_id
    assert not directory.exists()


@pytest.mark.parametrize("scope", ["", "../public", "a/b", "a\\b", "secret\nkey"])
def test_scope_cannot_escape_its_material_namespace(tmp_path, scope):
    with pytest.raises(ValueError, match="scope"):
        AuthorityRef(tmp_path / "deployment", scope=scope)


def test_portable_authority_requires_an_explicit_absolute_path():
    with pytest.raises(ValueError, match="absolute"):
        AuthorityRef(Path("implicit-project-cache"))


def test_an_explicit_official_revision_cannot_inherit_an_unversioned_url(tmp_path):
    from src.data_hub.materials import DataHub

    authority = AuthorityRef(tmp_path / "authority")
    hub = DataHub(authority)
    original = tmp_path / "old.pdf"
    old_bytes = b"%PDF-1.7 old annual report bytes"
    new_bytes = b"%PDF-1.7 officially revised report bytes"
    original.write_bytes(old_bytes)
    url = "https://static.cninfo.com.cn/annual-reused-url.pdf"
    hub.register_original(original, url=url)
    downloads = []

    def download(target):
        downloads.append(str(target))
        target.write_bytes(new_bytes)

    revised = hub.ensure_original(
        url,
        download,
        version="official-amendment-2025-v2",
        content_type="application/pdf",
    )
    assert len(downloads) == 1
    assert revised["sha256"] == hashlib.sha256(new_bytes).hexdigest()
    assert original.read_bytes() == old_bytes


@pytest.mark.parametrize("kind", ["amendment", "annual"])
def test_research_adapter_does_not_override_hub_revision_separation(tmp_path, kind):
    from src.research_pipeline.models import AnnualDocument
    from src.research_pipeline.sources import SourceService

    path = tmp_path / "old.pdf"
    raw = b"%PDF-1.7 original annual"
    path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    old = AnnualDocument(
        "a_share:600938",
        "a_share:600938",
        2024,
        sha,
        url="https://static.cninfo.com.cn/shared-filing-url.pdf",
        published_at="2025-03-01T00:00:00+08:00",
        kind="annual",
        local_path=str(path),
        sha256=sha,
    )
    newly_discovered = AnnualDocument(
        "a_share:600938",
        "a_share:600938",
        2024,
        "cninfo:official-new-revision",
        url=old.url,
        published_at="2025-09-01T00:00:00+08:00",
        kind=kind,
    )
    assert SourceService._reuse_candidate(newly_discovered, [old]) is None


def test_later_throttling_cannot_relax_a_permanent_model_access_block(tmp_path):
    from src.model_gateway.ledger import GatewayBlocked, GatewayLedger

    ledger = GatewayLedger(AuthorityRef(tmp_path / "authority"))
    authorization = ledger.authorize("approved-campaign", "1")
    ledger.block("same-account", "http_403")
    ledger.block("same-account", "http_429", retry_after=0.001)
    time.sleep(0.02)
    with pytest.raises(GatewayBlocked, match="blocked"), ledger.claim(
        "never-sent",
        authorization=authorization,
        producer="project-b",
        request_sha256="frozen-request-sha",
        account="same-account",
        reservation="0.01",
        capture=tmp_path / "capture",
    ):
        pytest.fail("a new producer bypassed the original permanent block")


def test_public_material_reference_cannot_read_a_private_scope_by_sha(tmp_path):
    from src.data_hub.materials import DataHub, MaterialError

    root = tmp_path / "authority"
    private = DataHub(AuthorityRef(root, scope="private-project-a"))
    public = DataHub(AuthorityRef(root, scope="public"))
    reference = private.import_bytes(b"a private derived research document")
    with pytest.raises(MaterialError, match="scope"):
        public.resolve(reference["sha256"])
    forged = tmp_path / "untrusted-reference.json"
    forged.write_text(
        json.dumps(
            {
                "material_reference": 1,
                "authority_id": public.authority.authority_id,
                "scope": "public",
                "sha256": reference["sha256"],
                "path": reference["path"],
                "kind": "text",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MaterialError, match="scope"):
        public.resolve_reference(forged)


def test_frozen_runtime_does_not_create_a_second_material_authority(tmp_path):
    from src.research_pipeline.models import ResearchSpec
    from src.research_pipeline.sources import SourceService

    spec = ResearchSpec(
        ("a_share:600938",),
        output=tmp_path / "run-output",
        repo=tmp_path / "original-project",
        store_root=tmp_path / "research-state",
        authority_root=tmp_path / "shared-deployment",
    )
    restored = ResearchSpec.from_dict(spec.to_dict())
    restored.repo = tmp_path / "run-output" / "frozen_runtime"
    store = SimpleNamespace(root=spec.store_root)
    original = SourceService(spec, store)
    frozen = SourceService(restored, store)
    assert original.authority == frozen.authority
    assert original.hub.root == frozen.hub.root
    assert restored.credential_root == spec.credential_root
    assert not (restored.repo / "data/shared_platform").exists()


def test_unknown_model_attempt_cannot_gain_a_new_producer_on_another_project(
    tmp_path,
):
    from src.model_gateway.ledger import ExistingAttempt, GatewayLedger

    authority = AuthorityRef(tmp_path / "authority")
    first = GatewayLedger(authority)
    initial_auth = first.authorize("first-approved-run", "1")
    lease = first.claim(
        "stable-financial-block",
        authorization=initial_auth,
        producer="project-a",
        request_sha256="same-frozen-request",
        account="account-fingerprint",
        reservation="0.1",
        capture=tmp_path / "original-capture",
    )
    lease.mark_sent()
    lease.close()
    assert first.record("stable-financial-block")["status"] == "unknown"
    second = GatewayLedger(authority)
    new_auth = second.authorize("second-approved-run", "1")
    with pytest.raises(ExistingAttempt) as error:
        second.claim(
            "stable-financial-block",
            authorization=new_auth,
            producer="project-b",
            request_sha256="same-frozen-request",
            account="account-fingerprint",
            reservation="0.1",
            capture=tmp_path / "new-capture",
        )
    assert error.value.record["producer"] == "project-a"
    assert second.totals()["unknown_reserved_usd_not_spend"] == "0.1"
    assert second.totals(new_auth)["sent"] == 0


def test_model_admission_reclaims_a_proven_dead_producers_slot(tmp_path):
    from src.model_gateway.ledger import GatewayLedger

    authority = AuthorityRef(tmp_path / "authority")
    first = GatewayLedger(authority, capacity=1)
    # The waiting caller already exists before the other producer disappears.
    second = GatewayLedger(authority, capacity=20)
    authorization = first.authorize("one-approved-campaign", "1")
    lost = first.claim(
        "lost-producer",
        authorization=authorization,
        producer="project-a",
        request_sha256="old-request",
        account="account",
        reservation="0.1",
        capture=tmp_path / "lost",
    )
    lost.mark_sent()
    # Simulate only the process boundary. PID cannot exist on supported hosts;
    # no real process is killed and no request was made.
    with first.connection() as db:
        db.execute(
            "UPDATE model_attempts SET owner_pid=?,owner_birth=0 WHERE intent=?",
            (1073741823, first._intent("lost-producer")),
        )
    next_lease = second.claim(
        "never-sent-next-task",
        authorization=authorization,
        producer="project-b",
        request_sha256="new-request",
        account="account",
        reservation="0.1",
        capture=tmp_path / "next",
        deadline=time.monotonic() + 3,
    )
    next_lease.close()
    old = first.record("lost-producer")
    assert old["status"] == "unknown"
    assert old["active"] == 0
    assert old["sent"] == 1


@pytest.mark.parametrize("kind", ["live-owner", "unreleased-claim"])
def test_first_shared_binding_refuses_a_still_owned_legacy_execution_queue(
    tmp_path, kind
):
    from src.research_pipeline.store import Store
    from src.task_runtime.execution import JobSpec, ModuleSpec, RunResult

    store = Store(tmp_path / "legacy-research")
    previous = store.execution
    owner = "old-worker"
    if kind == "live-owner":
        assert previous.owner_started(owner)
    else:
        previous.apply(
            ModuleSpec(
                "old-financial-module",
                (
                    JobSpec(
                        "old-model-invoke",
                        lambda payload, context: RunResult(),
                        lane="research_llm",
                    ),
                ),
            )
        )
        previous.submit("old-model-invoke", {}, request_id="original-request")
        assert (
            previous.claim(
                owner,
                "research_llm",
                ["old-model-invoke"],
                starting_side_effects={"old-model-invoke": "provider_post_intent"},
            )
            is not None
        )
    try:
        with pytest.raises(ValueError):
            store.bind_execution_authority(AuthorityRef(tmp_path / "authority"))
        assert store.execution.path == store.db_path
    finally:
        previous.owner_stopped(owner)
        previous.close_idle_connections()
        store.execution.close_idle_connections()


def test_first_shared_binding_keeps_old_unsent_queue_rows_unchanged(tmp_path):
    from src.research_pipeline.store import Store
    from src.task_runtime.execution import JobSpec, ModuleSpec, RunResult

    store = Store(tmp_path / "legacy-research")
    previous = store.execution
    previous.apply(
        ModuleSpec(
            "old-financial-module",
            (JobSpec("old-model-invoke", lambda payload, context: RunResult()),),
        )
    )
    old_id = previous.submit(
        "old-model-invoke", {"financial_value": "123.45"}, request_id="old-request"
    )
    before = previous.get(old_id)
    authority = AuthorityRef(tmp_path / "authority")
    store.bind_execution_authority(authority)
    assert store.execution.path == authority.execution_db
    assert store.execution.get(old_id) is None
    assert previous.get(old_id) == before
    reopened = Store(store.root)
    assert reopened.execution.path == authority.execution_db
    assert previous.get(old_id) == before
    previous.close_idle_connections()
    store.execution.close_idle_connections()
    reopened.execution.close_idle_connections()


def test_authority_http_refuses_automatic_redirect_policy_bypass(tmp_path):
    from src.data_hub.http import AuthorityHTTP

    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, content=b"{}", headers={})

    client = AuthorityHTTP(
        AuthorityRef(tmp_path / "authority"),
        http=SimpleNamespace(get=get),
        minimum_interval=0,
    )
    with pytest.raises(ValueError, match="redirect"):
        client.get("https://static.cninfo.com.cn/metadata", allow_redirects=True)
    assert calls == []


def test_authority_http_bounds_real_timeout_by_remaining_deadline(tmp_path):
    from src.data_hub.http import AuthorityHTTP

    calls = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, content=b"{}", headers={})

    client = AuthorityHTTP(
        AuthorityRef(tmp_path / "authority"),
        http=SimpleNamespace(get=get),
        minimum_interval=0,
    )
    client.get(
        "https://static.cninfo.com.cn/metadata",
        timeout=(10, 60),
        deadline=time.monotonic() + 3,
    )
    assert len(calls) == 1
    connect, read = calls[0]["timeout"]
    assert 0 < connect <= 3
    assert 0 < read <= 3
