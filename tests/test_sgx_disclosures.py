"""SGX contract regression from the 2026-11-04 production source captures."""

import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from src.data.sgx_disclosures import SGXDisclosureProvider
from src.research_pipeline.models import Instrument

FIXTURES = Path(__file__).parent / "fixtures/sgx"
DETAILS = {
    "PY3VCKEQ8KS6R716": "cict_annual_march",
    "KY9MFVRBH8BC7QVR": "cict_annual_april",
    "VCPNR9KRJGTHXPVY": "g3b_annual",
}


def raw(name):
    return json.loads((FIXTURES / name).read_text("utf-8"))


class Website:
    def __init__(self):
        self.calls = []
        self.count = 104
        self.mutate = None

    def get(self, url, **kwargs):
        self.calls.append(url)
        path, query = urlsplit(url).path, parse_qs(urlsplit(url).query)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        if path.endswith("appconfig.json"):
            content = (FIXTURES / "official_appconfig.json").read_bytes()
        elif "content-api" in path:
            content = b'{"data":{"qrValidator":"grfg-frffvba"}}'
        elif path.endswith("/count"):
            content = json.dumps({"meta": {"code": "200"}, "data": self.count}).encode()
        elif path.endswith("/securitycode"):
            code = query["value"][0]
            name = (
                "cict_page1.json"
                if query["pagestart"] == ["1"]
                else "announcements_" + code + ".json"
            )
            payload = raw(name)
            if self.mutate:
                payload = self.mutate(payload)
            content = json.dumps(payload).encode()
        else:
            identifier = path.split("/")[3]
            content = (FIXTURES / (DETAILS[identifier] + ".html")).read_bytes()
        response._content = content
        return response


@pytest.fixture
def source(tmp_path):
    website = Website()
    config = {"storage": {"authority_root": str(tmp_path)}}
    return SGXDisclosureProvider(config, http=website), website


def test_counts_not_meta_pages_and_no_duplicate_ids(source):
    provider, website = source
    rows = provider.fetch("C38U-SI", date(2026, 1, 1), date(2026, 11, 4))
    assert len(rows) == 104
    assert len({row["document_id"] for row in rows}) == 104
    assert any("pagestart=1" in url for url in website.calls)
    assert all(row["stock_code"] == "C38U.SI" for row in rows)
    assert all("+08:00" in row["published_at"] for row in rows)
    assert Instrument.parse("sg:C38U").market == "sg"


@pytest.mark.parametrize("problem", ["count", "identity", "code", "duplicate"])
def test_incomplete_or_wrong_directory_is_not_empty_success(source, problem):
    provider, website = source
    if problem == "count":
        website.count = 105
    else:

        def mutate(payload):
            if problem == "identity":
                payload["data"][0]["issuers"][0]["stock_code"] = "GAB"
            elif problem == "code":
                payload["meta"]["code"] = "SGX_4015"
            else:
                payload["data"].append(payload["data"][0])
            return payload

        website.mutate = mutate
    with pytest.raises(ValueError):
        provider.fetch("C38U.SI", date(2026, 1, 1), date(2026, 11, 4))


def test_annual_selection_uses_body_period_and_correct_attachment(source, monkeypatch):
    provider, _ = source
    rows = provider.fetch("C38U.SI", date(2026, 1, 1), date(2026, 11, 4))
    monkeypatch.setattr(
        provider,
        "fetch",
        lambda *args: [
            row
            for row in rows
            if row["document_id"] in {"PY3VCKEQ8KS6R716", "KY9MFVRBH8BC7QVR"}
        ],
    )
    documents = provider.annuals("C38U", date(2026, 1, 1), date(2026, 11, 4))
    assert len(documents) == 1
    assert documents[0]["period_end"] == "2025-12-31"
    assert "878885" in documents[0]["annual_attachment"]["url"]


def test_g3b_full_year_results_contains_the_annual_report(source, monkeypatch):
    provider, website = source
    website.count = 52
    rows = provider.fetch("G3B.SI", date(2026, 1, 1), date(2026, 11, 4))
    monkeypatch.setattr(
        provider,
        "fetch",
        lambda *args: [row for row in rows if row["document_id"] == "VCPNR9KRJGTHXPVY"],
    )
    documents = provider.annuals("G3B", date(2026, 1, 1), date(2026, 11, 4))
    assert documents[0]["period_end"] == "2026-06-30"
    assert documents[0]["security_identity"]["stock_code"] == "G3B"
    assert "905236" in documents[0]["annual_attachment"]["url"]


def test_cached_body_reuse_is_shared_and_no_second_network_call(source):
    provider, website = source
    rows = provider.fetch("C38U.SI", date(2026, 1, 1), date(2026, 11, 4))
    item = next(row for row in rows if row["document_id"] == "PY3VCKEQ8KS6R716")
    first = provider.detail(item)
    count = len(website.calls)
    other = SGXDisclosureProvider(provider.config, http=website)
    assert (
        other.detail(item)["source_document"]["sha256"]
        == first["source_document"]["sha256"]
    )
    assert len(website.calls) == count
