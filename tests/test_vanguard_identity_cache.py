"""Real Vanguard success/shell responses reproduce metadata cache failure."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from src.data import us_market_history as us

FIXTURES = Path(__file__).parent / "fixtures" / "vanguard_identity"
URL = "https://investor.vanguard.com/investment-products/etfs/profile/voo"


def html(kind="with_metadata"):
    return gzip.decompress((FIXTURES / (kind + ".html.gz")).read_bytes())


class Session:
    def __init__(self, content=None, *, status=200, url=URL):
        self.content = html() if content is None else content
        self.status, self.url, self.calls = status, url, 0

    def get(self, *args, **kwargs):
        self.calls += 1
        result = requests.Response()
        result.status_code, result.url, result._content = (
            self.status,
            self.url,
            self.content,
        )
        return result


def provider(tmp_path, session=None, overrides=None):
    settings = {
        "output_dir": str(tmp_path),
        "market_history": {"vanguard_fund_ids": overrides or {}},
    }
    return us.USDisclosedMarketHistoryProvider(
        {"point_in_time_data": settings}, http=session
    )


def cached_path(tmp_path):
    return tmp_path / "us_publications" / "fund_identities" / "VOO.json"


def test_real_success_is_reused_after_restart_without_second_request(tmp_path):
    first = Session()
    assert provider(tmp_path, first)._fund_id("VOO") == "0968"
    second = Session(html("without_metadata"))
    restarted = provider(tmp_path, second)
    assert restarted._fund_id("VOO") == "0968"
    assert first.calls == 1 and second.calls == 0
    receipt = json.loads(cached_path(tmp_path).read_text())
    assert receipt["symbol"] == "VOO" and receipt["fund_id"] == "0968"
    assert receipt["source_url"] == URL
    assert receipt["response_sha256"] == hashlib.sha256(html()).hexdigest()
    assert restarted.records[URL] == receipt["response_sha256"]


def test_real_http_200_shell_is_typed_transient_not_permanent_missing(tmp_path):
    with pytest.raises(us.USIssuerMetadataTransientError) as caught:
        provider(tmp_path, Session(html("without_metadata")))._fund_id("VOO")
    assert caught.value.symbol == "VOO" and caught.value.source_url == URL
    assert caught.value.retry_after_seconds > 0
    assert not cached_path(tmp_path).exists()


def test_identity_cache_reuses_365_days_without_network(tmp_path):
    provider(tmp_path, Session())._fund_id("VOO")
    path = cached_path(tmp_path)
    row = json.loads(path.read_text())
    observed = datetime.now(timezone.utc) - timedelta(days=365)
    row["observed_at"] = observed.isoformat()
    row["expires_at"] = (observed + timedelta(days=366)).isoformat()
    path.write_text(json.dumps(row))
    session = Session(html("without_metadata"))
    assert provider(tmp_path, session)._fund_id("VOO") == "0968" and session.calls == 0


@pytest.mark.parametrize(
    "mutation", ["symbol", "fund_id", "source_url", "bytes", "digest"]
)
def test_corrupt_identity_cache_never_accepted(tmp_path, mutation):
    provider(tmp_path, Session())._fund_id("VOO")
    path = cached_path(tmp_path)
    row = json.loads(path.read_text())
    if mutation == "bytes":
        (
            tmp_path
            / "us_publications"
            / "responses"
            / (row["response_sha256"] + ".bin")
        ).write_bytes(b"changed")
    else:
        key = {"digest": "response_sha256"}.get(mutation, mutation)
        row[key] = {
            "symbol": "SPY",
            "fund_id": "9999",
            "source_url": "https://evil.example/profile/voo",
            "digest": "0" * 64,
        }[mutation]
        path.write_text(json.dumps(row))
    session = Session()
    with pytest.raises(ValueError):
        provider(tmp_path, session)._fund_id("VOO")
    assert session.calls == 0


def test_configured_override_wins_even_if_cache_damaged(tmp_path):
    cached_path(tmp_path).parent.mkdir(parents=True)
    cached_path(tmp_path).write_text("bad json")
    session = Session()
    assert provider(tmp_path, session, {"VOO": "1234"})._fund_id("VOO") == "1234"
    assert session.calls == 0


@pytest.mark.parametrize("status", [403, 429])
def test_explicit_http_refusal_not_retyped_transient(tmp_path, status):
    with pytest.raises(requests.HTTPError) as caught:
        provider(tmp_path, Session(status=status))._fund_id("VOO")
    assert caught.value.response.status_code == status


def test_official_redirect_identity_mismatch_is_hard_failure(tmp_path):
    with pytest.raises(ValueError, match="official|URL|url"):
        provider(tmp_path, Session(url="https://evil.example/voo"))._fund_id("VOO")


def test_real_fixture_hashes_are_recorded():
    for source in json.loads((FIXTURES / "sources.json").read_text()):
        assert (
            hashlib.sha256(
                gzip.decompress((FIXTURES / source["file"]).read_bytes())
            ).hexdigest()
            == source["raw_sha256"]
        )


def test_expired_verified_identity_requires_fresh_source(tmp_path):
    provider(tmp_path, Session())._fund_id("VOO")
    path = cached_path(tmp_path)
    row = json.loads(path.read_text())
    observed = datetime.now(timezone.utc) - timedelta(days=367)
    row["observed_at"] = observed.isoformat()
    row["expires_at"] = (observed + timedelta(days=366)).isoformat()
    path.write_text(json.dumps(row))
    session = Session(html("without_metadata"))
    with pytest.raises(us.USIssuerMetadataTransientError):
        provider(tmp_path, session)._fund_id("VOO")
    assert session.calls == 1


def test_wrong_symbol_in_official_source_is_not_transient(tmp_path):
    changed = html().replace(b"VOO.P", b"SPY.P")
    with pytest.raises(ValueError, match="requested symbol") as caught:
        provider(tmp_path, Session(changed))._fund_id("VOO")
    assert not isinstance(caught.value, us.USIssuerMetadataTransientError)
    assert not cached_path(tmp_path).exists()


def test_cached_shell_even_with_matching_digest_is_rejected(tmp_path):
    provider(tmp_path, Session())._fund_id("VOO")
    path = cached_path(tmp_path)
    row = json.loads(path.read_text())
    body = html("without_metadata")
    digest = hashlib.sha256(body).hexdigest()
    (tmp_path / "us_publications" / "responses" / (digest + ".bin")).write_bytes(body)
    row["response_sha256"] = digest
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="not backed") as caught:
        provider(tmp_path, Session())._fund_id("VOO")
    assert not isinstance(caught.value, us.USIssuerMetadataTransientError)


def test_retry_contract_only_accepts_typed_transient(tmp_path):
    from src.data.backtest_data import _retry_delay

    with pytest.raises(us.USIssuerMetadataTransientError) as caught:
        provider(tmp_path, Session(html("without_metadata")))._fund_id("VOO")
    assert _retry_delay(caught.value, 1, datetime.now(timezone.utc)) == 1.0
    assert (
        _retry_delay(
            ValueError("Vanguard issuer metadata does not match requested symbol"),
            1,
            datetime.now(timezone.utc),
        )
        is None
    )
    with pytest.raises(requests.HTTPError) as blocked:
        provider(tmp_path, Session(status=403))._fund_id("VOO")
    assert _retry_delay(blocked.value, 1, datetime.now(timezone.utc)) is None
