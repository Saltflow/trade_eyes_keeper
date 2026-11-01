"""Source provenance for callers freezing portable model execution code."""

from pathlib import Path


def implementation_files():
    source_root = Path(__file__).resolve().parents[1]
    result = []
    for package in ("model_gateway", "task_runtime", "shared_contracts"):
        result.extend(sorted((source_root / package).glob("*.py")))
    host = source_root / "platform_composition.py"
    if host.is_file():
        result.append(host)
    return result
