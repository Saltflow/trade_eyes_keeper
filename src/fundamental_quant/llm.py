"""Single-attempt JSON LLM transport; the service owns durable budgets/retries."""

from __future__ import annotations

import hashlib
import json
import math
import os
from urllib.parse import urlsplit

import requests

TRANSPORT_VERSION = "fq-json-transport/4"


class ResearchLLMError(RuntimeError):
    """Credential-free error with billing information when the provider supplied it."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        usage: dict | None = None,
        diagnostics: dict | None = None,
    ) -> None:
        super().__init__(f"Fundamental research model request failed: {code}")
        self.code = code
        self.retryable = retryable
        self.usage = usage or {"input_tokens": None, "output_tokens": None}
        self.diagnostics = diagnostics or {}
        # Private audit only. Never include in exception text or ordinary logs.
        self.raw_content: str | None = None


def _number(value: object, low: float, high: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchLLMError(f"invalid_config_{name}")
    if not math.isfinite(value) or not low <= value <= high:
        raise ResearchLLMError(f"invalid_config_{name}")
    return value


def _usage(body: dict) -> dict:
    raw = body.get("usage") or {}
    if not isinstance(raw, dict):
        raw = {}
    result = {}
    for target, source in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
    ):
        value = raw.get(source)
        result[target] = value if type(value) is int and value >= 0 else None
    # Preserve explicit provider receipts. An omitted cache field is unknown,
    # never zero. Input cache hits do not establish an output-cache discount.
    cache = {}
    for key in (
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "completion_cache_hit_tokens",
        "completion_cache_miss_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = raw.get(key)
        if type(value) is int and value >= 0:
            cache[key] = value
    for key in ("prompt_tokens_details", "completion_tokens_details"):
        details = raw.get(key)
        if isinstance(details, dict):
            value = details.get("cached_tokens")
            if type(value) is int and value >= 0:
                cache[key] = {"cached_tokens": value}
    if cache:
        result["provider_cache_usage"] = cache
    return result


class _InvalidJSONValue(ValueError):
    """Fixed category only; never retain provider-controlled keys or values."""


def _unique_object(pairs: list[tuple], repairs: list | None = None) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            # One observed, semantics-preserving serialization defect only.
            # Numbers, duplicate objects and conflicting labels still fail.
            if (
                repairs is not None
                and key == "currency"
                and type(value) is str
                and value in {"CNY", "USD", "HKD", "EUR", "GBP", "JPY"}
                and type(result[key]) is str
                and result[key] == value
            ):
                repairs.append(
                    {"kind": "identical_currency_key_removed", "value": value}
                )
                continue
            raise _InvalidJSONValue("duplicate_json_field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _InvalidJSONValue("nonfinite_json_value")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _InvalidJSONValue("nonfinite_json_value")
    return parsed


def _text_diagnostics(value: object) -> dict:
    if not isinstance(value, str):
        return {}
    encoded = value.encode("utf-8", errors="surrogatepass")
    return {
        "content_chars": len(value),
        "content_utf8_bytes": len(encoded),
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _json_error_diagnostics(error: BaseException) -> dict:
    if isinstance(error, json.JSONDecodeError):
        return {
            "error_category": "json_syntax",
            "json_line": error.lineno,
            "json_column": error.colno,
            "json_offset": error.pos,
        }
    if isinstance(error, _InvalidJSONValue):
        return {"error_category": str(error)}
    if isinstance(error, RecursionError):
        return {"error_category": "json_nesting_limit"}
    return {"error_category": "json_value_error"}


def _retain_assistant_content(
    error: ResearchLLMError, content: object, limit: int
) -> ResearchLLMError:
    """Keep only bounded assistant output for the protected request audit trail."""
    if isinstance(content, str):
        error.raw_content = content[:limit]
        error.diagnostics["raw_content_truncated"] = len(content) > limit
        error.diagnostics["raw_content_retained_chars"] = len(error.raw_content)
    return error


class ResearchLLM:
    """Uses existing llm credentials without sharing conversational histories.

    Exactly one POST is attempted, redirects are disabled, and requests has no
    retry adapter. Never log bodies, headers, URLs with credentials, or exceptions.
    """

    def __init__(self, config: dict):
        llm = config.get("llm") or {}
        settings = (config.get("fundamental_quant") or {}).get("llm") or {}
        self.api_key = str(llm.get("api_key") or os.getenv("DEEPSEEK_API_KEY", ""))
        self.base_url = str(llm.get("base_url") or "https://api.deepseek.com/v1")
        self.model = str(settings.get("model") or llm.get("model") or "deepseek-chat")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ResearchLLMError("invalid_config_base_url")
        self.timeout = _number(
            settings.get("request_timeout_seconds", 60), 1, 120, "timeout"
        )
        self.max_tokens = _number(
            settings.get("max_tokens", 8192),
            128,
            393216 if settings.get("raw_pdf_session") is True else 32768,
            "max_tokens",
        )
        if int(self.max_tokens) != self.max_tokens:
            raise ResearchLLMError("invalid_config_max_tokens")
        self.max_tokens = int(self.max_tokens)
        self.temperature = _number(settings.get("temperature", 0), 0, 2, "temperature")
        self.thinking_mode = settings.get("thinking_mode", "enabled")
        if self.thinking_mode not in {"enabled", "disabled"}:
            raise ResearchLLMError("invalid_config_thinking_mode")
        self.max_context_chars = int(
            _number(settings.get("max_context_chars", 200000), 1000, 1000000, "context")
        )
        self.raw_pdf_session = settings.get("raw_pdf_session") is True

    def complete(self, messages: list[dict]) -> dict:
        if not self.api_key.strip():
            raise ResearchLLMError("credentials_missing")
        if not isinstance(messages, list) or not messages:
            raise ResearchLLMError("invalid_messages")
        try:
            encoded = json.dumps(messages, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise ResearchLLMError("invalid_messages") from None
        if self.raw_pdf_session:
            # The session harness checks tokens/images/output headroom before
            # durable reservation. Keep a separate wire-size guard here.
            if len(encoded.encode("utf-8")) > 48 * 1024 * 1024:
                raise ResearchLLMError("session_request_body_limit")
        elif len(encoded) > self.max_context_chars:
            raise ResearchLLMError("context_limit")
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "thinking": {"type": self.thinking_mode},
        }
        usage = {"input_tokens": None, "output_tokens": None}
        diagnostics = {"parse_stage": "transport"}
        raw_content = None
        try:
            with requests.post(
                self.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=(min(5, self.timeout), self.timeout),
                allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    code = int(response.status_code)
                    raise ResearchLLMError(
                        f"http_{code}",
                        retryable=(code == 429 or code >= 500),
                        diagnostics={
                            "parse_stage": "http_status",
                            "error_category": "http_status",
                            "http_status": code,
                        },
                    )
                diagnostics = {"parse_stage": "provider_json"}
                try:
                    body = response.json()
                except (ValueError, RecursionError) as error:
                    raise ResearchLLMError(
                        "invalid_response",
                        diagnostics={
                            **diagnostics,
                            **_text_diagnostics(response.text),
                            **_json_error_diagnostics(error),
                        },
                    ) from None
            diagnostics = {"parse_stage": "provider_envelope"}
            if not isinstance(body, dict):
                diagnostics["error_category"] = "envelope_not_object"
                raise TypeError
            usage = _usage(body)
            if not isinstance(body.get("choices"), list) or not body["choices"]:
                diagnostics["error_category"] = "choices_missing_or_invalid"
                raise TypeError
            choice = body["choices"][0]
            if not isinstance(choice, dict):
                diagnostics["error_category"] = "choice_not_object"
                raise TypeError
            finish = choice.get("finish_reason")
            diagnostics["finish_reason"] = (
                finish
                if isinstance(finish, str)
                and finish
                in {"stop", "length", "content_filter", "tool_calls", "function_call"}
                else "missing"
                if finish is None
                else "unrecognized"
            )
            message = choice.get("message")
            if isinstance(message, dict):
                raw_content = message.get("content")
                diagnostics.update(_text_diagnostics(message.get("content")))
                diagnostics["reasoning_present"] = bool(
                    message.get("reasoning_content")
                )
            if choice.get("finish_reason") != "stop":
                raise ResearchLLMError(
                    "incomplete_response",
                    usage=usage,
                    diagnostics={
                        **diagnostics,
                        "error_category": "incomplete_response",
                    },
                )
            if not isinstance(message, dict):
                diagnostics["error_category"] = "message_not_object"
                raise TypeError
            if message.get("tool_calls"):
                raise ResearchLLMError(
                    "unexpected_tool_calls",
                    usage=usage,
                    diagnostics={
                        **diagnostics,
                        "error_category": "unexpected_tool_calls",
                    },
                )
            diagnostics["parse_stage"] = "assistant_content"
            raw_content = message.get("content")
            if not isinstance(raw_content, str):
                diagnostics["error_category"] = "content_not_string"
                raise TypeError
            if not raw_content.strip():
                diagnostics["error_category"] = "content_empty"
                raise TypeError
            try:
                repairs = []
                content = json.loads(
                    raw_content,
                    object_pairs_hook=lambda pairs: _unique_object(pairs, repairs),
                    parse_constant=_reject_constant,
                    parse_float=_finite_float,
                )
            except (ValueError, RecursionError) as error:
                raise ResearchLLMError(
                    "invalid_response",
                    usage=usage,
                    diagnostics={**diagnostics, **_json_error_diagnostics(error)},
                ) from None
            if not isinstance(content, dict):
                diagnostics["error_category"] = "content_not_object"
                raise TypeError
            return {
                "content": content,
                "usage": usage,
                "model": str(body.get("model") or self.model),
                "finish_reason": "stop",
                "transport_repairs": repairs,
                "transport_diagnostics": {
                    **diagnostics,
                    "raw_content_truncated": len(raw_content) > self.max_context_chars,
                    "raw_content_retained_chars": min(
                        len(raw_content), self.max_context_chars
                    ),
                }
                if repairs
                else {},
                "raw_assistant_content": raw_content[: self.max_context_chars]
                if repairs
                else None,
            }
        except ResearchLLMError as error:
            raise _retain_assistant_content(
                error, raw_content, self.max_context_chars
            ) from None
        except (requests.Timeout, requests.ConnectionError):
            raise ResearchLLMError(
                "transport_unavailable",
                retryable=True,
                diagnostics={
                    "parse_stage": "transport",
                    "error_category": "unavailable",
                },
            ) from None
        except requests.RequestException:
            raise ResearchLLMError(
                "transport_error",
                diagnostics={
                    "parse_stage": "transport",
                    "error_category": "request_error",
                },
            ) from None
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise _retain_assistant_content(
                ResearchLLMError(
                    "invalid_response",
                    usage=usage,
                    diagnostics={"error_category": "invalid_structure", **diagnostics},
                ),
                raw_content,
                self.max_context_chars,
            ) from None
