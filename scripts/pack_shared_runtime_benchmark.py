"""Pack named, credential-free runtime sources for an isolated benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    files = [
        "src/__init__.py",
        "src/core/__init__.py",
        "src/core/config_store.py",
        "src/core/process_lock.py",
        "src/core/execution.py",
        "src/core/execution_health.py",
        "src/core/report_execution.py",
        "src/core/runtime_files.py",
        "src/core/runtime_service.py",
        "src/core/schedule_manager.py",
        "src/personal_monitor_runtime.py",
        "scripts/benchmark_shared_runtime.py",
    ]
    files.extend(
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "src/personal_monitor").glob("*.py"))
    )
    archive = evidence / "server_benchmark.tar.gz"
    manifest = {}
    with tarfile.open(archive, "w:gz") as tar:
        for relative in files:
            path = ROOT / relative
            if path.is_symlink():
                raise ValueError("source links cannot enter the benchmark")
            tar.add(path, arcname=relative, recursive=False)
            manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        for relative in (
            "src/core/runtime_service.py",
            "src/core/schedule_manager.py",
            "src/personal_monitor_runtime.py",
        ):
            path = evidence / "before" / relative
            name = "evidence/before/" + relative
            tar.add(path, arcname=name, recursive=False)
            manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (evidence / "server_package_manifest.json").write_text(
        json.dumps(
            {
                "files": manifest,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "credentials_included": False,
                "production_modified": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(str(archive))


if __name__ == "__main__":
    main()
