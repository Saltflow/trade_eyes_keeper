"""Financial domain projections must not create a second execution authority."""

import json

import pytest

from src.research_pipeline.models import ResearchSpec
from src.research_pipeline.store import Store
from src.shared_contracts import AuthorityRef


def spec(tmp_path, project, authority):
    return ResearchSpec(
        companies=("a_share:600938",),
        repo=tmp_path / project,
        output=tmp_path / project / "run",
        store_root=tmp_path / project / "domain",
        authority_root=authority.root,
        authority_isolated=authority.isolated,
    )


def test_two_domain_stores_share_real_execution_db(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    one, two = spec(tmp_path, "one", authority), spec(tmp_path, "two", authority)
    first, second = Store(one.store_root), Store(two.store_root)
    first.create_run(one.to_dict())
    second.create_run(two.to_dict())
    assert first.db_path != second.db_path
    assert first.execution.path == second.execution.path == authority.execution_db
    reopened = Store(one.store_root)
    assert reopened.execution.path == authority.execution_db
    assert reopened.execution_authority.authority_id == authority.authority_id


def test_domain_cannot_rebind_and_hide_prior_execution_state(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    configuration = spec(tmp_path, "project", authority)
    store = Store(configuration.store_root)
    run = store.create_run(configuration.to_dict())
    with pytest.raises(ValueError, match="authority_changed"):
        store.bind_execution_authority(AuthorityRef(tmp_path / "another-authority"))
    assert store.execution.path == authority.execution_db
    assert store.get_run(run["id"])["state"] == "created"


def test_historical_domain_queue_not_copied_or_claimed_on_binding(tmp_path):
    store = Store(tmp_path / "domain")
    old_path = store.execution.path
    # Freeze the old facts and test that binding merely points new execution at
    # the authority; no SELECT+resubmit or silent queue migration is allowed.
    old_status = json.dumps(store.execution.status(), sort_keys=True)
    store.bind_execution_authority(AuthorityRef(tmp_path / "authority"))
    assert store.execution.path != old_path
    from src.task_runtime import ExecutionStore

    assert json.dumps(ExecutionStore(old_path).status(), sort_keys=True) == old_status
    assert not store.execution.status()["counts"]
