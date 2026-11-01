"""Tencent wire protocol once; business consumers own their distinct views."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

from ..shared_contracts import AuthorityRef
from ..task_runtime.request_policy import RequestPolicy
from .parsing import hash_lock
from .storage import atomic_write, fingerprint, load

_RESPONSE = re.compile(
    r'(?:v_)?([A-Za-z0-9_.-]+)\s*=\s*(?:"([^"\r\n]*)"|([^\r\n;]*))\s*;?'
)


class QuoteSnapshot:
    """Keep the original HTTP contract while sharing its parsed evidence."""

    def __init__(self, response, quotes, reference):
        self.response = response
        self.quotes = quotes
        self.material_reference = reference

    def __getattr__(self, name):
        return getattr(self.response, name)


@dataclass(frozen=True)
class TencentQuote:
    symbol: str
    fields: tuple[str, ...]
    raw: str

    def text(self, index: int) -> str:
        return self.fields[index] if len(self.fields) > index else ""

    def number(self, index: int) -> float | None:
        try:
            value = float(self.text(index))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    @property
    def source_clock(self) -> str:
        return self.text(30)


def parse_tencent_quotes(text: str | bytes) -> dict[str, TencentQuote]:
    """Preserve raw fields; never replace missing source time with fetch time."""
    if isinstance(text, QuoteSnapshot):
        return text.quotes
    if isinstance(text, bytes):
        text = text.decode("gbk", errors="replace")
    if not isinstance(text, str):
        raise TypeError("quote_wire_text_required")
    result = {}
    for match in _RESPONSE.finditer(text):
        symbol, quoted, bare = match.groups()
        raw = quoted if quoted is not None else bare
        fields = tuple(raw.split("~"))
        if len(fields) < 4:
            continue
        # A mismatched response cannot be relabelled as the requested stock.
        if (
            symbol.startswith(("sh", "sz", "bj", "hk"))
            and fields[2]
            and fields[2].zfill(5 if symbol.startswith("hk") else 6) != symbol[2:]
        ):
            continue
        result[symbol] = TencentQuote(symbol, fields, raw)
    return result


def require_tencent_quote(
    text: str | bytes, symbol: str, *, min_fields: int = 4
) -> TencentQuote:
    value = parse_tencent_quotes(text).get(symbol)
    if value is None or len(value.fields) < min_fields:
        raise ValueError("quote_response_invalid_or_incomplete")
    return value


def record_tencent(response, url: str, authority: AuthorityRef) -> QuoteSnapshot:
    """A fresh quote may be refetched; identical bytes are parsed/stored once."""
    import hashlib

    from ..task_runtime.authority import bind_authority

    bind_authority(authority)
    raw = getattr(response, "content", None)
    raw_available = isinstance(raw, bytes)
    if not raw_available:
        raw = response.text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    folder = authority.materials_root / "quotes" / digest
    with hash_lock(authority.materials_root / "quote_locks" / (digest + ".lock")):
        record = folder / "parsed.json"
        if record.exists():
            saved = load(record)
            if (
                saved["payload_sha256"] != fingerprint(saved["quotes"])
                or saved.get("scope") != authority.scope
                or saved.get("source_sha256") != digest
                or not (folder / "source.bin").is_file()
                or hashlib.sha256((folder / "source.bin").read_bytes()).hexdigest()
                != digest
            ):
                raise ValueError("quote_cache_integrity_changed")
            values = saved["quotes"]
        else:
            quotes = parse_tencent_quotes(raw if raw_available else response.text)
            values = {
                key: {"fields": list(q.fields), "raw": q.raw}
                for key, q in quotes.items()
            }
            import os

            folder.mkdir(parents=True, exist_ok=True)
            source = folder / "source.bin"
            if source.exists():
                if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                    raise ValueError("quote_cache_integrity_changed")
            else:
                with source.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            atomic_write(
                record,
                {
                    "quotes": values,
                    "payload_sha256": fingerprint(values),
                    "source_sha256": digest,
                    "scope": authority.scope,
                    "capture_kind": "raw_bytes" if raw_available else "text_projection",
                },
                exclusive=True,
            )
    quotes = {
        key: TencentQuote(key, tuple(value["fields"]), value["raw"])
        for key, value in values.items()
    }
    return QuoteSnapshot(
        response,
        quotes,
        {
            "authority_id": authority.authority_id,
            "scope": authority.scope,
            "source_url": url,
            "sha256": digest,
            "path": str(record),
        },
    )


def fetch_tencent(get, url: str, *, authority: AuthorityRef, **kwargs):
    """All current quote consumers share source admission, not private caches."""
    policy = RequestPolicy(authority)
    policy.wait(url, min_interval=0.2, deadline=time.monotonic() + 30)
    try:
        response = get(url, **kwargs)
    except Exception as error:
        response = getattr(error, "response", None)
        if response is not None and getattr(response, "status_code", None) in {
            401,
            403,
            429,
        }:
            policy.block(url, "quote_source_access_limited")
        raise
    code = getattr(response, "status_code", None)
    if type(code) is int and code in {401, 403, 429}:
        policy.block(url, "quote_source_access_limited")
        response.raise_for_status()
    return record_tencent(response, url, authority)
