"""Official metadata clients share source admission without double wrapping."""

import time
from types import SimpleNamespace

import pytest

from src.data.announcement_fetcher import _AnnouncementBudgetHTTP
from src.data_hub import DataHub, MaterialError
from src.data_hub.http import AuthorityHTTP
from src.instruments.point_in_time import (
    CninfoAnnualReportProvider,
    HkexStatementProvider,
)
from src.instruments.providers import SecCompanyFactsProvider
from src.shared_contracts import AuthorityRef
from src.task_runtime.request_policy import RequestPolicy, RequestPolicyBlocked


def response(content_type="application/json", raw=b"{}", status=200):
    return SimpleNamespace(
        status_code=status, headers={"Content-Type": content_type}, content=raw
    )


def test_default_official_metadata_providers_share_explicit_deployment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TRADE_EYES_AUTHORITY_ROOT", str(tmp_path / "authority"))
    clients = [
        CninfoAnnualReportProvider({}),
        HkexStatementProvider({}),
        SecCompanyFactsProvider({}),
    ]
    assert all(isinstance(provider.http, AuthorityHTTP) for provider in clients)
    assert len({provider.http.authority.authority_id for provider in clients}) == 1
    supplied = SimpleNamespace(get=lambda *_args, **_kw: None)
    assert CninfoAnnualReportProvider({}, http=supplied).http is supplied
    assert HkexStatementProvider({}, http=supplied).http is supplied
    assert SecCompanyFactsProvider({}, http=supplied).http is supplied


def test_metadata_access_verification_blocks_other_projects_and_alias_hosts(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return response("text/html", b"<html><title>Just a moment</title></html>")

    first = AuthorityHTTP(authority, SimpleNamespace(get=get), minimum_interval=0)
    second = AuthorityHTTP(authority, SimpleNamespace(get=get), minimum_interval=0)
    with pytest.raises(ValueError, match="403"):
        first.get("https://www.cninfo.com.cn/directory")
    with pytest.raises(RequestPolicyBlocked):
        second.get("https://static.cninfo.com.cn/different.pdf")
    assert len(calls) == 1


def test_original_verification_html_is_not_a_local_not_pdf_gap(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    hub = DataHub(authority)
    with pytest.raises(MaterialError, match="403"):
        hub.ensure_original(
            "https://static.cninfo.com.cn/annual.pdf",
            lambda _: b"<html><title>Access Denied</title></html>",
            content_type="application/pdf",
        )
    with pytest.raises(RequestPolicyBlocked):
        RequestPolicy(authority).wait("https://www.cninfo.com.cn/query", min_interval=0)


def test_announcement_deadline_reaches_shared_policy_wait(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return response()

    client = AuthorityHTTP(authority, SimpleNamespace(get=get), minimum_interval=5)
    client.get("https://www.cninfo.com.cn/first")
    wrapper = _AnnouncementBudgetHTTP(client, None, time.monotonic() + 0.05)
    with pytest.raises(TimeoutError):
        wrapper.get("https://static.cninfo.com.cn/second", timeout=1)
    assert len(calls) == 1


def test_automatic_redirects_cannot_opt_out_of_source_policy(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    calls = []
    client = AuthorityHTTP(
        authority, SimpleNamespace(get=lambda *_a, **_kw: calls.append(1))
    )
    with pytest.raises(ValueError, match="automatic_redirects"):
        client.get("https://www.cninfo.com.cn/source", allow_redirects=True)
    assert calls == []


@pytest.mark.parametrize("timeout", [None, 60, (20, 60), (None, 30)])
def test_deadline_clips_actual_network_timeout_after_admission(tmp_path, timeout):
    authority = AuthorityRef(tmp_path / "authority")
    calls = []

    def get(_url, **kwargs):
        calls.append(kwargs)
        return response()

    client = AuthorityHTTP(authority, SimpleNamespace(get=get), minimum_interval=0)
    client.get(
        "https://www.cninfo.com.cn/source",
        deadline=time.monotonic() + 0.5,
        timeout=timeout,
    )
    actual = calls[0]["timeout"]
    assert all(
        0 < x <= 0.5 for x in (actual if isinstance(actual, tuple) else (actual,))
    )
    assert calls[0]["allow_redirects"] is False
    with pytest.raises(TimeoutError):
        client.get("https://www.cninfo.com.cn/source", deadline=time.monotonic() - 1)
    assert len(calls) == 1
