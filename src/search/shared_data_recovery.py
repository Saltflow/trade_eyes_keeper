"""One bounded recovery of data-blocked markets after shared cache repairs.

No fetching occurs in the readiness probe. Original failed runs stay immutable;
an exclusive attempt receipt prevents replaying the same failed run on restart.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

from ..data.backtest_data import validate_market_bundle
from ..data.market_history import PointInTimeMarketStore

logger = logging.getLogger(__name__)


def cached_recovery_evidence(config: dict, receipt: dict) -> dict:
    issues = receipt.get("issues", [])
    if not issues or any(
        x.get("category") != "fetch_or_validation_failed"
        or x.get("source") == "fundamental"
        for x in issues
    ):
        raise ValueError("only market data preparation failures can be recovered")
    codes = set(receipt.get("ready_codes", [])) | {x["code"] for x in issues}
    cutoffs = receipt["market_cutoffs"]
    if not codes or codes != set(cutoffs):
        raise ValueError("readiness receipt does not cover the full requested universe")
    settings = config["point_in_time_data"]
    store = PointInTimeMarketStore(settings.get("output_dir", "data/point_in_time"))
    tolerance = int(
        (settings.get("market_history") or {}).get("coverage_tolerance_days", 31)
    )
    start = date.fromisoformat(receipt["requested_start"])
    result = {}
    for code in sorted(codes):
        end = date.fromisoformat(cutoffs[code]["effective_end"])
        bundle = store.read(code, config=config)
        validate_market_bundle(
            bundle,
            code,
            start,
            end,
            require_current=True,
            now=date.fromisoformat(receipt["requested_end"]),
            tolerance_days=tolerance,
        )
        stem = store._safe_code(code)
        result[code] = {
            "effective_end": end.isoformat(),
            "files": {
                suffix: hashlib.sha256(
                    (store.market_dir / f"{stem}{suffix}").read_bytes()
                ).hexdigest()
                for suffix in (".csv", ".actions.json", ".meta.json")
            },
        }
    return result


def recover_shared_data(
    config,
    reports,
    completed,
    market_configs,
    run_group,
    root=Path("data/optimizer/runs"),
):
    """Replace only the effective summary; retain failed run and attempt history."""
    effective = list(reports)
    for index, old in enumerate(reports):
        for group, summary in old.groups.items():
            if group in completed or summary.status != "no_data":
                continue
            run_id = old.run_ids_by_group.get(group) or old.run_id
            if not run_id or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
                continue
            folder = Path(root) / run_id
            attempt_path = folder / "shared_data_recovery.json"
            if attempt_path.exists():
                continue
            try:
                receipt = json.loads(
                    (folder / "data_readiness.json").read_text(encoding="utf-8")
                )
                evidence = cached_recovery_evidence(config, receipt)
            except Exception as exc:  # noqa: BLE001 - isolate failed market probes
                summary.ranking_diagnostics["shared_data_recovery_probe"] = str(exc)
                logger.warning(
                    "Shared-data recovery remains unavailable for %s: %s", group, exc
                )
                continue
            attempt = {
                "schema_version": 1,
                "original_run_id": run_id,
                "group": group,
                "status": "started",
                "attempt": 1,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "validated_cache": evidence,
            }
            try:
                with attempt_path.open("x", encoding="utf-8") as stream:
                    json.dump(attempt, stream, ensure_ascii=False, indent=2)
            except FileExistsError:
                continue
            logger.info(
                "Shared data recovered; retrying %s once after %s", group, run_id
            )
            retried = []
            try:
                result = run_group(
                    config, group, market_configs[group], report_sink=retried
                )
                if not retried or group not in retried[-1].groups:
                    raise RuntimeError("recovery did not return a market report")
                new = retried[-1]
                new.elapsed_seconds += old.elapsed_seconds
                new.groups[group].ranking_diagnostics["shared_data_recovery"] = {
                    "original_run_id": run_id,
                    "attempt": 1,
                    "receipt": str(attempt_path),
                }
                effective[index] = new
                completed.update(result)
                attempt.update(
                    status="completed" if group in result else "failed",
                    replacement_run_id=new.run_ids_by_group.get(group) or new.run_id,
                    failure_reason=new.failure_reason,
                )
            except Exception as exc:
                logger.exception("Shared-data recovery failed for %s", group)
                attempt.update(status="failed", failure_reason=str(exc))
                old.failure_reason += f"; shared-data recovery failed: {exc}"
                summary.ranking_diagnostics["shared_data_recovery"] = dict(attempt)
            attempt["finished_at"] = datetime.now(timezone.utc).isoformat()
            temporary = attempt_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(attempt, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(attempt_path)
    return effective
