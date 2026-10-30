"""Hash-bound local migration evidence; no deployment or channel operations."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADDED = (
    "src/core/execution.py",
    "src/core/execution_health.py",
    "src/core/report_execution.py",
    "src/core/report_cli.py",
    "src/core/runtime_files.py",
    "src/interactive/ingress.py",
    "src/interactive/assistant/runtime_support.py",
    "src/personal_monitor/tasks.py",
    "src/personal_monitor/history.py",
    "src/notification/receipts.py",
    "scripts/personal_monitor_shared_test.py",
    "scripts/benchmark_shared_runtime.py",
    "scripts/pack_shared_runtime_benchmark.py",
    "scripts/audit_shared_runtime.py",
    "tests/test_shared_execution.py",
    "tests/test_execution_health.py",
    "tests/test_report_execution.py",
    "tests/test_runtime_files.py",
    "tests/interactive/test_shared_conversation.py",
    "tests/interactive/test_shared_conversation_regressions.py",
    "tests/personal_monitor/test_registered_tasks.py",
    "tests/personal_monitor/test_position_history.py",
    "tests/personal_monitor/test_correction_ux.py",
    "tests/personal_monitor/test_manual_correction_semantics.py",
    "tests/personal_monitor/test_manual_confirmation_sources.py",
    "tests/notification/test_channel_receipts.py",
    "docs/development/shared_runtime_migration.md",
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def review_lines(data):
    return data.decode("utf8").replace("\r\n", "\n").splitlines(keepends=True)


def ruff(paths, root):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format=json",
            "--config",
            str(ROOT / "pyproject.toml"),
            *map(str, paths),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf8",
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("Ruff could not complete")
    findings = json.loads(result.stdout)
    for item in findings:
        item["filename"] = Path(item["filename"]).relative_to(root).as_posix()
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    if not evidence.is_relative_to(ROOT / "data" / "acceptance"):
        raise ValueError("Evidence must stay inside the repository acceptance folder")
    baseline = json.loads((evidence / "baseline.json").read_text(encoding="utf8"))
    files = sorted(set(baseline["source_sha256"]) | set(ADDED))
    manifest, patch, changed = {}, [], []
    for name in files:
        current, before = ROOT / name, evidence / "before" / name
        old = before.read_bytes() if before.is_file() else None
        if not current.is_file():
            manifest[name] = {
                "sha256": None,
                "baseline_sha256": sha(old) if old is not None else None,
                "changed": old is not None,
                "deleted": old is not None,
                "bytes": 0,
            }
            if old is not None:
                changed.append(name)
                patch.extend(
                    difflib.unified_diff(
                        review_lines(old),
                        [],
                        fromfile="before/" + name,
                        tofile="/dev/null",
                    )
                )
            continue
        after_bytes = current.read_bytes()
        manifest[name] = {
            "sha256": sha(after_bytes),
            "baseline_sha256": sha(old) if old is not None else None,
            "changed": old != after_bytes,
            "bytes": len(after_bytes),
        }
        if old == after_bytes:
            continue
        changed.append(name)
        target = evidence / "after" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(current, target)
        patch.extend(
            difflib.unified_diff(
                review_lines(old or b""),
                review_lines(after_bytes),
                fromfile="before/" + name if old is not None else "/dev/null",
                tofile="after/" + name,
            )
        )
    result = {
        "at": datetime.now(timezone.utc).isoformat(),
        "files": manifest,
        "changed": changed,
        "production_deployed": False,
        "scope_is_full_worktree_closure": False,
        "scope_contract": "Explicit related M01 paths plus ADDED; unlisted work is not certified or included",
        "overlay_contract": "Only apply against the exact M01 before hashes; not a production deployment package",
        "diff_contract": "LF-normalized logical review diff; exact after/ bytes and SHA256 remain authoritative",
    }
    (evidence / "scope_manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8"
    )
    (evidence / "worktree_overlay.patch").write_bytes("".join(patch).encode("utf8"))
    current_py = [
        ROOT / name
        for name in changed
        if name.endswith(".py") and (ROOT / name).is_file()
    ]
    old_py = [
        evidence / "before" / name
        for name in changed
        if name.endswith(".py") and (evidence / "before" / name).is_file()
    ]
    findings = {
        "at": result["at"],
        "current": ruff(current_py, ROOT),
        "baseline": ruff(old_py, evidence / "before"),
    }
    (evidence / "ruff_scope.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(
        json.dumps(
            {
                "changed_files": len(changed),
                "ruff_current": len(findings["current"]),
                "ruff_baseline": len(findings["baseline"]),
            }
        )
    )


if __name__ == "__main__":
    main()
