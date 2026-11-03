"""Provider-neutral frozen capture, SSE parsing and usage pricing.

Application planning and financial contracts do not belong in this module.
"""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from tokenizers import Tokenizer

ENDPOINT = "https://api.deepseek.com/chat/completions"


def send_once(lease, url, *, post=None, **kwargs):
    """The single provider side-effect boundary after durable admission."""
    if lease is None or not lease.sent:
        raise ValueError("durable_model_send_intent_required")
    if post is not None:
        return post(url, **kwargs), None
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=0))
    try:
        return session.post(url, **kwargs), session
    except BaseException:
        session.close()
        raise


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(data, (str, bytes)):
        data = json.dumps(data, ensure_ascii=False, indent=2)
    with path.open("xb") as stream:
        stream.write(data.encode("utf8") if isinstance(data, str) else data)


def verify(hashes):
    changed = [
        name
        for name, digest in hashes.items()
        if not Path(name).is_file() or sha(Path(name)) != digest
    ]
    if changed:
        raise ValueError("frozen_file_changed: " + changed[0])


def estimate(request, tokenizer, config):
    total = config["message_token_reserve"]
    if request.get("tools"):
        total += len(
            tokenizer.encode(json.dumps(request["tools"], ensure_ascii=False)).ids
        )
    for message in request["messages"]:
        content = message.get("content") or ""
        for field in ("tool_calls", "reasoning_content"):
            if message.get(field):
                total += len(
                    tokenizer.encode(json.dumps(message[field], ensure_ascii=False)).ids
                )
        parts = (
            [{"type": "text", "text": content}] if isinstance(content, str) else content
        )
        for part in parts:
            total += (
                len(tokenizer.encode(part["text"]).ids)
                if part["type"] == "text"
                else config["image_token_reserve_each"]
            )
    return total


def reserve(input_tokens, output_tokens, plan):
    rates = plan["peak_rates"]
    return (
        Decimal(input_tokens)
        * Decimal(plan["input_cost_safety_factor"])
        * Decimal(str(rates["input"]))
        + Decimal(output_tokens) * Decimal(str(rates["output"]))
    ) / 1000000


