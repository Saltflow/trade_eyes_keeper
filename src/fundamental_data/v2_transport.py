"""PDF preparation and one-attempt API transport for structured data."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values
from tokenizers import Tokenizer


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, ensure_ascii=False, indent=2).encode()
    )
    with path.open("xb") as stream:
        stream.write(raw)


def event(output, step, **details):
    output.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(timezone.utc).isoformat(), "step": step, **details}
    with (output / "events.jsonl").open("a", encoding="utf8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


class RecordedResponse:
    """Response-shaped local fixture; cannot make an HTTP request."""

    def __init__(self, source):
        self.content = (source / "response.body").read_bytes()
        self.recorded_receipt = json.loads(
            (source / "receipt.json").read_text(encoding="utf8")
        )
        self.status_code = self.recorded_receipt["http_status"]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("recorded_http_error")


def prepare_replay(repo, output, source):
    files = implementation_files(repo)
    files += [
        source / name for name in ("request.json", "response.body", "receipt.json")
    ]
    hashes = {
        str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
    }
    request_bytes = (source / "request.json").read_bytes()
    save(output / "request.json", request_bytes)
    save(
        output / "freeze.json",
        {"files": hashes, "mode": "replay", "max_calls": 0, "automatic_retries": 0},
    )
    event(output, "request_prepared", mode="replay", source=str(source.resolve()))
    return json.loads(request_bytes), {
        "input_tokens_estimate": 0,
        "max_output_tokens": 0,
    }


def read_pdf(path):
    """Compatibility backend; durable callers use the shared data-hub cache."""
    from src.data_hub.pdf import read_pdf as neutral_read_pdf

    return neutral_read_pdf(path)


def payload(config, stage, content):
    repo = Path(__file__).resolve().parents[2]
    examples = json.loads((repo / config["examples_path"]).read_text(encoding="utf8"))
    example = (
        config.get("peers_example", {})
        if stage == "peers"
        else examples["blocks"][stage]
    )
    task = (
        config[stage + "_prompt"]
        + "\nJSON schema:\n"
        + json.dumps(config[stage + "_schema"], ensure_ascii=False)
        + "\n虚构规范示例（只示意结构，不得借用公司、数字或删减提取范围）：\n"
        + json.dumps(example, ensure_ascii=False)
    )
    if stage in config.get("jobs", {}):
        task += "\n逐项提取清单（不得仅按主题名选关键项）：\n" + json.dumps(
            config.get("task_details", {}).get(stage, []), ensure_ascii=False
        )
        task += (
            "\n固定身份/单位/维度目录（目录外明确null，不自造代码）：\n"
            + json.dumps(
                {
                    "identity_catalog": config.get("identity_catalog", {}),
                    "dimension_axes": config.get("dimension_axes", {}),
                },
                ensure_ascii=False,
            )
        )
        task += "\n歧义正反对照（虚构示意，数字不得作为本报告事实）：\n" + json.dumps(
            config.get("normalization_examples", []), ensure_ascii=False
        )
    if stage in config.get("jobs", {}):
        messages = [
            {"role": "system", "content": config["chunk_common_prompt"]},
            {
                "role": "user",
                "content": [
                    *content,
                    {"type": "text", "text": "[block:" + stage + "]\n" + task},
                ],
            },
        ]
    else:
        messages = [
            {"role": "system", "content": task},
            {"role": "user", "content": content},
        ]
    return {
        "model": config["budget_policy"]["model"],
        "thinking": config.get("thinking", {"type": "enabled"}),
        "max_tokens": config.get("jobs", {}).get(stage, {}).get("max_tokens", 8192),
        "response_format": {"type": "json_object"},
        "messages": messages,
    }


def implementation_files(repo):
    from src.model_gateway.integrity import implementation_files as gateway_files

    config = json.loads(
        (repo / "config/fundamental_data_v2.json").read_text(encoding="utf8")
    )
    return [
        *Path(__file__).parent.glob("v2_*.py"),
        repo / "src/__init__.py",
        Path(__file__).parent / "__init__.py",
        repo / "scripts/build_fundamental_data_v2.py",
        repo / "config/fundamental_data_v2.json",
        repo / config["examples_path"],
        *gateway_files(),
        *(repo / "src/data_hub").glob("*.py"),
    ]


def prepare_request(repo, output, request, sources):
    tokenizer = Tokenizer.from_file(
        str(repo / "data/fundamental_quant/tokenizer/tokenizer.json")
    )
    serialized = json.dumps(request)
    texts, images = [], 0
    for message in request["messages"]:
        if isinstance(message["content"], str):
            texts.append(message["content"])
        else:
            for block in message["content"]:
                if block["type"] == "text":
                    texts.append(block["text"])
                else:
                    images += 1
    estimate = sum(len(tokenizer.encode(t).ids) for t in texts) + images * 1024 + 4096
    if (
        estimate + request["max_tokens"] > 1000000
        or images > 600
        or len(serialized.encode()) > 48 * 1024 * 1024
    ):
        raise ValueError("request_exceeds_capacity")
    save(output / "request.json", request)
    event(output, "request_prepared", mode="api", input_tokens_estimate=estimate)
    files = [*implementation_files(repo), *sources]
    hashes = {
        str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
    }
    request_path = output / "request.json"
    hashes[str(request_path.resolve())] = hashlib.sha256(
        request_path.read_bytes()
    ).hexdigest()
    save(
        output / "freeze.json",
        {
            "files": hashes,
            "input_tokens_estimate": estimate,
            "max_calls": 1,
            "automatic_retries": 0,
        },
    )
    return {
        "input_tokens_estimate": estimate,
        "max_output_tokens": request["max_tokens"],
    }


def call(repo, output, request, parse, replay_source=None):
    frozen = json.loads((output / "freeze.json").read_text(encoding="utf8"))
    run_freeze = output.parent.parent / "freeze.json"
    if output.parent.name == "blocks" and run_freeze.is_file():
        frozen["files"].update(json.loads(run_freeze.read_text("utf8"))["files"])
    if any(
        hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest
        for name, digest in frozen["files"].items()
    ):
        raise ValueError("frozen_code_or_input_changed")
    if json.loads((output / "request.json").read_text(encoding="utf8")) != request:
        raise ValueError("frozen_request_changed")
    key = None
    if replay_source is None:
        key = os.getenv("DEEPSEEK_API_KEY") or dotenv_values(repo / "config/.env").get(
            "DEEPSEEK_API_KEY"
        )
        if not key:
            raise ValueError("missing_api_key")
    receipt = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "usage": None,
        "status": "failed",
        "automatic_retries": 0,
        "mode": "replay" if replay_source is not None else "api",
    }
    result = None
    save(
        output / "attempt.json",
        {"started_at": receipt["started_at"], "mode": receipt["mode"]},
    )
    event(output, "attempt_started", mode=receipt["mode"])
    try:
        if replay_source is not None:
            response = RecordedResponse(replay_source)
            receipt["source_record"] = str(replay_source.resolve())
            receipt["historical_usage"] = response.recorded_receipt.get("usage")
        else:
            from decimal import Decimal

            from src.model_gateway.json_client import request_json
            from src.platform_composition import authority_for

            config = json.loads((repo / "config/fundamental_data_v2.json").read_bytes())
            pricing = config["pricing"]
            rates = {
                "input": pricing["input_cache_miss_per_million"],
                "cached_input": pricing["input_cache_hit_per_million"],
                "output": pricing["output_per_million"],
            }
            run_root = output.parent.parent if output.parent.name == "blocks" else output
            call_plan_path = run_root / "call_plan.json"
            call_plan = json.loads(call_plan_path.read_bytes()) if call_plan_path.exists() else {}
            reservation = (
                Decimal(frozen.get("input_tokens_estimate", 0)) * Decimal(str(rates["input"]))
                + Decimal(request["max_tokens"]) * Decimal(str(rates["output"]))
            ) / 1000000
            budget = call_plan.get("budget", {}).get("planning_limit_usd")
            if budget is None:
                budget = reservation
            response, gateway = request_json(
                authority=authority_for(repo),
                intent="v2_extract:" + hashlib.sha256(
                    json.dumps(request, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
                producer="v2_extract:" + str(run_root.resolve()),
                authorization="v2_extract:" + str(run_root.resolve()),
                budget=budget, reservation=reservation,
                endpoint="https://api.deepseek.com/v1/chat/completions",
                payload=request, key=key, rates=rates, capture=output,
                timeout=(60, 1800), post=requests.post,
            )
            receipt["gateway"] = gateway
            if gateway.get("reused"):
                receipt["reused"] = True
        save(output / "response.body", response.content)
        event(
            output,
            "response_saved",
            bytes=len(response.content),
            http_status=response.status_code,
        )
        receipt["http_status"] = response.status_code
        body = json.loads(response.content)
        if replay_source is None and not receipt.get("reused"):
            receipt["usage"] = body.get("usage")
        elif receipt.get("reused"):
            receipt["historical_usage"] = body.get("usage")
        response.raise_for_status()
        choice = body["choices"][0]
        raw = choice["message"]["content"]
        save(output / "content.txt", raw.encode())
        event(output, "content_saved", characters=len(raw))
        receipt["finish_reason"] = choice["finish_reason"]
        if choice["finish_reason"] != "stop":
            receipt["completion_note"] = "provider_reported_incomplete_output"
        result = parse(raw)
        save(output / "parsed.json", result)
        event(output, "json_parsed")
        receipt["status"] = "received"
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        OSError,
    ) as error:
        result = None
        receipt["error_type"] = type(error).__name__
        if isinstance(error, json.JSONDecodeError):
            receipt["parse_error"] = {
                "message": error.msg,
                "line": error.lineno,
                "column": error.colno,
                "position": error.pos,
            }
        event(
            output,
            "processing_failed",
            error_type=type(error).__name__,
            parse_error=receipt.get("parse_error"),
        )
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(output / "receipt.json", receipt)
    event(output, "attempt_finished", status=receipt["status"])
    return result
