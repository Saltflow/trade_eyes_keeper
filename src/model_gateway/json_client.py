"""Single-attempt JSON transport using the same authority as streaming models."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from .ledger import ExistingAttempt, GatewayBlocked, GatewayLedger, account_id
from .transport import cost, send_once


class CapturedResponse:
    """A verified prior response; constructing it performs no provider request."""

    def __init__(self, root, receipt):
        self.content = (root / "response.body").read_bytes()
        if hashlib.sha256(self.content).hexdigest() != receipt.get("response_sha256"):
            raise GatewayBlocked("gateway_cached_response_changed")
        self.status_code = receipt["http_status"]
        self.headers = {"Content-Type": "application/json"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("recorded_model_http_error", response=self)

    def json(self):
        return json.loads(self.content)


def request_json(
    *,
    authority,
    intent,
    producer,
    authorization,
    budget,
    reservation,
    endpoint,
    payload,
    key,
    rates,
    capture,
    timeout,
    post=None,
    initial_known=0,
    allow_unpriced=False,
    deadline=None,
    cancelled=None,
):
    required_rates = {"input", "output", "cached_input"}
    priced = isinstance(rates, dict) and required_rates.issubset(rates)
    if not priced and not allow_unpriced:
        raise GatewayBlocked("gateway_verified_pricing_required")
    ledger = GatewayLedger(authority)
    auth = ledger.authorize(authorization, budget, initial_known=initial_known)
    digest = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    account = account_id(endpoint, key)
    if deadline is None:
        values = timeout if isinstance(timeout, tuple) else (timeout,)
        finite = [float(value) for value in values if value is not None]
        if finite:
            deadline = time.monotonic() + sum(finite)
    root = (
        authority.root / "model_captures" / authority.scope_id / ledger._intent(intent)
    )
    try:
        lease = ledger.claim(
            intent,
            authorization=auth,
            producer=producer,
            request_sha256=digest,
            account=account,
            reservation=reservation,
            capture=root,
            deadline=deadline,
            cancelled=cancelled,
        )
    except ExistingAttempt as existing:
        old = json.loads(existing.record["receipt"] or "{}")
        if not old.get("response_sha256"):
            raise GatewayBlocked(
                "gateway_attempt_unknown_or_artifact_unavailable"
            ) from existing
        response = CapturedResponse(root, old)
        return response, {
            "reused": True,
            "charged_to_current_run": False,
            "historical_usage": old.get("usage"),
            "historical_cost": old.get("cost"),
            "producer": existing.record["producer"],
            "capture": str(root),
            "usage": None,
            "cost": {"usd": "0", "kind": "reuse_no_new_model_charge"},
        }
    with lease:
        root.mkdir(parents=True, exist_ok=True)
        frozen_body = json.dumps(payload, allow_nan=False).encode()
        request_file = root / "request.body"
        if request_file.exists():
            if request_file.read_bytes() != frozen_body:
                raise GatewayBlocked("gateway_frozen_request_changed")
        else:
            with request_file.open("xb") as handle:
                handle.write(frozen_body)
        if (cancelled is not None and cancelled()) or (
            deadline is not None and time.monotonic() >= deadline
        ):
            raise GatewayBlocked("gateway_cancelled_or_expired_before_send")
        lease.mark_sent()
        response, _session = send_once(
            lease,
            endpoint,
            post=post or requests.post,
            json=payload,
            headers={"Authorization": "Bearer " + key},
            timeout=timeout,
            allow_redirects=False,
        )
        if response.status_code in {401, 402, 403, 429}:
            retry_after = None
            if response.status_code == 429:
                header = getattr(response, "headers", {}).get("Retry-After")
                try:
                    retry_after = float(header)
                except (ValueError, TypeError):
                    try:
                        retry_after = max(
                            0,
                            (
                                parsedate_to_datetime(header)
                                - datetime.now(timezone.utc)
                            ).total_seconds(),
                        )
                    except (ValueError, TypeError, OverflowError):
                        retry_after = None
            ledger.block(account, "http_" + str(response.status_code), retry_after)
        raw = getattr(response, "content", None)
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw)
            capture_kind = "raw_provider_bytes"
        else:
            raw = json.dumps(
                response.json(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            capture_kind = "adapter_json_not_raw"
        try:
            body = json.loads(raw)
            usage = body.get("usage") if isinstance(body, dict) else None
        except (ValueError, TypeError):
            usage = None
        receipt = {
            "status": "received" if response.status_code == 200 else "failed",
            "http_status": response.status_code,
            "usage": usage,
            "cost": cost(usage, {"execution_rates": rates})
            if priced
            else {"usd": None, "kind": "pricing_unavailable"},
            "producer": producer,
            "authorization": authorization,
            "request_sha256": digest,
            "automatic_retries": 0,
            "capture": str(root),
            "consumer_capture": str(capture),
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "response_capture_kind": capture_kind,
        }
        with (root / "response.body").open("xb") as handle:
            handle.write(raw)
        with (root / "receipt.json").open("x", encoding="utf-8") as handle:
            json.dump(receipt, handle, ensure_ascii=False, indent=2)
        lease.finish(receipt)
        return response, receipt