def cost(usage, plan):
    """USD from returned usage; completion already includes reasoning tokens."""
    if not isinstance(usage, dict) or any(
        not isinstance(usage.get(k), int) or usage[k] < 0
        for k in ("prompt_tokens", "completion_tokens")
    ):
        return {"usd": None, "kind": "usage_unknown"}
    prompt = usage["prompt_tokens"]
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = details.get("cached_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if isinstance(hit, int) and miss is None:
        miss = prompt - hit
    exact = (
        isinstance(hit, int)
        and isinstance(miss, int)
        and hit >= 0
        and miss >= 0
        and hit + miss == prompt
    )
    if not exact:
        hit, miss = 0, prompt
    rates = plan["execution_rates"]
    amount = (
        Decimal(hit) * Decimal(str(rates["cached_input"]))
        + Decimal(miss) * Decimal(str(rates["input"]))
        + Decimal(usage["completion_tokens"]) * Decimal(str(rates["output"]))
    ) / 1000000
    return {
        "usd": str(amount),
        "kind": "usage_at_published_rates"
        if exact
        else "usage_cache_unknown_upper_bound",
        "cache_hit_tokens": hit if exact else None,
        "cache_miss_tokens": miss if exact else None,
    }


class Stream:
    """Persist all bytes and deltas; parse only the provider transport envelope."""

    def __init__(self, raw, content, reasoning, events):
        self.raw, self.content, self.reasoning, self.events = (
            raw,
            content,
            reasoning,
            events,
        )
        self.pending, self.data = b"", []
        self.usage, self.finish, self.done, self.errors = None, None, False, []
        self.response_id, self.model = None, None
        self.tool_calls = {}

    def event(self, **data):
        self.events.write(
            json.dumps({"at": now(), **data}, ensure_ascii=False).encode() + b"\n"
        )
        self.events.flush()

    def envelope(self, value):
        self.response_id = value.get("id", self.response_id)
        self.model = value.get("model", self.model)
        if value.get("usage") is not None:
            self.usage = value["usage"]
        for choice in value.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            for position, call in enumerate(delta.get("tool_calls") or []):
                index = call.get("index", position)
                if type(index) is not int or not 0 <= index < 64:
                    raise ValueError("invalid_tool_call_index")
                target = self.tool_calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if call.get("type") not in (None, "function"):
                    raise ValueError("invalid_tool_call_type")
                for field in ("id",):
                    if call.get(field) is not None:
                        if not isinstance(call[field], str):
                            raise ValueError("invalid_tool_call_id")
                        target[field] += call[field]
                for field in ("name", "arguments"):
                    fragment = (call.get("function") or {}).get(field)
                    if fragment is not None:
                        if not isinstance(fragment, str):
                            raise ValueError("invalid_tool_call_fragment")
                        target["function"][field] += fragment
            for key, target in (
                ("content", self.content),
                ("reasoning_content", self.reasoning),
            ):
                text = delta.get(key)
                if isinstance(text, str):
                    target.write(text.encode("utf8"))
                    target.flush()
            if choice.get("finish_reason") is not None:
                self.finish = choice["finish_reason"]
        if value.get("error"):
            self.errors.append("provider_error_frame")

    def frame(self):
        if not self.data:
            return
        data = b"\n".join(self.data)
        self.data = []
        if data.strip() == b"[DONE]":
            self.done = True
            return
        try:
            self.envelope(json.loads(data))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self.errors.append(type(error).__name__)
            self.event(step="transport_frame_error", error_type=type(error).__name__)

    def feed(self, chunk):
        self.raw.write(chunk)
        self.raw.flush()
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                self.data.append(line[5:].lstrip(b" "))
            elif not line:
                self.frame()

    def end(self):
        if self.pending.startswith(b"data:"):
            self.data.append(self.pending[5:].strip())
        self.frame()


def perform(folder, job, request, plan, key, post=None, *, lease=None, clock=datetime):
    if lease is None and not plan.get("_gateway_managed"):
        raise ValueError("gateway_admission_required")
    endpoint = plan.get("endpoint")
    if not endpoint:
        raise ValueError("explicit_model_endpoint_required")
    root = folder / "jobs" / job["id"]
    root.mkdir(parents=True, exist_ok=True)
    save(root / "request.json", request)
    save(
        root / "request.freeze.json",
        {
            "request_sha256": sha(root / "request.json"),
            "run_freeze_sha256": sha(folder / "freeze.json"),
            "dependencies": {
                d: {
                    "content": sha(folder / "jobs" / d / "content.md"),
                    "receipt": sha(folder / "jobs" / d / "receipt.json"),
                }
                for d in job["dependencies"]
            },
        },
    )
    receipt = {
        "id": job["id"],
        "started_at": now(),
        "status": "failed",
        "automatic_retries": 0,
        "usage": None,
        "reserved_usd_peak": job["reserved_usd_peak"],
    }
    verify(load(folder / "freeze.json")["files"])
    if clock.now(timezone.utc).date().isoformat() not in plan["pricing_utc_dates"]:
        raise ValueError("outside_frozen_pricing_dates")
    save(
        root / "attempt.json",
        {
            "started_at": receipt["started_at"],
            "requests": 1,
            "request_sha256": sha(root / "request.json"),
        },
    )
    with (root / "response.raw").open("xb") as raw, (root / "content.md").open(
        "xb"
    ) as content, (root / "reasoning.txt").open("xb") as reasoning, (
        root / "events.jsonl"
    ).open("xb") as events:
        stream = Stream(raw, content, reasoning, events)
        stream.event(step="request_started", job=job["id"])
        try:
            with requests.Session() as session:
                session.mount("https://", HTTPAdapter(max_retries=0))
                send = post or session.post
                if lease is not None:
                    lease.mark_sent()
                with send(
                    endpoint,
                    data=(root / "request.json").read_bytes(),
                    headers={
                        "Authorization": "Bearer " + key,
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    timeout=(60, 1800),
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    receipt["http_status"] = response.status_code
                    is_sse = (
                        "text/event-stream"
                        in response.headers.get("Content-Type", "").lower()
                    )
                    receipt["transport"] = "sse" if is_sse else "json"
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            if is_sse:
                                stream.feed(chunk)
                            else:
                                raw.write(chunk)
                                raw.flush()
                    if not is_sse:
                        raw.flush()
                        try:
                            stream.envelope(load(root / "response.raw"))
                        except (ValueError, AttributeError, TypeError):
                            stream.errors.append("non_sse_response_unreadable")
                    if response.status_code != 200:
                        receipt["error_type"] = "HTTPStatus" + str(response.status_code)
        except (
            requests.RequestException,
            ValueError,
            TypeError,
            KeyError,
            OSError,
            IndexError,
            AttributeError,
            RuntimeError,
        ) as error:
            receipt["error_type"] = type(error).__name__
            stream.event(step="transport_failed", error_type=type(error).__name__)
        finally:
            stream.end()
            if receipt.get("transport") == "sse" and not stream.done:
                stream.errors.append("stream_missing_DONE")
        content.flush()
        receipt.update(
            usage=stream.usage,
            finish_reason=stream.finish,
            done=stream.done,
            transport_notes=stream.errors,
            response_id=stream.response_id,
            model=stream.model,
        )
        calls = [stream.tool_calls[index] for index in sorted(stream.tool_calls)]
        if calls and (
            not request.get("tools")
            or len({call["id"] for call in calls}) != len(calls)
            or any(not call["id"] or not call["function"]["name"] for call in calls)
        ):
            stream.errors.append("invalid_or_unrequested_tool_calls")
        message = {
            "role": "assistant",
            "content": (root / "content.md").read_text("utf-8"),
        }
        if calls:
            message["tool_calls"] = calls
        if request.get("tools"):
            reasoning.flush()
            message["reasoning_content"] = (root / "reasoning.txt").read_text("utf-8")
        save(root / "assistant_message.json", message)
        receipt["assistant_message_sha256"] = sha(root / "assistant_message.json")
        if content.tell() or calls:
            receipt["status"] = (
                "complete"
                if (
                    stream.finish == "stop"
                    and not calls
                    or stream.finish == "tool_calls"
                    and calls
                )
                and not receipt.get("error_type")
                and not stream.errors
                else "partial"
            )
        stream.event(
            step="request_finished",
            status=receipt["status"],
            finish_reason=stream.finish,
        )
    receipt["finished_at"] = now()
    receipt["cost"] = cost(receipt["usage"], plan)
    receipt["content_sha256"] = sha(root / "content.md")
    receipt["visible_tokens_local"] = len(
        Tokenizer.from_file(plan["tokenizer"])
        .encode((root / "content.md").read_bytes().decode("utf8"))
        .ids
    )
    save(root / "receipt.json", receipt)
    if lease is not None:
        lease.finish(receipt)
    print(
        json.dumps(
            {
                "job": job["id"],
                "status": receipt["status"],
                "finish": stream.finish,
                "usage": stream.usage,
                "cost": receipt["cost"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return receipt
