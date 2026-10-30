"""One attempt per frozen request. No retry, response repair, or fallback."""

import json
import threading
from datetime import datetime, timezone

import requests
from tokenizers import Tokenizer

from .api_io import check_seal, write_once


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def reject(value):
        raise ValueError("nonfinite_json_value")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)


class Client:
    def __init__(self, output, settings, hashes, key):
        self.output, self.settings, self.hashes, self.key = (
            output,
            settings,
            hashes,
            key,
        )
        self.tokenizer = Tokenizer.from_file(settings["tokenizer"])
        self.lock, self.reserved, self.receipts = threading.Lock(), 0.0, []

    def payload(self, stage, content):
        return {
            "model": self.settings["model"],
            "thinking": {"type": "disabled"},
            "max_tokens": self.settings["max_output_tokens"],
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": self.settings["protocol"] + "\n本次执行阶段：" + stage,
                },
                {"role": "user", "content": content},
            ],
        }

    def meter(self, payload):
        tokens = 4096
        images = 0
        for message in payload["messages"]:
            content = message["content"]
            if isinstance(content, str):
                tokens += len(self.tokenizer.encode(content).ids)
            else:
                images += sum(b["type"] == "image_url" for b in content)
                tokens += sum(
                    len(self.tokenizer.encode(b["text"]).ids)
                    if b["type"] == "text"
                    else 1024
                    for b in content
                )
        if (
            tokens + payload["max_tokens"] > 1000000
            or images > 600
            or len(json.dumps(payload).encode()) > 48 * 1024 * 1024
        ):
            raise ValueError("request_exceeds_frozen_capacity")
        return tokens

    def call(self, stage, name, content):
        folder = self.output / stage / name
        payload = self.payload(stage, content)
        tokens = self.meter(payload)
        reserve = (tokens * 0.3 + payload["max_tokens"] * 1.2) / 1000000
        check_seal(self.hashes)
        with self.lock:
            if (
                self.reserved + reserve > self.settings["max_usd"]
                or len(self.receipts) >= self.settings["max_calls"]
            ):
                raise ValueError("frozen_budget_exhausted")
            self.reserved += reserve
            receipt = {
                "stage": stage,
                "name": name,
                "input_tokens_estimate": tokens,
                "reserved_usd_peak_upper_estimate": reserve,
                "usage": None,
                "status": "failed",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            self.receipts.append(receipt)
        write_once(folder / "request.json", payload)
        write_once(
            folder / "attempt.json",
            {"automatic_retries": 0, "started_at": receipt["started_at"]},
        )
        data = None
        try:
            response = requests.post(
                self.settings["endpoint"],
                json=payload,
                headers={"Authorization": "Bearer " + self.key},
                timeout=(20, self.settings["timeout_seconds"]),
                allow_redirects=False,
            )
            receipt["http_status"] = response.status_code
            write_once(folder / "response.body", response.content)
            body = strict_json(response.content)
            receipt["usage"] = body.get("usage")
            if response.status_code != 200:
                raise ValueError("provider_http_error")
            choice = body["choices"][0]
            raw = choice["message"]["content"]
            write_once(folder / "content.txt", raw.encode())
            if choice["finish_reason"] != "stop":
                raise ValueError("incomplete_model_output")
            data = strict_json(raw)
            receipt["status"] = "received"
        except (
            requests.RequestException,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
            OSError,
        ) as error:
            receipt["error_type"] = type(error).__name__
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_once(folder / "receipt.json", receipt)
        print(
            json.dumps({k: receipt[k] for k in ("stage", "name", "status")}), flush=True
        )
        return data, receipt
