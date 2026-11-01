"""Export only portable source packages; no host, credentials or project data."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import keyword
from pathlib import Path

PACKAGES = (
    "shared_contracts",
    "task_runtime",
    "data_hub",
    "model_gateway",
    "agent_runtime",
)


def export_platform(output: Path, namespace="trade_eyes_platform", *, repo=None):
    if not namespace.isidentifier() or keyword.iskeyword(namespace):
        raise ValueError("valid_python_namespace_required")
    repo = Path(repo or Path(__file__).resolve().parents[1]).resolve()
    output = Path(output).resolve()
    selected = []
    for package in PACKAGES:
        directory = repo / "src" / package
        for path in sorted(directory.rglob("*.py")):
            if path.is_symlink() or not path.resolve().is_relative_to(directory):
                raise ValueError("portable_source_link_not_allowed")
            content = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(content)):
                if isinstance(node, ast.ImportFrom) and not node.level:
                    if (node.module or "").startswith(("src.", "scripts")):
                        raise ValueError("portable_source_application_import")
                elif isinstance(node, ast.Import) and any(
                    alias.name.startswith(("src.", "scripts")) for alias in node.names
                ):
                    raise ValueError("portable_source_application_import")
            selected.append((path, content.encode("utf-8")))
    found = {path.relative_to(repo / "src").parts[0] for path, _ in selected}
    if found != set(PACKAGES):
        raise ValueError("portable_package_missing")
    output.mkdir(parents=True, exist_ok=False)
    root = output / namespace
    root.mkdir()
    (root / "__init__.py").write_text(
        '"""Portable source, model and execution ports; compose an explicit authority."""\n',
        encoding="utf-8",
    )
    hashes = {}
    for path, raw in selected:
        target = root / path.relative_to(repo / "src")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        hashes[target.relative_to(output).as_posix()] = hashlib.sha256(raw).hexdigest()
    (output / "requirements.txt").write_text(
        "requests>=2.31\npsutil>=5.9,<8\nbeautifulsoup4>=4.14\n"
        "pypdfium2>=4.18\ntokenizers>=0.20,<1\n",
        encoding="utf-8",
    )
    manifest = {
        "namespace": namespace,
        "packages": list(PACKAGES),
        "files": hashes,
        "model_calls": 0,
        "includes_credentials_or_business_data": False,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "README.md").write_text(
        "# Portable local platform\n\nSource-only export; dependencies are not installed automatically.\n\n"
        f"Import `{namespace}.shared_contracts.AuthorityRef`, `{namespace}.data_hub.DataHub`, "
        f"`{namespace}.task_runtime.ExecutionRuntime`, `{namespace}.agent_runtime.ConversationHarness`.\n\n"
        "Pass the same explicit absolute authority root to trusted projects on one machine. "
        "Development and production use separate roots, credentials, queues and budgets. "
        "No financial contracts, Feishu bridge, project configuration or historical data are included. "
        "Supply model/tool ports and a Pi bridge explicitly. Scope is trusted-SDK separation, not OS ACL.\n",
        encoding="utf-8",
    )
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--namespace", default="trade_eyes_platform")
    args = parser.parse_args(argv)
    manifest = export_platform(args.output, args.namespace)
    print(
        json.dumps(
            {"output": str(args.output.resolve()), "files": len(manifest["files"])},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
