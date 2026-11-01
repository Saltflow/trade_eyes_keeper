"""Single-attempt model calls through the existing shared execution runtime."""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from tokenizers import Tokenizer

from src.core.execution import ExecutionRuntime, JobSpec, ModuleSpec, RunResult
from src.model_gateway import transport
from src.model_gateway.integrity import implementation_files as gateway_files
from src.model_gateway.ledger import GatewayLedger, account_id
from src.platform_composition import authority_for

from . import common, integrity, models
from . import store as store_module
from .common import atomic_write, file_sha, fingerprint, load, now
from .models import ResearchSpec
from .store import money

_ACCOUNT_LOCK = threading.Lock()
_ACCOUNT_LOCKS = {}
_ACCOUNT_CACHE = {}


class IntegrityGateError(RuntimeError):
    """Local authority or storage failed before a new provider side effect."""


class ModelExecutor:
    """Budgeted, globally bounded POSTs; reusing an attempt never sends again.

    ``post`` and ``account_reader`` are injectable for offline acceptance. Mock
    POSTs never consult the real account or environment credentials. An account
    reader returns the public balance response (or an available USD amount).
    """

    def __init__(
        self,
        store,
        spec,
        run_id,
        post=None,
        key=None,
        rates=None,
        event_callback=None,
        account_reader=None,
    ):
        self.store = store
        self.spec = (
            spec if isinstance(spec, ResearchSpec) else ResearchSpec.from_dict(spec)
        )
        self.run_id, self.post, self.key = run_id, post, key
        self.event_callback, self.account_reader = event_callback, account_reader
        self._runtime = None
        self._lock = threading.RLock()
        self._arguments = {}
        self._code_snapshot_files = None
        self._closed = False
        self._identity = uuid.uuid4().hex
        self._gateway = None
        self._job = "research.model." + self._identity
        self._module = "research_pipeline." + self._identity
        config_path = self.spec.config or self.spec.repo / "config/research_report.json"
        self.config = load(config_path) if Path(config_path).is_file() else {}
        self.rates = dict(rates or self.config.get("execution_rates", {}))
        self._peak_rates = self.rates.pop(
            "peak_rates", self.config.get("peak_rates", {})
        )
        self._tariff_phase = self.rates.pop("_tariff_phase", None)
        self._account_tariff_ratio = self.rates.pop("_account_tariff_ratio", None)
        if self._account_tariff_ratio is not None:
            self._account_tariff_ratio = money(self._account_tariff_ratio)
            if not self._account_tariff_ratio:
                raise ValueError("account_tariff_ratio_must_be_positive")
        self._pricing_dates = self.rates.pop(
            "pricing_utc_dates", self.config.get("pricing_utc_dates", [])
        )
        for name in ("input", "output", "cached_input"):
            if name in self.rates:
                self.rates[name] = str(money(self.rates[name]))
        self._peak_rates = {
            name: str(money(value)) for name, value in self._peak_rates.items()
        }
        # A mock exercises the normal size, money and ownership guards without
        # pretending the repository's historical price window is current.
        if post is not None and not self._pricing_dates:
            self._pricing_dates = [datetime.now(timezone.utc).date().isoformat()]
        self._implementation = [
            Path(__file__),
            Path(store_module.__file__),
            Path(common.__file__),
            Path(integrity.__file__),
            Path(models.__file__),
            Path(transport.__file__),
            *gateway_files(),
        ]
        self._import_hashes = {
            str(p.resolve()): file_sha(p) for p in self._implementation
        }

    def _emit(self, name, **fields):
        event = self.store.event(self.run_id, name, **fields)
        if self.event_callback:
            try:
                self.event_callback(event)
            except Exception as error:  # noqa: BLE001 - optional presentation/transport hook
                # An optional UI or outbound transport cannot break paid work.
                self.store.event(
                    self.run_id,
                    "event_callback_failed",
                    callback_event=name,
                    error_type=type(error).__name__,
                    reason=self._reason(error),
                )
        return event

    def _reason(self, error):
        from .observability import redact

        reason = str(error)
        if self.key:
            reason = reason.replace(self.key, "[REDACTED]")
        return redact(reason)[:2000]

    def _blocked(self, reason, task_key, capture_dir=""):
        result = {
            "id": task_key,
            "status": "not_sent",
            "reason": reason,
            "error_type": reason,
            "capture_dir": str(capture_dir),
            "reused": False,
            "usage": None,
            "cost": {"usd": None, "kind": "not_sent"},
            "automatic_retries": 0,
            "simulated": self.post is not None,
        }
        self._emit("dispatch_blocked", task_key=task_key, reason=reason)
        return result

    def _reuse(self, record):
        receipt = dict(
            record.get("receipt")
            or {
                "status": record.get("status", "unknown"),
                "usage": None,
                "cost": {"usd": None, "kind": "usage_unknown"},
            }
        )
        receipt.update(
            capture_dir=record.get("capture_dir", ""),
            reused=True,
            producer_run_id=record.get("producer_run"),
            historical=bool(record.get("historical")),
            charged_to_current_run=False,
        )
        self._emit(
            "attempt_reused",
            task_key=record.get("key"),
            status=receipt["status"],
            producer_run_id=record.get("producer_run"),
        )
        return receipt

    def _wait_existing(self, task_key, deadline=None):
        started = time.monotonic()
        while True:
            row = self.store.get_attempt(task_key)
            if row and row["status"] not in {"queued", "sending"}:
                return self._reuse(row)
            self.store.reconcile()
            if (
                deadline is not None and time.monotonic() >= deadline
            ) or time.monotonic() - started > float(
                self.config.get("consumer_wait_seconds", 7200)
            ):
                return {
                    "id": task_key,
                    "status": "unknown",
                    "reason": "producer_still_active",
                    "capture_dir": (row or {}).get("capture_dir", ""),
                    "reused": True,
                    "usage": None,
                    "cost": {"usd": None, "kind": "usage_unknown"},
                }
            time.sleep(0.05)

    def _key(self):
        if self.post is not None:
            return self.key or "offline-injected-transport"
        if self.key:
            return self.key
        from dotenv import dotenv_values

        self.key = os.getenv("DEEPSEEK_API_KEY") or dotenv_values(
            Path(getattr(self.spec, "credential_root", None) or self.spec.repo)
            / "config/.env"
        ).get("DEEPSEEK_API_KEY")
        return self.key

    def _provider_id(self):
        return fingerprint(
            {
                "endpoint": transport.ENDPOINT,
                "credential_sha256": fingerprint(self._key()),
            }
        )

    def _pricing_changed(self):
        if self.post is not None:
            return False
        from .pricing import tariff_phase

        return datetime.now(
            timezone.utc
        ).date().isoformat() not in self._pricing_dates or (
            self._tariff_phase is not None and tariff_phase() != self._tariff_phase
        )

    def _model_gateway(self):
        if self._gateway is None:
            configured = getattr(self.spec, "authority_root", None) or self.config.get(
                "authority_root"
            )
            authority = authority_for(
                self.spec.repo,
                explicit_root=configured
                or (
                    self.store.root / "gateway_authority"
                    if self.post is not None
                    else None
                ),
                isolated=getattr(self.spec, "authority_isolated", False)
                or self.post is not None
                and not configured,
            )
            self._gateway = GatewayLedger(authority)
        return self._gateway

    def _provider_gate(self, task_key, capture_dir=""):
        block = self.store.provider_block(
            self._provider_id()
        ) or self._model_gateway().provider_block(
            account_id(transport.ENDPOINT, self._key())
        )
        if block:
            result = self._blocked("provider_blocked", task_key, capture_dir)
            result["provider_block"] = block
            return result
        return None

    def _shared_attempt(self, task_key, deadline):
        """Reuse a declared domain task across stores sharing one authority.

        Domain task identity, not a new prompt/model release, owns validity.
        Verify the captured original request, not the newly proposed request.
        """
        gateway = self._model_gateway()
        record = gateway.record("research:" + task_key)
        started = time.monotonic()
        while record and record["active"]:
            if (deadline is not None and time.monotonic() >= deadline) or (
                time.monotonic() - started
                > float(self.config.get("consumer_wait_seconds", 7200))
            ):
                return {
                    "id": task_key,
                    "status": "unknown",
                    "reason": "producer_still_active",
                    "capture_dir": record["capture"],
                    "usage": None,
                    "cost": {"usd": None, "kind": "usage_unknown"},
                    "reused": True,
                    "charged_to_current_run": False,
                    "producer_run_id": record["producer"],
                }
            gateway.reconcile()
            time.sleep(0.05)
            record = gateway.record("research:" + task_key)
        if not record or not record["sent"]:
            return None
        receipt = json.loads(record.get("receipt") or "{}")
        capture = Path(record["capture"])
        if receipt.get("status") in {"complete", "partial"}:
            try:
                original = load(capture / "request.json")
                if fingerprint(original) != record["request_sha256"]:
                    raise ValueError("shared_request_changed")
                if file_sha(capture / "content.md") != receipt["content_sha256"]:
                    raise ValueError("shared_content_changed")
            except (OSError, ValueError, KeyError, TypeError):
                return {
                    "id": task_key,
                    "status": "failed",
                    "reason": "artifact_unavailable",
                    "capture_dir": str(capture),
                    "usage": receipt.get("usage"),
                    "cost": receipt.get("cost")
                    or {"usd": None, "kind": "usage_unknown"},
                    "reused": True,
                    "charged_to_current_run": False,
                    "producer_run_id": record["producer"],
                }
        return {
            **receipt,
            "id": task_key,
            "status": receipt.get("status", record["status"]),
            "capture_dir": str(capture),
            "reused": True,
            "producer_run_id": record["producer"],
            "charged_to_current_run": False,
        }

    def _block_response(self, status, headers, capture_dir=""):
        raw_retry = headers.get("Retry-After") or headers.get("retry-after")
        retry = None
        if raw_retry is not None:
            try:
                retry = float(raw_retry)
                if not math.isfinite(retry) or retry < 0:
                    retry = None
            except (ValueError, TypeError):
                try:
                    retry = max(
                        0,
                        (
                            parsedate_to_datetime(raw_retry)
                            - datetime.now(timezone.utc)
                        ).total_seconds(),
                    )
                except (ValueError, TypeError, OverflowError):
                    retry = None
        block = self.store.block_provider(
            self._provider_id(), status, capture_dir, retry
        )
        if self._key():
            self._model_gateway().block(
                account_id(transport.ENDPOINT, self._key()),
                "http_" + str(status),
                retry,
            )
        self._emit("provider_blocked", **block)
        if status in {401, 402, 403}:
            self.store.update_run(
                self.run_id, state="source_blocked", requires_resume=True
            )
        return block

    def _estimate(self, request):
        path = Path(
            self.config.get(
                "tokenizer", "data/fundamental_quant/tokenizer/tokenizer.json"
            )
        )
        path = path if path.is_absolute() else self.spec.repo / path
        settings = {
            "message_token_reserve": 4096,
            "image_token_reserve_each": 1024,
            **self.config,
        }
        count = transport.estimate(request, Tokenizer.from_file(str(path)), settings)
        output = int(request.get("max_tokens", 0))
        if output <= 0 or output > 131072:
            raise ValueError("invalid_output_quota")
        total = (
            int(Decimal(count) * Decimal("1.10") + Decimal(".999999")) + output + 8192
        )
        if total > int(self.config.get("hard_context_limit", 1000000)):
            raise ValueError("hard_context_exceeded_no_truncation")
        if len(
            __import__("json")
            .dumps(request, ensure_ascii=False, indent=2)
            .encode("utf-8")
        ) > int(self.config.get("maximum_request_bytes", 50 * 1024 * 1024)):
            raise ValueError("hard_request_body_exceeded_no_truncation")
        peak = {
            "input": max(
                money(self._peak_rates.get("input", self.rates["input"])),
                money(self.rates["input"]),
                money(self.rates["cached_input"]),
            ),
            "output": max(
                money(self._peak_rates.get("output", self.rates["output"])),
                money(self.rates["output"]),
            ),
        }
        reserve = (
            Decimal(self.config.get("hard_context_limit", 1000000)) * peak["input"]
            + Decimal(output) * money(peak["output"])
        ) / 1000000
        return path, count, reserve

    def _funds(self, key, deadline=None):
        if self.post is not None and self.account_reader is None:
            return None
        if self.account_reader is not None:
            return self._read_funds(key, deadline)
        scope = (str(self.store.db_path), self._provider_id())
        cache_scope = (*scope, str(self._account_tariff_ratio))
        with _ACCOUNT_LOCK:
            lock = _ACCOUNT_LOCKS.setdefault(scope, threading.Lock())
        while not lock.acquire(timeout=0.1):
            if deadline is not None and time.monotonic() >= deadline:
                raise requests.ReadTimeout("waiting_for_shared_balance_query")
        try:
            cached = _ACCOUNT_CACHE.get(cache_scope, {})
            if cached.get("failure_until", 0) > time.monotonic():
                raise requests.ReadTimeout("shared_balance_query_backoff")
            known, unknown = self.store.provider_usage(self._provider_id())
            if "available" in cached and time.monotonic() - cached["at"] <= float(
                self.config.get("balance_cache_seconds", 5)
            ):
                return max(
                    Decimal(0),
                    cached["available"]
                    - max(Decimal(0), known - cached["known"])
                    - unknown,
                )
            try:
                available = self._read_funds(key, deadline)
            except requests.RequestException:
                _ACCOUNT_CACHE[cache_scope] = {
                    "failure_until": time.monotonic()
                    + float(self.config.get("balance_query_cooldown_seconds", 5))
                }
                raise
            _ACCOUNT_CACHE[cache_scope] = {
                "at": time.monotonic(),
                "available": available,
                "known": known,
            }
            return max(Decimal(0), available - unknown)
        finally:
            lock.release()

    def _await_funds(self, task_key, args, context):
        started = time.monotonic()
        maximum = max(0, float(self.config.get("balance_wait_seconds", 300)))
        stop_at = started + maximum
        if args["deadline"] is not None:
            stop_at = min(stop_at, args["deadline"])
        delays = self.config.get("balance_backoff_seconds", [5, 10, 20, 40, 60]) or [0]
        attempts = 0
        while True:
            blocked = self._provider_gate(task_key, args["capture"])
            if blocked:
                return None, blocked
            if context.cancel_event.is_set() or not self.store.dispatch_allowed(
                self.store.get_run(self.run_id), args["repair"]
            ):
                return None, self._blocked(
                    "cancelled_before_send", task_key, args["capture"]
                )
            if attempts and time.monotonic() >= stop_at:
                return None, self._blocked("waiting_balance", task_key, args["capture"])
            try:
                available = self._funds(
                    self._key(), stop_at if maximum else args["deadline"]
                )
                if attempts:
                    self._emit(
                        "balance_ready",
                        task_key=task_key,
                        phase=args["phase"],
                        free_queries=attempts + 1,
                    )
                return available, None
            except (
                requests.RequestException,
                ValueError,
                KeyError,
                TypeError,
            ) as error:
                attempts += 1
                blocked = self._provider_gate(task_key, args["capture"])
                if blocked:
                    return None, blocked
                retryable = isinstance(error, requests.RequestException)
                status = getattr(getattr(error, "response", None), "status_code", None)
                if status is not None and status not in {500, 502, 503, 504}:
                    retryable = False
                delay = max(0, float(delays[min(attempts - 1, len(delays) - 1)]))
                self._emit(
                    "waiting_balance",
                    task_key=task_key,
                    phase=args["phase"],
                    error_type=type(error).__name__,
                    reason=self._reason(error),
                    free_query_attempts=attempts,
                    next_query_seconds=delay,
                    retrying_free_query_only=bool(retryable and maximum),
                )
                context.progress("waiting_balance")
                if not retryable or not maximum or time.monotonic() >= stop_at:
                    return None, self._blocked(
                        "waiting_balance", task_key, args["capture"]
                    )
                wake = min(stop_at, time.monotonic() + delay)
                while time.monotonic() < wake:
                    if context.cancel_event.wait(
                        min(0.1, max(0, wake - time.monotonic()))
                    ):
                        return None, self._blocked(
                            "cancelled_before_send", task_key, args["capture"]
                        )
                    if (self.spec.output / "STOP").exists():
                        return None, self._blocked(
                            "cancelled_before_send", task_key, args["capture"]
                        )

    def _read_funds(self, key, deadline=None):
        if self.post is not None and self.account_reader is None:
            return None
        if self.account_reader:
            value = self.account_reader()
        else:
            response = requests.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": "Bearer " + key},
                timeout=min(15, max(0.01, deadline - time.monotonic()))
                if deadline is not None
                else 15,
            )
            if response.status_code in {401, 402, 403, 429}:
                self._block_response(response.status_code, response.headers)
            response.raise_for_status()
            value = response.json()
        if isinstance(value, (str, int, float, Decimal)):
            return money(value)
        if not value.get("is_available"):
            raise ValueError("account_unavailable")
        if not value.get("balance_infos"):
            raise ValueError("account_balance_missing")
        total = Decimal(0)
        for row in value.get("balance_infos", []):
            if row.get("currency") == "USD":
                total += money(row["total_balance"])
            elif row.get("currency") == "CNY":
                ratio = self._account_tariff_ratio
                if ratio is None and (
                    self.post is not None or self.account_reader is not None
                ):
                    # Explicit offline injection can exercise historical fixtures;
                    # a real CNY account requires this run's verified tariff ratio.
                    ratio = money(self.config.get("account_tariff_ratio", ".15"))
                if ratio is None or ratio <= 0:
                    raise ValueError("account_tariff_ratio_unverified")
                total += money(row["total_balance"]) * ratio
            else:
                raise ValueError("account_currency_not_supported")
        self._emit(
            "account_observed",
            available_usd_tariff_comparable=str(total),
            verified_account_tariff_ratio=str(self._account_tariff_ratio)
            if self._account_tariff_ratio is not None
            else None,
            native_balances=[
                {
                    key: str(row[key])
                    for key in (
                        "currency",
                        "total_balance",
                        "granted_balance",
                        "topped_up_balance",
                    )
                    if key in row
                }
                for row in value["balance_infos"]
            ],
            note="Account scope only; tariff comparison is not FX or per-run debit.",
        )
        return total

    def _start_runtime(self):
        with self._lock:
            if self._runtime is None:
                from src.shared_contracts import AuthorityRef

                authority = AuthorityRef(
                    self.spec.authority_root,
                    isolated=bool(getattr(self.spec, "authority_isolated", False)),
                )
                if self.store.execution.path != authority.execution_db:
                    self.store.bind_execution_authority(authority)
                # Each executor claims only its own submitted jobs; SQLite's
                # shared lane gate still caps all producer executors at 20.
                self.store.reconcile()
                runtime = ExecutionRuntime(self.store.execution, {"research_llm": 20})
                runtime.register(
                    ModuleSpec(
                        self._module,
                        (
                            JobSpec(
                                self._job,
                                self._execute,
                                lane="research_llm",
                                timeout_seconds=7200,
                                recovery="unknown",
                            ),
                        ),
                    )
                )
                runtime.start()
                self._runtime = runtime
            return self._runtime

    def call(
        self,
        task_key,
        request,
        folder,
        phase,
        company="",
        hold_usd=0,
        repair=False,
        deadline=None,
    ):
        if self._closed:
            raise RuntimeError("executor_closed")
        old = self.store.get_attempt(task_key)
        if old and old["status"] != "not_sent":
            return (
                self._wait_existing(task_key, deadline)
                if old["status"] in {"queued", "sending"}
                else self._reuse(old)
            )
        shared = self._shared_attempt(task_key, deadline)
        if shared is not None:
            return shared
        if not self.spec.paid:
            return self._blocked("paid_not_authorized", task_key)
        if not self.store.dispatch_allowed(self.store.get_run(self.run_id), repair):
            return self._blocked("run_paused", task_key)
        if deadline is not None and time.monotonic() >= deadline:
            return self._blocked("deadline_before_send", task_key)
        if not self._key():
            return self._blocked("missing_api_key", task_key)
        blocked = self._provider_gate(task_key)
        if blocked:
            return blocked
        if self._pricing_changed():
            return self._blocked("pricing_refresh_needed", task_key)
        if datetime.now(timezone.utc).date().isoformat() not in self._pricing_dates:
            return self._blocked("pricing_unverified", task_key)
        if not all(k in self.rates for k in ("input", "output", "cached_input")):
            return self._blocked("pricing_unverified", task_key)
        request = copy.deepcopy(request)
        tokenizer, estimate, reservation = self._estimate(request)
        folder = Path(folder).resolve()
        if not (folder == self.spec.output or folder.is_relative_to(self.spec.output)):
            raise ValueError("capture_outside_run_output")
        identity = fingerprint({"task": task_key, "producer": uuid.uuid4().hex})
        transport_folder = folder / identity
        capture = transport_folder / "jobs" / identity
        owned, record = self.store.claim_task(
            task_key,
            self.run_id,
            capture,
            phase=phase,
            company=company,
            repair=repair,
            provider_id=self._provider_id(),
            request_sha256=fingerprint(request),
        )
        if not owned:
            return (
                self._wait_existing(task_key, deadline)
                if record["status"] in {"queued", "sending"}
                else self._reuse(record)
            )
        with self._lock:
            self._arguments[task_key] = {
                "request": request,
                "task_key": task_key,
                "folder": transport_folder,
                "identity": identity,
                "capture": capture,
                "phase": phase,
                "company": company,
                "hold_usd": hold_usd,
                "reservation": reservation,
                "tokenizer": tokenizer,
                "estimate": estimate,
                "repair": repair,
                "deadline": deadline,
                "post_invoked": False,
            }
        try:
            try:
                runtime = self._start_runtime()
                runtime_id = runtime.submit(
                    self._job,
                    {"task_key": task_key},
                    request_id=identity,
                    source="research_pipeline",
                    max_pending=1000,
                )
            except BaseException as error:  # noqa: BLE001 - reconcile any committed queue admission.
                return self._submission_failed(task_key, capture, identity, error)
            try:
                runtime.wait(
                    runtime_id,
                    timeout=max(0, deadline - time.monotonic())
                    if deadline is not None
                    else None,
                )
            except TimeoutError:
                runtime.cancel(runtime_id)
                execution = runtime.get_run(runtime_id)
                row = self.store.get_attempt(task_key)
                if not execution.get("side_effect") and not row["sent_intent"]:
                    receipt = self._blocked("deadline_before_send", task_key, capture)
                    self.store.settle(task_key, receipt, capture)
                    return receipt
                # The real handler retains its lane and reservation until the
                # network call exits; never relabel this as an unsent retry.
                return {
                    "id": task_key,
                    "status": "unknown",
                    "reason": "deadline_inflight",
                    "capture_dir": str(capture),
                    "reused": False,
                    "usage": None,
                    "cost": {"usd": None, "kind": "usage_unknown"},
                }
            row = self.store.get_attempt(task_key)
            if row["status"] in {"queued", "sending"}:
                failed = self._arguments[task_key].get("handler_failed")
                if failed:
                    return failed
                return self._wait_existing(task_key, deadline)
            result = dict(row.get("receipt") or {})
            result.update(
                capture_dir=str(capture),
                reused=False,
                producer_run_id=self.run_id,
                charged_to_current_run=True,
            )
            return result
        finally:
            with self._lock:
                # Drop large complete request bodies after the handler exits.
                # A timed-out live handler already owns its local argument copy.
                self._arguments.pop(task_key, None)

    def _submission_failed(self, task_key, capture, identity, error):
        # Submit can fail before admission or after a committed admission. Fence
        # first, then inspect/cancel the durable queue identity before labelling
        # anything unsent. A possible provider side effect remains unknown.
        self._fence(
            error,
            pause_reason="ledger_error"
            if isinstance(error, sqlite3.Error)
            else "queue_error",
        )
        with self.store.execution.connection() as db:
            queued = db.execute(
                "SELECT * FROM execution_runs WHERE job_id=? AND request_id=?",
                (self._job, identity),
            ).fetchone()
        if queued and self._runtime is not None:
            self._runtime.cancel(queued["id"])
        record = self.store.get_attempt(task_key)
        if record and record.get("receipt"):
            return {**record["receipt"], "capture_dir": str(capture), "reused": False}
        if record and (record["sent_intent"] or queued and queued["side_effect"]):
            return {
                "id": task_key,
                "status": "unknown",
                "reason": "queue_admission_uncertain",
                "capture_dir": str(capture),
                "usage": None,
                "cost": {"usd": None, "kind": "usage_unknown"},
                "reused": False,
            }
        receipt = self._blocked("queue_submission_failed", task_key, capture)
        receipt.update(error_type=type(error).__name__, detail=self._reason(error))
        self.store.settle(task_key, receipt, capture)
        return receipt

    def _snapshot(self, args):
        folder = args["folder"]
        files = {}
        if (
            fingerprint(args["request"])
            != self.store.get_attempt(args["task_key"])["request_sha256"]
        ):
            raise IntegrityGateError("request_changed_after_admission")
        # Freeze one immutable code image for this executor, then reference it
        # from every request manifest. Recopying the full implementation for
        # every concurrent POST multiplies fsync work and delays admission.
        # Live code and the saved image are still verified for every request.
        with self._lock:
            for path in self._implementation:
                if file_sha(path) != self._import_hashes[str(path.resolve())]:
                    raise IntegrityGateError("implementation_changed_restart_required")
            if self._code_snapshot_files is None:
                image = self.spec.output / "execution_code" / self._identity
                saved = {}
                for index, path in enumerate(self._implementation):
                    target = image / (str(index) + "_" + path.name)
                    atomic_write(target, path.read_bytes(), exclusive=True)
                    saved[str(target)] = self._import_hashes[str(path.resolve())]
                self._code_snapshot_files = saved
            for path, digest in self._code_snapshot_files.items():
                if file_sha(Path(path)) != digest:
                    raise IntegrityGateError("execution_code_snapshot_changed")
            files.update(self._code_snapshot_files)
        preview = folder / "approved_request.json"
        atomic_write(preview, args["request"], exclusive=True)
        files[str(preview)] = file_sha(preview)
        pricing = folder / "approved_pricing.json"
        atomic_write(
            pricing,
            {
                "execution_rates": self.rates,
                "peak_rates": self._peak_rates,
                "pricing_utc_dates": self._pricing_dates,
                "tariff_phase": self._tariff_phase,
                "account_tariff_ratio": str(self._account_tariff_ratio)
                if self._account_tariff_ratio is not None
                else None,
                "reservation_usd_not_spend": str(args["reservation"]),
            },
            exclusive=True,
        )
        files[str(pricing)] = file_sha(pricing)
        files[str(args["tokenizer"])] = file_sha(args["tokenizer"])
        atomic_write(
            folder / "freeze.json",
            {
                "files": files,
                "implementation": self._import_hashes,
                "request_sha256": fingerprint(args["request"]),
            },
            exclusive=True,
        )
        atomic_write(
            folder / "freeze.sha256", file_sha(folder / "freeze.json"), exclusive=True
        )
        return files

    def _fence(self, reason, pause_reason="integrity_or_resource_error"):
        self.store.update_run(
            self.run_id,
            state="paused",
            requires_resume=True,
            pause_reason=pause_reason,
            error=self._reason(reason),
        )
        self._emit("hard_gate_failed", reason=self._reason(reason))

    def _pre_post_integrity(self, args, data):
        try:
            integrity.verify_input_manifests(self.store.get_run(self.run_id))
            minimum = int(self.config.get("minimum_free_bytes", 256 * 1024 * 1024))
            if minimum < 0:
                raise IntegrityGateError("invalid_minimum_free_bytes")
            if shutil.disk_usage(args["folder"]).free < minimum:
                raise IntegrityGateError("insufficient_disk_free_before_post")
            for path in self._implementation:
                if file_sha(path) != self._import_hashes[str(path.resolve())]:
                    raise IntegrityGateError("implementation_changed_restart_required")
            freeze_path = args["folder"] / "freeze.json"
            if file_sha(freeze_path) != (args["folder"] / "freeze.sha256").read_text(
                "utf-8"
            ):
                raise IntegrityGateError("freeze_manifest_changed_before_post")
            freeze = load(freeze_path)
            transport.verify(freeze["files"])
            approved = load(args["folder"] / "approved_request.json")
            actual = json.loads(data)
            if (
                fingerprint(actual) != freeze["request_sha256"]
                or fingerprint(actual) != fingerprint(approved)
                or fingerprint(actual)
                != self.store.get_attempt(args["task_key"])["request_sha256"]
            ):
                raise IntegrityGateError("serialized_request_changed_before_post")
        except (OSError, ValueError, TypeError, KeyError, IntegrityGateError) as error:
            self._fence(error)
            raise IntegrityGateError(self._reason(error)) from error

    def _execute(self, payload, context):
        task_key = payload["task_key"]
        args = self._arguments[task_key]
        receipt = None
        try:
            receipt = self._provider_gate(task_key, args["capture"])
            if receipt is not None:
                pass
            elif context.cancel_event.is_set():
                receipt = self._blocked(
                    "cancelled_before_send", task_key, args["capture"]
                )
            else:
                available, receipt = self._await_funds(task_key, args, context)
                if receipt is None:
                    reason = self.store.reserve(
                        task_key,
                        args["reservation"],
                        args["hold_usd"],
                        available,
                        args["capture"],
                    )
                    if reason:
                        receipt = self._blocked(reason, task_key, args["capture"])
            if receipt is None:
                try:
                    frozen = self._snapshot(args)
                except (OSError, IntegrityGateError) as error:
                    self._fence(error)
                    raise
                if context.cancel_event.is_set():
                    receipt = self._blocked(
                        "cancelled_before_send", task_key, args["capture"]
                    )
                else:
                    self._emit(
                        "model_started",
                        task_key=task_key,
                        phase=args["phase"],
                        company=args["company"],
                        estimated_input_tokens=args["estimate"],
                        over_soft_500k=args["estimate"] > 500000,
                        reserved_usd_not_spend=str(args["reservation"]),
                        repair=args["repair"],
                    )
                    plan = {
                        "endpoint": self.config.get("endpoint", transport.ENDPOINT),
                        "execution_rates": self.rates,
                        "pricing_utc_dates": self._pricing_dates,
                        "tokenizer": str(args["tokenizer"]),
                        "_gateway_managed": True,
                    }
                    if self._pricing_changed():
                        raise ValueError("pricing_refresh_needed")
                    receipt = transport.perform(
                        args["folder"],
                        {
                            "id": args["identity"],
                            "dependencies": [],
                            "reserved_usd_peak": str(args["reservation"]),
                        },
                        args["request"],
                        plan,
                        self._key(),
                        post=self._observed_post(
                            task_key, args["phase"], args["deadline"], context, args
                        ),
                    )
                    try:
                        transport.verify(frozen)
                    except (OSError, ValueError) as error:
                        self._fence(error)
                        raise IntegrityGateError(self._reason(error)) from error
                    receipt.update(
                        capture_dir=str(args["capture"]),
                        simulated=self.post is not None,
                        phase=args["phase"],
                        company=args["company"],
                        provider_post_invoked=args["post_invoked"],
                    )
                    if not args["post_invoked"]:
                        receipt.update(
                            status="not_sent",
                            reason=args.get(
                                "dispatch_block_reason",
                                "cancelled_or_expired_before_send",
                            ),
                        )
                    if receipt.get("http_status") in {401, 402, 403}:
                        self.store.update_run(
                            self.run_id, state="source_blocked", requires_resume=True
                        )
            if args.get("gateway_lease"):
                args["gateway_lease"].finish(receipt)
            self.store.settle(task_key, receipt, args["capture"])
            self._emit(
                "model_finished"
                if receipt["status"] != "not_sent"
                else "task_not_sent",
                task_key=task_key,
                status=receipt["status"],
                phase=args["phase"],
                usage=receipt.get("usage"),
                cost=receipt.get("cost"),
                reason=receipt.get("reason"),
            )
            return RunResult(
                "succeeded"
                if receipt["status"] == "complete"
                else "skipped"
                if receipt["status"] == "not_sent"
                else "partial"
                if receipt["status"] == "partial"
                else "unknown"
                if receipt["status"] == "unknown"
                else "failed",
                receipt.get("reason", ""),
                artifacts={"capture_dir": str(args["capture"])},
            )
        except BaseException as error:
            observed = receipt or {}
            args["handler_failed"] = {
                "id": task_key,
                "status": "unknown" if args.get("post_invoked") else "not_sent",
                "reason": self._reason(error),
                "capture_dir": str(args["capture"]),
                "usage": observed.get("usage"),
                "cost": observed.get("cost") or {"usd": None, "kind": "usage_unknown"},
                "provider_receipt_status": observed.get("status"),
                "reused": False,
            }
            if isinstance(error, sqlite3.Error):
                self._fence(error, pause_reason="ledger_error")
            if (
                isinstance(error, IntegrityGateError)
                or str(error).startswith("frozen_file_changed:")
                or isinstance(error, OSError)
                and error.errno == 28
            ) and not self.store.get_run(self.run_id).get("requires_resume"):
                self._fence(error)
            record = self.store.get_attempt(task_key)
            if (
                record
                and not record.get("receipt")
                and record["capture_dir"] == str(args["capture"])
            ):
                receipt = {
                    "id": task_key,
                    "status": "unknown" if record["sent_intent"] else "not_sent",
                    "error_type": type(error).__name__,
                    "reason": self._reason(error),
                    "capture_dir": str(args["capture"]),
                    "usage": observed.get("usage"),
                    "cost": observed.get("cost")
                    or {"usd": None, "kind": "usage_unknown"},
                    "provider_receipt_status": observed.get("status"),
                    "automatic_retries": 0,
                    "simulated": self.post is not None,
                }
                self.store.settle(task_key, receipt, args["capture"])
                self._emit(
                    "model_error",
                    task_key=task_key,
                    error_type=type(error).__name__,
                    reason=self._reason(error),
                    status=receipt["status"],
                )
            raise
        finally:
            if args.get("gateway_lease"):
                args["gateway_lease"].close()

    def _observed_post(
        self, task_key, phase, deadline=None, context=None, task_args=None
    ):
        executor = self

        class ObservedResponse:
            def __init__(self, response, session=None):
                self.response, self.session = response, session
                self.status_code, self.headers = response.status_code, response.headers
                if self.status_code in {401, 402, 403, 429}:
                    executor._block_response(
                        self.status_code,
                        self.headers,
                        (task_args or {}).get("capture", ""),
                    )
                if task_args is not None:
                    allowed = {
                        str(k): str(v)
                        for k, v in self.headers.items()
                        if str(k).lower()
                        in {"content-type", "date", "retry-after", "x-request-id"}
                    }
                    try:
                        atomic_write(
                            task_args["capture"] / "transport_headers.json",
                            allowed,
                            exclusive=True,
                        )
                    except OSError as error:
                        executor._emit(
                            "header_capture_failed",
                            task_key=task_key,
                            error_type=type(error).__name__,
                        )

            def __enter__(self):
                self.response.__enter__()
                return self

            def __exit__(self, *args):
                try:
                    return self.response.__exit__(*args)
                finally:
                    if self.session:
                        self.session.close()

            def iter_content(self, chunk_size):
                last, count = 0.0, 0
                for chunk in self.response.iter_content(chunk_size=chunk_size):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise requests.ReadTimeout("research_call_deadline")
                    count += len(chunk)
                    if time.monotonic() - last >= 1:
                        executor._emit(
                            "stream_progress",
                            task_key=task_key,
                            phase=phase,
                            received_bytes=count,
                            last_byte_at=now(),
                        )
                        last = time.monotonic()
                    yield chunk

        def send(url, **kwargs):
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise requests.ReadTimeout("research_call_deadline")
                # Short bounded reads ensure a silent repair stream cannot
                # outlive its deadline by the ordinary 1800-second read cap.
                kwargs["timeout"] = (min(60, remaining), min(15, remaining))
            if executor._pricing_changed():
                if task_args is not None:
                    task_args["dispatch_block_reason"] = "pricing_refresh_needed"
                raise RuntimeError("pricing_refresh_needed")
            if executor.store.provider_block(executor._provider_id()):
                if task_args is not None:
                    task_args["dispatch_block_reason"] = "provider_blocked"
                raise RuntimeError("provider_blocked_before_post")
            if task_args is not None:
                if not executor.store.dispatch_allowed(
                    executor.store.get_run(executor.run_id),
                    task_args.get("repair", False),
                ):
                    task_args["dispatch_block_reason"] = "run_paused"
                    raise RuntimeError("run_paused_before_post")
                try:
                    executor._pre_post_integrity(task_args, kwargs.get("data"))
                except IntegrityGateError:
                    task_args["dispatch_block_reason"] = "integrity_or_resource_error"
                    raise
            executor._model_gateway()
            authorization = executor._gateway.authorize(
                "research:" + executor.run_id, executor.spec.budget_limit_usd
            )
            lease = executor._gateway.claim(
                "research:" + task_key,
                authorization=authorization,
                producer=executor.run_id,
                request_sha256=fingerprint((task_args or {}).get("request", {})),
                account=account_id(url, executor._key()),
                reservation=(task_args or {}).get("reservation", 0),
                capture=(task_args or {}).get("capture", ""),
                deadline=deadline,
                cancelled=(lambda: context.cancel_event.is_set()) if context else None,
            )
            if task_args is not None:
                task_args["gateway_lease"] = lease
            if deadline is not None and time.monotonic() >= deadline:
                lease.close()
                raise requests.ReadTimeout("research_call_deadline")
            if context is not None and not context.mark_side_effect(
                "provider_post_intent"
            ):
                lease.close()
                raise RuntimeError("cancelled_before_provider_post")
            if task_args is not None:
                executor.store.mark_sent(task_key, task_args["capture"])
                task_args["post_invoked"] = True
                executor._emit("provider_post_started", task_key=task_key, phase=phase)
            lease.mark_sent()
            response, session = transport.send_once(
                lease, url, post=executor.post, **kwargs
            )
            return ObservedResponse(response, session)

        return send

    def close(self):
        self._closed = True
        if self._runtime is not None:
            self._runtime.stop(drain=True, timeout=7200)
