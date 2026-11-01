"""Synchronous material producers use the deployment's durable shared lanes."""

from __future__ import annotations

import time
import uuid

from ..task_runtime.execution import (
    TERMINAL,
    ExecutionRuntime,
    ExecutionStore,
    JobSpec,
    ModuleSpec,
    RunResult,
)


class MaterialTaskUnknown(RuntimeError):
    """A live or interrupted handler has no trustworthy completed result."""


def run_material_task(authority, lane, callback, *, timeout=660):
    """Only register/claim this new producer; never revive another handler.

    Results stay in the material store, not in a second SQLite copy. A timeout
    marks uncertainty but the live handler retains its concurrency hold until
    it really returns. Original/download attempt records prevent future resend.
    """
    identifier = "material." + uuid.uuid4().hex
    store = ExecutionStore(authority.execution_db)
    runtime = ExecutionRuntime(store, {"material_download": 3, "material_parse": 1})
    results, errors = [], []

    def handle(payload, context):
        try:
            if context.cancel_event.is_set():
                return RunResult(
                    status="unknown", reason="material_cancelled_before_start"
                )
            context.progress(lane)
            results.append(callback())
            return RunResult()
        except BaseException as error:  # noqa: BLE001 - rethrow worker errors in the caller
            errors.append(error)
            return RunResult(status="failed", reason=type(error).__name__)

    runtime.register(
        ModuleSpec(
            identifier,
            (
                JobSpec(
                    identifier + ".produce",
                    handle,
                    lane=lane,
                    timeout_seconds=timeout,
                    starting_side_effect="material_producer_claimed",
                ),
            ),
        )
    )
    run_id = runtime.submit(
        identifier + ".produce",
        {"authority_id": authority.authority_id, "scope": authority.scope},
        source="data_hub",
    )
    try:
        while True:
            runtime.tick()
            row = runtime.get_run(run_id)
            if row["status"] in TERMINAL:
                if errors:
                    raise errors[0]
                if results:
                    return results[0]
                # A timed-out callback can still be active. Do not free its
                # capacity or pretend an absent material is a successful parse.
                if row["status"] != "succeeded":
                    raise MaterialTaskUnknown("material_task_" + row["status"])
                raise RuntimeError("material_producer_returned_no_result")
            time.sleep(0.01)
    finally:
        runtime.stop(timeout=1)
