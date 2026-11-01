"""One actual wire parser and one source authority across quote consumers."""

import json
from types import SimpleNamespace

import pytest

from src.data_hub.quotes import (
    fetch_tencent,
    parse_tencent_quotes,
    require_tencent_quote,
)
from src.shared_contracts import AuthorityRef
from src.task_runtime.request_policy import RequestPolicy


def wire(symbol="sh600938", code="600938", clock="20261030161457"):
    fields = [""] * 74
    fields[1], fields[2], fields[3], fields[30] = "公司", code, "35.97", clock
    fields[39], fields[46], fields[73] = "8", "1.2", "47.5"
    return "v_" + symbol + '="' + "~".join(fields) + '";'


def test_protocol_identity_and_source_clock_are_preserved():
    quote = require_tencent_quote(wire(), "sh600938", min_fields=74)
    assert quote.source_clock == "20261030161457"
    assert quote.number(3) == 35.97
    assert quote.number(73) == 47.5
    assert not parse_tencent_quotes(wire(code="600000"))
    assert require_tencent_quote(wire(clock=""), "sh600938").source_clock == ""


def test_shared_snapshot_parsing_and_rate_owner(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    response = SimpleNamespace(
        text=wire(), content=wire().encode("gbk"), status_code=200
    )
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return response

    first = fetch_tencent(get, "https://qt.gtimg.cn/q=sh600938", authority=authority)
    second = fetch_tencent(get, "https://qt.gtimg.cn/q=sh600938", authority=authority)
    assert len(calls) == 2  # Explicit fresh snapshots, not a hidden long TTL.
    assert first.material_reference == second.material_reference
    paths = list((authority.materials_root / "quotes").glob("*/parsed.json"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_text())["capture_kind"] == "raw_bytes"
    assert require_tencent_quote(second, "sh600938").source_clock == "20261030161457"
    assert RequestPolicy(authority).snapshot()["sources"][0]["requests"] == 2


def test_corrupt_shared_raw_does_not_silently_reuse(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    response = SimpleNamespace(
        text=wire(), content=wire().encode("gbk"), status_code=200
    )
    fetch_tencent(
        lambda *a, **k: response, "https://qt.gtimg.cn/q=x", authority=authority
    )
    next((authority.materials_root / "quotes").glob("*/source.bin")).write_bytes(b"bad")
    with pytest.raises(ValueError, match="integrity"):
        fetch_tencent(
            lambda *a, **k: response, "https://qt.gtimg.cn/q=x", authority=authority
        )
