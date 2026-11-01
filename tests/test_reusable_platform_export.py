"""Actually load the exported libraries from another project's namespace."""

import json
import os
import subprocess
import sys

import pytest

from scripts.export_reusable_platform import export_platform


def test_foreign_project_import_and_local_state_without_application(tmp_path):
    output = tmp_path / "another-project"
    manifest = export_platform(output, "another_platform")
    assert manifest["packages"] == [
        "shared_contracts",
        "task_runtime",
        "data_hub",
        "model_gateway",
        "agent_runtime",
    ]
    assert not (output / "src").exists()
    program = """
import sys
from pathlib import Path
from another_platform.shared_contracts import AuthorityRef
from another_platform.task_runtime import ExecutionStore
from another_platform.task_runtime.request_policy import RequestPolicy
from another_platform.data_hub import DataHub
from another_platform.model_gateway.ledger import GatewayLedger
from another_platform.agent_runtime import ConversationHarness
authority=AuthorityRef(Path.cwd()/"explicit-authority")
assert DataHub(authority).authority.authority_id == authority.authority_id
assert ExecutionStore(authority.execution_db).status()["counts"] == {}
RequestPolicy(authority).wait("offline-fixture",min_interval=0)
assert GatewayLedger(authority).totals()["sent"] == 0
assert not any(n=="src" or n.startswith(("src.","scripts.")) for n in sys.modules)
print("foreign namespace and explicit shared state passed; model POSTs=0")
"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", program],
        cwd=output,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "POSTs=0" in result.stdout
    assert json.loads((output / "manifest.json").read_text())["model_calls"] == 0
    with pytest.raises(FileExistsError):
        export_platform(output, "another_platform")


@pytest.mark.parametrize("name", ["../outside", "with.dot", "class", ""])
def test_export_does_not_allow_path_escape_or_overwrite(tmp_path, name):
    with pytest.raises(ValueError):
        export_platform(tmp_path / "export", name)
    assert not (tmp_path / "export").exists()
