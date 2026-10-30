"""Single-attempt, budgeted transport with complete private response receipts.

The root is a private audit directory, not a web publication directory. Provider
reasoning remains in raw receipts and is never included in returned results.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from .llm import (
    ResearchLLM,
    ResearchLLMError,
    _finite_float,
    _reject_constant,
    _unique_object,
    _usage,
)
from .session_context import load_token_counter


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _write(path, value):
    raw = value if isinstance(value, bytes) else _json(value).encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _parse_content(raw):
    """Repair only one missing final array closer; keep blocked verdicts intact."""

    def parse(text):
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )

    try:
        return parse(raw), []
    except json.JSONDecodeError as error:
        if not isinstance(raw, str):
            raise
        trimmed = raw.rstrip()
        if not trimmed.endswith("}") or error.pos != len(trimmed) - 1:
            raise
        stack, quoted, escaped = [], False, False
        for char in trimmed[:-1]:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char in "[{":
                stack.append(char)
            elif char in "]}":
                expected = "[" if char == "]" else "{"
                if not stack or stack.pop() != expected:
                    raise
        if quoted or stack != ["{", "["]:
            raise
        recovered = parse(trimmed[:-1] + "]}")
        return recovered, [
            {
                "kind": "missing_final_array_closer",
                "inserted": "]",
                "position": len(trimmed) - 1,
                "semantic_change": False,
            }
        ]


def measure_request_tokens(payload, count_tokens, image_token_upper_bound=None):
    """Meter image blocks separately from base64 bytes; actual usage wins.

    Bound is provider-profile dependent. None keeps legacy conservative counting.
    JSON text which happens to contain a data URL is still counted as text.
    """
    if image_token_upper_bound is None:
        return count_tokens(_json(payload))
    import copy

    measured = copy.deepcopy(payload)
    messages = measured.get("messages", []) if isinstance(measured, dict) else measured
    images = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    images += 1
                    block["image_url"] = {"url": "[metered image]"}
    return count_tokens(_json(measured)) + images * image_token_upper_bound


class ComparisonTransport:
    """An explicit call ID is attempted once, including across process restarts."""

    def __init__(
        self,
        config,
        campaign,
        root,
        tokenizer_path,
        *,
        clock=None,
        input_margin_tokens=4096,
        tokenizer_sha256=None,
        context_tokens=1000000,
        max_output_tokens=393216,
        image_token_upper_bound=None,
        request_timeout_seconds=None,
    ):
        self.transport = ResearchLLM(config)
        self.context_tokens = context_tokens
        self.max_output_tokens = max_output_tokens
        self.image_token_upper_bound = image_token_upper_bound
        if request_timeout_seconds is not None and (
            type(request_timeout_seconds) not in (int, float)
            or not 1 <= request_timeout_seconds <= 1800
        ):
            raise ValueError("invalid_request_timeout")
        self.request_timeout_seconds = request_timeout_seconds
        self.campaign = campaign
        self.root = Path(root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if type(input_margin_tokens) is not int or input_margin_tokens < 0:
            raise ValueError("invalid_input_margin")
        self.input_margin_tokens = input_margin_tokens
        self.count_tokens = load_token_counter(tokenizer_path, tokenizer_sha256)
        if not campaign.enabled:
            raise ResearchLLMError("campaign_required")
        state = campaign.state()
        if not state.get("expires_at"):
            raise ResearchLLMError("campaign_not_started")
        self.deadline = datetime.fromisoformat(state["expires_at"])
        self.campaign_id = state["id"]

    def _remaining(self):
        return (self.deadline - self.clock()).total_seconds()

    def call(self, call_id, messages, max_tokens=16384):
        if not isinstance(call_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", call_id
        ):
            raise ResearchLLMError("invalid_call_id")
        if (
            type(max_tokens) is not int
            or not 128 <= max_tokens <= self.max_output_tokens
        ):
            raise ResearchLLMError("invalid_output_limit")
        if not isinstance(messages, list) or not messages:
            raise ResearchLLMError("invalid_messages")
        llm = self.transport
        if not llm.api_key.strip():
            raise ResearchLLMError("credentials_missing")
        payload = {
            "model": llm.model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": llm.temperature,
            "response_format": {"type": "json_object"},
            "thinking": {"type": llm.thinking_mode},
        }
        try:
            wire = _json(payload).encode("utf-8")
        except (ValueError, TypeError):
            raise ResearchLLMError("invalid_messages") from None
        if llm.api_key.encode("utf-8") in wire:
            raise ResearchLLMError("credential_in_request")
        digest = hashlib.sha256(
            self.campaign_id.encode() + llm.base_url.encode() + wire
        ).hexdigest()
        directory = self.root / call_id
        if directory.exists():
            saved_path = directory / "state.json"
            if not saved_path.is_file():
                raise ResearchLLMError("interrupted_call_requires_review")
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            if saved.get("request_hash") != digest:
                raise ResearchLLMError("call_id_conflict")
            if saved.get("status") == "completed":
                return json.loads((directory / "result.json").read_text("utf-8"))
            raise ResearchLLMError("existing_call_not_retryable")
        if self._remaining() <= 0:
            raise ResearchLLMError("campaign_deadline")
        serialized_tokens = measure_request_tokens(
            payload, self.count_tokens, self.image_token_upper_bound
        )
        reservation = serialized_tokens + max(
            self.input_margin_tokens, math.ceil(serialized_tokens * 0.05)
        )
        if reservation + max_tokens > self.context_tokens:
            raise ResearchLLMError("context_limit")
        if len(wire) > 48 * 1024 * 1024:
            raise ResearchLLMError("request_body_limit")
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
        except FileExistsError:
            raise ResearchLLMError("call_in_progress") from None
        state = {
            "call_id": call_id,
            "campaign_id": self.campaign_id,
            "request_hash": digest,
            "status": "intent",
            "started_at": self.clock().isoformat(),
            "deadline": self.deadline.isoformat(),
            "serialized_tokens": serialized_tokens,
            "input_token_reservations": reservation,
            "output_token_reservations": max_tokens,
            "token_measurement": "tokenizer_text_plus_provider_image_bound_and_margin"
            if self.image_token_upper_bound
            else "official_tokenizer_serialized_request_plus_margin",
            "image_token_upper_bound": self.image_token_upper_bound,
            "reserved": False,
            "usage": {},
            "usage_accounting": "not_started",
        }
        _write(directory / "request.json", wire)
        _write(directory / "state.json", state)
        try:
            amounts = {
                "model_calls": 1,
                "input_token_reservations": reservation,
                "output_token_reservations": max_tokens,
            }
            day = self.clock().date().isoformat()
            if not self.campaign.reserve_model(day, amounts):
                raise ResearchLLMError("campaign_budget_or_deadline")
            state.update(status="reserved", reserved=True)
            _write(directory / "state.json", state)
            remaining = self._remaining()
            if remaining <= 0:
                raise ResearchLLMError("campaign_deadline")
            # Both connect and read are bounded. A response completing after the
            # absolute campaign deadline is retained and explicitly marked late.
            timeout = (
                min(float(self.request_timeout_seconds), max(0.1, remaining - 5))
                if self.request_timeout_seconds is not None
                else min(float(llm.timeout), 120.0, remaining / 2)
            )
            state["status"] = "sending"
            _write(directory / "state.json", state)
            with requests.post(
                llm.base_url.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm.api_key}",
                    "Content-Type": "application/json",
                },
                data=wire,
                timeout=(min(5.0, timeout), timeout),
                allow_redirects=False,
            ) as response:
                raw = response.content
                # Keep the full body, except any provider echo of our credential.
                secret = llm.api_key.encode("utf-8")
                safe_raw = raw.replace(secret, b"[REDACTED_CREDENTIAL]")
                _write(directory / "response.raw", safe_raw)
                state.update(
                    status="received",
                    http_status=response.status_code,
                    received_at=self.clock().isoformat(),
                    late=self._remaining() <= 0,
                    credential_redacted=safe_raw != raw,
                    response_sha256=hashlib.sha256(safe_raw).hexdigest(),
                )
                _write(directory / "state.json", state)
            try:
                body = json.loads(safe_raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ResearchLLMError("invalid_provider_json") from None
            if not isinstance(body, dict):
                raise ResearchLLMError("invalid_provider_envelope")
            usage = _usage(body)
            state.update(usage=usage, usage_accounting="started")
            _write(directory / "state.json", state)
            # Never repeat this accounting on replay. A crash during accounting
            # leaves a visible reconciliation requirement, not a second charge.
            self.campaign.store.add_usage(day, usage)
            self.campaign.add_usage(usage)
            state["usage_accounting"] = "completed"
            _write(directory / "state.json", state)
            if state["http_status"] != 200:
                raise ResearchLLMError(f"http_{state['http_status']}", usage=usage)
            try:
                choice = body["choices"][0]
                message = choice["message"]
                if choice.get("finish_reason") != "stop":
                    raise ResearchLLMError("incomplete_response", usage=usage)
                if message.get("tool_calls"):
                    raise ResearchLLMError("unexpected_tool_calls", usage=usage)
                content, syntax_repairs = _parse_content(message["content"])
                if syntax_repairs:
                    state["syntax_repairs"] = syntax_repairs
                    _write(directory / "state.json", state)
                if not isinstance(content, dict):
                    raise TypeError
            except (KeyError, IndexError, TypeError, ValueError, RecursionError):
                raise ResearchLLMError("invalid_assistant_json", usage=usage) from None
            result = {
                "content": content,
                "usage": usage,
                "model": body.get("model") or llm.model,
                "finish_reason": "stop",
                "late": state["late"],
                "request_hash": digest,
                "syntax_repairs": syntax_repairs,
            }
            _write(directory / "result.json", result)
            state["status"] = "completed"
            _write(directory / "state.json", state)
            return result
        except Exception as exc:  # noqa: BLE001 - preserve paid-call crash receipt
            code = getattr(exc, "code", "transport_or_persistence_error")
            state.update(status="failed", error_code=code)
            _write(directory / "state.json", state)
            raise ResearchLLMError(code, usage=state.get("usage")) from None

    def status(self):
        """Read local receipts without mutating campaign state or making requests."""
        result = {
            "calls": [],
            "model_calls_reserved": 0,
            "input_token_reservations": 0,
            "output_token_reservations": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "unknown_usage_calls": 0,
            "accounting_reconciliation_calls": 0,
            "reservation_uncertain_calls": 0,
            "provider_cache_usage": {},
        }
        if not self.root.is_dir():
            return result
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            path = directory / "state.json"
            try:
                state = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                result["calls"].append(
                    {"call_id": directory.name, "status": "interrupted_or_unreadable"}
                )
                result["reservation_uncertain_calls"] += 1
                continue
            result["calls"].append(state)
            if state.get("status") == "intent":
                result["reservation_uncertain_calls"] += 1
            if state.get("usage_accounting") == "started":
                result["accounting_reconciliation_calls"] += 1
            if state.get("reserved"):
                result["model_calls_reserved"] += 1
                for key in ("input_token_reservations", "output_token_reservations"):
                    result[key] += state[key]
                usage = state.get("usage", {})
                known = True
                for key in ("input_tokens", "output_tokens"):
                    value = usage.get(key)
                    if type(value) is int and value >= 0:
                        result[key] += value
                    else:
                        known = False
                result["unknown_usage_calls"] += int(not known)
                for key, value in usage.get("provider_cache_usage", {}).items():
                    if type(value) is int:
                        result["provider_cache_usage"][key] = (
                            result["provider_cache_usage"].get(key, 0) + value
                        )
        return result
