"""Official annual-directory adapters and immutable original acquisition.

Discovery is always live when requested. Merely reading local documents never
constructs a network client. Same-URL silent replacement detection is not claimed.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from zoneinfo import ZoneInfo

from src.data_hub import DataHub
from src.platform_composition import authority_for

from .common import atomic_write, file_sha, fingerprint, load, now
from .models import AnnualDocument, Instrument
from .parsing import hash_lock


class SourceError(ValueError):
    """A scoped source gap, not evidence that a financial fact was undisclosed."""


def document_from_record(record: dict, base: Path, spec) -> AnnualDocument:
    """Only declared identities are accepted; no folder/file-name inference."""
    identity = record.get("instrument")
    if not identity:
        code = str(record.get("code", ""))
        market = record.get("market")
        candidates = [Instrument.parse(c) for c in spec.companies]
        candidates = [
            c
            for c in candidates
            if c.code == code or (c.market == "hk" and c.code == code.zfill(5))
        ]
        if market:
            candidates = [c for c in candidates if c.market == market]
        if len(candidates) != 1:
            raise SourceError("legacy_document_identity_ambiguous")
        identity = candidates[0].key
    instrument = Instrument.parse(identity)
    year = record.get("fiscal_year", record.get("year", record.get("report_year")))
    if not re.fullmatch(r"\d{4}", str(year or "")):
        raise SourceError("document_fiscal_year_not_declared")
    path = record.get("local_path") or record.get("pdf") or record.get("pdf_path") or ""
    if path:
        path = Path(path)
        path = path if path.is_absolute() else base / path
        path = str(path.resolve())
    digest = record.get("sha256") or record.get("pdf_sha256") or ""
    if path and Path(path).is_file():
        actual = file_sha(Path(path))
        if digest and actual != digest:
            raise SourceError("declared_original_hash_changed")
        digest = actual
    issuer = (
        record.get("issuer_id")
        or spec.issuer_map.get(instrument.key)
        or instrument.issuer
    )
    url = record.get("url") or record.get("source_url") or ""
    provenance = dict(record.get("provenance", {}))
    extraction = Path(path).parent / "extraction.json" if path else None
    if not url and extraction and extraction.is_file():
        try:
            archive = load(extraction)
            if not isinstance(archive, dict):
                raise TypeError("archive_receipt_object_required")
            if archive.get("pdf_sha256") == digest and isinstance(
                archive.get("download"), dict
            ):
                url = archive["download"].get("url") or ""
                if url:
                    provenance["url_evidence"] = {
                        "path": str(extraction),
                        "sha256": file_sha(extraction),
                    }
        except (OSError, ValueError, TypeError) as error:
            provenance["url_evidence_gap"] = str(error)
    content_type = record.get("content_type") or (
        "text/html"
        if Path(path).suffix.lower() in {".html", ".htm"}
        else "application/pdf"
    )
    profiles = (
        record.get("profiles")
        or record.get("profile")
        or record.get("profile_for_estimate")
        or spec.profiles.get(instrument.key, [])
    )
    profiles = [profiles] if isinstance(profiles, str) else list(profiles)
    return AnnualDocument(
        instrument=instrument.key,
        issuer_id=issuer,
        fiscal_year=int(year),
        document_id=str(
            record.get("document_id")
            or record.get("announcement_id")
            or digest
            or fingerprint(record)
        ),
        url=url,
        published_at=record.get("published_at") or record.get("publication_date") or "",
        period_start=record.get("period_start"),
        period_end=record.get("period_end"),
        kind=record.get("kind", "annual"),
        content_type=content_type,
        local_path=path,
        sha256=digest,
        profiles=profiles,
        currency=record.get("currency"),
        provenance={**provenance, "declared_record": record},
    )


def register_document(root: Path, document: AnnualDocument) -> None:
    key = fingerprint(
        [document.instrument, document.document_id, document.sha256 or document.url]
    )
    path = Path(root) / "documents" / f"{key}.json"
    with hash_lock(Path(root) / "documents/locks" / f"{key}.lock"):
        if path.exists():
            try:
                previous = load(path)
            except (OSError, ValueError, TypeError):
                previous = None
            if previous != document.to_dict():
                # Keep every source record; never rewrite an earlier declaration.
                path = path.with_name(f"{key}_{fingerprint(document.to_dict())}.json")
        if not path.exists():
            atomic_write(path, document.to_dict(), exclusive=True)


class _OfficialHTTP:
    def __init__(self, service):
        self.service = service

    def get(self, url, **kwargs):
        return self.service._request("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self.service._request("post", url, **kwargs)


class SourceService:
    def __init__(
        self,
        spec,
        store,
        *,
        http=None,
        discovery=None,
        downloader=None,
        quoter=None,
        minimum_interval=None,
        sleep=time.sleep,
    ):
        self.spec, self.store = spec, store
        self.root = Path(getattr(store, "root", spec.store_root))
        self.http = http
        self.discovery = discovery
        self.downloader = downloader
        self.quoter = quoter
        self.minimum_interval = minimum_interval
        self.sleep = sleep
        self._local = None
        self.gaps: list[dict] = []
        self.authority = authority_for(
            spec.repo,
            self._config(),
            explicit_root=getattr(spec, "authority_root", None),
            isolated=getattr(spec, "authority_isolated", False),
        )
        self.hub = DataHub(self.authority)

    def _config(self) -> dict:
        import os

        from dotenv import dotenv_values

        value = {}
        if self.spec.config and Path(self.spec.config).is_file():
            value = load(Path(self.spec.config))
        credentials = (
            Path(getattr(self.spec, "credential_root", None) or self.spec.repo)
            / "config/.env"
        )
        private = dotenv_values(credentials) if credentials.is_file() else {}
        agent = os.getenv("SEC_USER_AGENT") or private.get("SEC_USER_AGENT")
        email = os.getenv("EMAIL_SENDER") or private.get("EMAIL_SENDER")
        if agent or email:
            value = {
                **value,
                "instrument_audit": {
                    **value.get("instrument_audit", {}),
                    "sec_user_agent": agent or f"trade-eyes-keeper/1.0 {email}",
                },
            }
        return value

    def _gap(self, instrument, error, **details):
        self.gaps.append({"instrument": instrument.key, "error": str(error), **details})

    def local_documents(self) -> list[AnnualDocument]:
        if self._local is not None:
            return list(self._local)
        documents = []
        for path in sorted((self.root / "documents").glob("*.json")):
            try:
                value = load(path)
                documents.append(AnnualDocument(**value))
            except (OSError, ValueError, TypeError) as error:
                self.gaps.append(
                    {"source": str(path), "error": f"registry_unreadable:{error}"}
                )
        if self.spec.source_manifest:
            path = Path(self.spec.source_manifest)
            value = load(path)
            records = (
                value
                if isinstance(value, list)
                else value.get("documents", value.get("records", []))
            )
            for record in records:
                try:
                    documents.append(
                        document_from_record(record, path.parent, self.spec)
                    )
                except (OSError, ValueError, TypeError) as error:
                    self.gaps.append(
                        {"source": str(path), "error": str(error), "record": record}
                    )
        selected = set(self.spec.companies)
        unique = {}
        for document in documents:
            if document.instrument in selected:
                key = (
                    document.instrument,
                    document.document_id,
                    document.sha256 or document.url,
                )
                previous = unique.get(key)
                if (
                    previous is None
                    or (not previous.local_path and document.local_path)
                    or (not previous.url and document.url)
                ):
                    unique[key] = document
        self._local = list(unique.values())
        return list(self._local)

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        interval = self.minimum_interval
        if interval is None:
            interval = (
                0.2
                if host.endswith("sec.gov")
                else 5.0
                if host.endswith("cninfo.com.cn")
                else 1.0
            )
        from src.task_runtime.request_policy import RequestPolicy

        ledger = self.root / "source_rate" / f"{fingerprint(host)}.json"
        policy = RequestPolicy(self.authority)
        marker = (
            self.authority.root
            / "legacy_source_imports"
            / (fingerprint(str(ledger.resolve())) + ".json")
        )
        if ledger.is_file() and not marker.exists():
            previous = load(ledger)
            if previous.get("blocked"):
                policy.block(url, "legacy_source_access_blocked:" + host)
            elif float(previous.get("next_at", 0)) > time.time():
                policy.block(
                    url,
                    "legacy_source_cooldown",
                    retry_after=float(previous["next_at"]) - time.time(),
                )
            atomic_write(
                marker,
                {"source": str(ledger), "sha256": file_sha(ledger)},
                exclusive=True,
            )
        policy.wait(url, min_interval=float(interval))

    def _request(self, method, url, **kwargs):
        if self.http is None:
            import requests

            self.http = requests.Session()
        self._throttle(url)
        response = getattr(self.http, method)(url, **kwargs)
        if response.status_code in {401, 403, 429}:
            host = urlsplit(url).hostname or ""
            ledger = self.root / "source_rate" / f"{fingerprint(host)}.json"
            retry = str(response.headers.get("Retry-After", "60"))
            delay = max(60, int(retry)) if retry.isdigit() else 60
            atomic_write(
                ledger,
                {
                    "host": host,
                    "blocked": response.status_code != 429,
                    "next_at": time.time() + delay,
                    "http_status": response.status_code,
                },
            )
            from src.task_runtime.request_policy import RequestPolicy

            RequestPolicy(self.authority).block(
                url,
                f"source_http_{response.status_code}",
                retry_after=delay if response.status_code == 429 else None,
            )
        return response

    def discover(self, instrument: Instrument, as_of: date) -> list[AnnualDocument]:
        local = [d for d in self.local_documents() if d.instrument == instrument.key]
        if self.discovery is not None:
            rows = self.discovery(instrument, as_of)
            found = [
                r
                if isinstance(r, AnnualDocument)
                else document_from_record(r, self.spec.repo, self.spec)
                for r in rows
            ]
        elif instrument.market == "a_share":
            from src.instruments.classifier import classify_instrument
            from src.instruments.models import InstrumentType

            fund = classify_instrument(instrument.code) != InstrumentType.EQUITY
            if fund:
                found = self._fund_annuals(instrument, as_of)
            else:
                found = self._cninfo(instrument, as_of)
        elif instrument.market == "hk":
            found = self._hkex(instrument, as_of)
        elif instrument.market == "sg":
            found = self._sgx(instrument, as_of)
        else:
            found = self._sec(instrument, as_of)
        result = []
        for document in found:
            if document.published_at and document.published_at[:10] > as_of.isoformat():
                continue
            old = self._reuse_candidate(document, local)
            if old is not None:
                document = replace(
                    document,
                    local_path=old.local_path,
                    sha256=old.sha256,
                    profiles=document.profiles or old.profiles,
                    period_start=document.period_start or old.period_start,
                    period_end=document.period_end or old.period_end,
                    currency=document.currency or old.currency,
                    provenance={
                        **old.provenance,
                        **document.provenance,
                        "legacy_document_id": old.document_id,
                        "official_document_id": document.document_id,
                    },
                )
            result.append(document)
        # A successful empty/current directory is not evidence to delete archived versions.
        existing = {(d.document_id, d.url) for d in result}
        result.extend(
            d
            for d in local
            if (d.document_id, d.url) not in existing
            and (not d.published_at or d.published_at[:10] <= as_of.isoformat())
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        atomic_write(
            self.root / "discovery" / instrument.safe_id / f"{stamp}.json",
            {
                "as_of": as_of.isoformat(),
                "checked_at": now(),
                "documents": [d.to_dict() for d in result],
                "gaps": self.gaps,
            },
        )
        return result

    @staticmethod
    def _reuse_candidate(document, local):
        def same_version(candidate):
            if document.sha256:
                return candidate.sha256 == document.sha256
            if candidate.kind != document.kind:
                return False
            if candidate.published_at != document.published_at:
                # CNINFO's full official identifier and publication date are
                # encoded in its immutable filing URL. This is positive source
                # identity evidence, unlike a merely non-official old ID.
                path = urlsplit(document.url).path
                published = re.search(r"/finalpage/(\d{4}-\d{2}-\d{2})/", path)
                mapped = (
                    not candidate.published_at
                    and candidate.document_id == candidate.sha256
                    and document.document_id == "cninfo:" + Path(path).stem
                    and published is not None
                    and document.published_at[:10] == published[1]
                )
                return mapped
            if candidate.document_id == document.document_id:
                return True
            if not document.document_id.startswith(
                ("cninfo:", "hkex:", "sec:", "sse:")
            ):
                # Compatibility-only identities carry no official revision
                # signal. Do not apply this to newly discovered official IDs.
                return True
            declared = candidate.provenance.get("declared_record", {})
            return (
                not document.published_at
                and candidate.document_id == candidate.sha256
                and declared.get("url") == document.url
                and declared.get("sha256") == candidate.sha256
            )

        candidates = [
            d
            for d in local
            if d.instrument == document.instrument
            and d.fiscal_year == document.fiscal_year
            and d.url == document.url
            and same_version(d)
        ]
        if document.sha256:
            candidates = [d for d in candidates if d.sha256 == document.sha256]
        for candidate in candidates:
            if not re.fullmatch(r"[0-9a-f]{64}", candidate.sha256):
                raise SourceError("cached_original_hash_missing")
            if (
                candidate.local_path
                and Path(candidate.local_path).is_file()
                and file_sha(Path(candidate.local_path)) != candidate.sha256
            ):
                raise SourceError("cached_original_hash_changed")
        if len({d.sha256 for d in candidates}) > 1:
            raise SourceError("legacy_same_url_versions_ambiguous")
        return candidates[0] if candidates else None

    def _years(self, as_of):
        # Discovery considers already filed non-December fiscal years in the
        # current calendar year. The orchestrator selects the final common FY
        # window from actual documents; one extra older candidate prevents loss
        # when the newest period has not yet been published for this issuer.
        end = self.spec.end_year or as_of.year
        start = self.spec.start_year or end - self.spec.years + (
            1 if self.spec.end_year else 0
        )
        return start, end

    def _issuer(self, instrument):
        return self.spec.issuer_map.get(instrument.key) or instrument.issuer

    def _cninfo(self, instrument, as_of):
        from src.instruments.point_in_time import CninfoAnnualReportProvider

        provider = CninfoAnnualReportProvider(self._config(), http=_OfficialHTTP(self))
        org = provider._org_id(instrument.code)
        start, end = self._years(as_of)
        result = []
        for page in range(1, 101):
            response = provider.http.post(
                provider.ANNOUNCEMENT_URL,
                data={
                    "pageNum": page,
                    "pageSize": 50,
                    "column": "sse"
                    if instrument.code.startswith(("5", "6", "9"))
                    else "szse",
                    "tabName": "fulltext",
                    "stock": f"{instrument.code},{org}",
                    "searchkey": "",
                    "category": "category_ndbg_szsh;",
                    "seDate": f"{start}-01-01~{as_of.isoformat()}",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "true",
                },
                headers=provider._headers,
                timeout=max(provider.timeout, 45),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("announcements"), list
            ):
                raise SourceError("cninfo_directory_invalid")
            for row in payload["announcements"]:
                if str(row.get("secCode", instrument.code)) != instrument.code:
                    continue
                title = re.sub(
                    r"<[^>]+>|\s+", "", str(row.get("announcementTitle", ""))
                )
                year = re.search(r"(20\d{2})年?年度报告", title)
                if (
                    not year
                    or not start <= int(year[1]) <= end
                    or "摘要" in title
                    or "英文" in title
                ):
                    continue
                adjunct = str(row.get("adjunctUrl") or "")
                if not adjunct:
                    continue
                published = datetime.fromtimestamp(
                    float(row["announcementTime"]) / 1000, ZoneInfo("Asia/Shanghai")
                ).isoformat()
                kind = (
                    "amendment"
                    if re.search(r"更正公告|更正说明|补充公告|补充说明|修订公告", title)
                    else "annual"
                )
                url = urljoin(provider.STATIC_ROOT, adjunct.lstrip("/"))
                self._validate_url(instrument, url)
                result.append(
                    AnnualDocument(
                        instrument.key,
                        self._issuer(instrument),
                        int(year[1]),
                        "cninfo:" + str(row.get("announcementId") or fingerprint(url)),
                        url=url,
                        published_at=published,
                        period_start=f"{year[1]}-01-01",
                        period_end=f"{year[1]}-12-31",
                        kind=kind,
                        profiles=self.spec.profiles.get(instrument.key, []),
                        provenance={
                            "source": "cninfo",
                            "title": title,
                            "announcement_id": row.get("announcementId"),
                            "official_directory_row": row,
                        },
                    )
                )
            total = int(payload.get("totalpages", page))
            if page >= total:
                break
            if page == 100 or not payload["announcements"]:
                raise SourceError("cninfo_directory_pagination_incomplete")
        return result

    def _fund_annuals(self, instrument, as_of):
        from src.data.fund_announcements import (
            CninfoFundAnnouncementProvider,
            FundAnnouncementError,
            SseFundAnnouncementProvider,
            is_fund_report,
        )

        start, end = self._years(as_of)
        http = _OfficialHTTP(self)
        try:
            if instrument.code.startswith("5"):
                rows = SseFundAnnouncementProvider(http_get=http.get).fetch_disclosures(
                    instrument.code, date(start, 1, 1), as_of
                )
            else:
                rows = CninfoFundAnnouncementProvider(http=http).fetch(
                    instrument.code,
                    date(start, 1, 1),
                    as_of,
                    category="category_ndbg_jjgg;",
                )
        except FundAnnouncementError as error:
            raise SourceError(str(error)) from error
        result = []
        for row in rows:
            title = re.sub(r"\s+", "", row["title"])
            year = re.search(r"(20\d{2})年?年度报告", title)
            if not year or not start <= int(year[1]) <= end or not is_fund_report(row):
                continue
            source = row["exchange"]
            document_id = (
                row["official_directory_row"].get("announcementId")
                if source == "cninfo"
                else None
            ) or fingerprint(row["url"])
            self._validate_url(instrument, row["url"])
            result.append(
                AnnualDocument(
                    instrument.key,
                    self._issuer(instrument),
                    int(year[1]),
                    source + ":" + str(document_id),
                    url=row["url"],
                    published_at=row.get("published_at") or row["date"],
                    kind="amendment"
                    if re.search(r"更正公告|更正说明|补充公告|补充说明|修订公告", title)
                    else "annual",
                    profiles=self.spec.profiles.get(instrument.key, []),
                    provenance={
                        "source": source,
                        "title": title,
                        "official_directory_row": row["official_directory_row"],
                    },
                )
            )
        return result

    def _sgx(self, instrument, as_of):
        from src.data.sgx_disclosures import SGXDisclosureProvider

        config = self._config()
        provider = SGXDisclosureProvider(
            config, http=_OfficialHTTP(self), authority=self.authority, hub=self.hub,
        )
        start, end = self._years(as_of)
        result = []
        for item in provider.annuals(instrument.code, date(start, 1, 1), as_of):
            period_end = date.fromisoformat(item["period_end"])
            if not start <= period_end.year <= end:
                continue
            attachment = item["annual_attachment"]
            result.append(AnnualDocument(
                instrument=instrument.key, issuer_id=self._issuer(instrument),
                fiscal_year=period_end.year,
                document_id="sgx:" + item["document_id"] + ":" + fingerprint(attachment["url"]),
                url=attachment["url"], published_at=item["published_at"],
                period_end=item["period_end"], kind="annual",
                profiles=self.spec.profiles.get(instrument.key, []),
                provenance={
                    "official_directory_row": item["source_record"],
                    "official_announcement": item["source_document"],
                    "security_identity": item["security_identity"],
                    "attachment": attachment,
                },
            ))
        return result

    def _hkex(self, instrument, as_of):
        from src.data.hk_announcements import HkexAnnouncementProvider

        provider = HkexAnnouncementProvider(self._config(), http=_OfficialHTTP(self))
        start, end = self._years(as_of)
        rows = provider.fetch(instrument.code, date(start, 1, 1), as_of)
        result = []
        for row in rows:
            title = row["title"]
            if not re.search(
                r"annual\s+report|年報|年报|年度報告|年度报告", title, re.IGNORECASE
            ):
                continue
            if re.search(
                r"annual\s+results|final\s+results|业绩|業績|摘要", title, re.IGNORECASE
            ):
                continue
            years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", title)
            short_range = re.search(r"(?<!\d)(20\d{2})\s*[/\-]\s*(\d{2})(?!\d)", title)
            if short_range:
                first, suffix = int(short_range[1]), int(short_range[2])
                last = first // 100 * 100 + suffix
                years.append(str(last if last >= first else last + 100))
            if not years:
                self._gap(instrument, "hk_annual_fiscal_year_not_in_title", row=row)
                continue
            year = max(map(int, years))
            if not start <= year <= end:
                continue
            kind = (
                "amendment"
                if re.search(
                    r"errata|corrigendum|supplement|更正|補充|补充",
                    title,
                    re.IGNORECASE,
                )
                else "annual"
            )
            self._validate_url(instrument, row["url"])
            result.append(
                AnnualDocument(
                    instrument.key,
                    self._issuer(instrument),
                    year,
                    "hkex:" + fingerprint(row["url"]),
                    url=row["url"],
                    published_at=row.get("published_at") or row["date"],
                    kind=kind,
                    profiles=self.spec.profiles.get(instrument.key, []),
                    provenance={
                        "source": "hkex",
                        "title": title,
                        "official_directory_row": row,
                        "period_note": "Fiscal label comes from title; exact fiscal dates remain unconfirmed until supplied by the original.",
                    },
                )
            )
        return result

    def _sec(self, instrument, as_of):
        from src.data.us_announcements import SecAnnouncementProvider

        service = self

        class AnnualSecProvider(SecAnnouncementProvider):
            """Scope validation to annuals; retain the general provider contract."""

            def _fund_filings(self, code, cik, fund_class, start, end):
                rows = super()._fund_filings(code, cik, fund_class, start, end)
                result = []
                for row in rows:
                    if row["type"].removesuffix("/A") != "N-CSR":
                        continue
                    try:
                        result.append(self.resolve_fund_report(row, cik, fund_class))
                    except (ValueError, TypeError, AttributeError) as error:
                        service._gap(
                            instrument,
                            "sec_fund_annual_row_invalid",
                            detail=str(error),
                            row=row,
                        )
                return result

            def _company_rows(self, columns, code, cik, start, end):
                required = ("accessionNumber", "filingDate", "form", "primaryDocument")
                forms = columns.get("form")
                if not isinstance(forms, list) or any(
                    not isinstance(columns.get(name), list)
                    or len(columns[name]) != len(forms)
                    for name in required
                ):
                    raise SourceError(f"{code}: incomplete SEC filing columns")
                names = [*required]
                for name in ("primaryDocDescription", "reportDate"):
                    if name in columns:
                        if not isinstance(columns[name], list) or len(
                            columns[name]
                        ) != len(forms):
                            raise SourceError(f"{code}: incomplete SEC {name} columns")
                        names.append(name)
                result = []
                for index, form in enumerate(forms):
                    # E.g. an 8-K's future shareholder-meeting reportDate must
                    # never block unrelated valid annuals before this filter.
                    if not isinstance(form, str) or form.removesuffix("/A") not in {
                        "10-K",
                        "20-F",
                        "40-F",
                    }:
                        continue
                    selected = {name: [columns[name][index]] for name in names}
                    try:
                        result.extend(
                            super()._company_rows(selected, code, cik, start, end)
                        )
                    except (ValueError, TypeError, AttributeError) as error:
                        service._gap(
                            instrument,
                            "sec_annual_row_invalid",
                            detail=str(error),
                            row={name: values[0] for name, values in selected.items()},
                        )
                return result

        provider = AnnualSecProvider(self._config(), http=_OfficialHTTP(self))
        start, end = self._years(as_of)
        rows = provider.fetch(instrument.code, date(start, 1, 1), as_of)
        result = []
        for row in rows:
            form = row["type"]
            if form.removesuffix("/A") not in {"10-K", "20-F", "40-F", "N-CSR"}:
                continue
            period = row.get("reportDate")
            if not period:
                self._gap(instrument, "sec_annual_report_period_missing", row=row)
                continue
            year = date.fromisoformat(period).year
            if not start <= year <= end:
                continue
            self._validate_url(instrument, row["url"])
            accession = row["url"].split("/")[-2]
            result.append(
                AnnualDocument(
                    instrument.key,
                    self._issuer(instrument),
                    year,
                    "sec:" + accession + ":" + row["url"].split("/")[-1],
                    url=row["url"],
                    published_at=row["date"],
                    period_end=period,
                    kind="amendment" if form.endswith("/A") else "annual",
                    content_type="application/pdf"
                    if urlsplit(row["url"]).path.lower().endswith(".pdf")
                    else "text/html",
                    profiles=self.spec.profiles.get(instrument.key, []),
                    provenance={
                        "source": "sec",
                        "form": form,
                        "report_series": form.removesuffix("/A") + ":" + period,
                        "accession": accession,
                        "official_directory_row": row,
                        "fiscal_year_basis": "official_report_period_end_year",
                    },
                )
            )
        return result

    @staticmethod
    def _validate_url(instrument, url):
        parsed = urlsplit(url)
        allowed = {
            "a_share": {"static.cninfo.com.cn", "www.sse.com.cn", "static.sse.com.cn"},
            "hk": {"www.hkexnews.hk", "www1.hkexnews.hk"},
            "us": {"www.sec.gov", "data.sec.gov"},
            "sg": {"links.sgx.com"},
        }[instrument.market]
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed
            or parsed.username
            or parsed.password
        ):
            raise SourceError("nonofficial_annual_source_url")

    def acquire(self, document: AnnualDocument) -> AnnualDocument:
        instrument = Instrument.parse(document.instrument)
        if document.local_path and Path(document.local_path).is_file():
            digest = file_sha(Path(document.local_path))
            if document.sha256 and document.sha256 != digest:
                raise SourceError("cached_original_hash_changed")
            self.hub.register_original(
                document.local_path,
                url=document.url,
                version=document.document_id,
                expected_sha=digest,
            )
            result = replace(document, sha256=digest)
            register_document(self.root, result)
            self._local = None
            return result
        self._validate_url(instrument, document.url)
        existing = self._reuse_candidate(document, self.local_documents())
        if (
            existing is not None
            and existing.local_path
            and Path(existing.local_path).is_file()
        ):
            result = replace(
                document,
                local_path=existing.local_path,
                sha256=existing.sha256,
                profiles=document.profiles or existing.profiles,
                period_start=document.period_start or existing.period_start,
                period_end=document.period_end or existing.period_end,
                currency=document.currency or existing.currency,
                provenance={
                    **existing.provenance,
                    **document.provenance,
                    "legacy_document_id": existing.document_id,
                },
            )
            self.hub.register_original(
                result.local_path,
                url=result.url,
                version=result.document_id,
                expected_sha=result.sha256,
            )
            register_document(self.root, result)
            self._local = None
            return result
        attempt_key = fingerprint(
            [document.issuer_id, document.document_id, document.sha256, document.url]
        )
        attempt_file = self.root / "source_attempts" / f"{attempt_key}.json"
        if attempt_file.exists():
            raise SourceError(
                "source_attempt_not_retried:"
                + load(attempt_file).get("status", "unknown")
            )
        # Import a verified historical original without moving or rewriting it.
        legacy_folder = (
            self.root / "originals" / fingerprint([document.url, document.document_id])
        )
        legacy = legacy_folder / (
            "original.html" if "html" in document.content_type else "original.pdf"
        )
        if legacy.is_file():
            receipt = legacy_folder / "receipt.json"
            if not receipt.is_file() or load(receipt)["sha256"] != file_sha(legacy):
                raise SourceError("immutable_original_cache_corrupt")
        invoked = False
        attempt = {
            "started_at": now(),
            "status": "sending",
            "requests": 1,
            "document": document.to_dict(),
            "automatic_retries": 0,
        }

        def download(target):
            nonlocal invoked
            invoked = True
            atomic_write(attempt_file, attempt, exclusive=True)
            if self.downloader:
                return self.downloader(document, target)
            if instrument.market == "us":
                return self._download_sec(document.url, target)
            from src.data.dataset_documents import download_document

            return download_document(
                document.url,
                target,
                time.monotonic() + 600,
                threading.Event(),
                lambda _deadline: self._throttle(document.url),
                max_bytes=None,
                minimum_free_bytes=10 * 1024**3,
            )

        try:
            stored = self.hub.ensure_original(
                document.url,
                download,
                version=document.document_id,
                expected_sha=document.sha256,
                content_type=document.content_type,
                local_path=legacy if legacy.is_file() else None,
            )
        except Exception as error:
            if invoked:
                shared_attempt = (
                    self.hub.root
                    / "attempts"
                    / (fingerprint([document.url, document.document_id]) + ".json")
                )
                raw = load(shared_attempt) if shared_attempt.exists() else {}
                atomic_write(
                    attempt_file,
                    {
                        **attempt,
                        "status": raw.get("status", "failed"),
                        "finished_at": now(),
                        "error": f"{type(error).__name__}:{error}",
                        **{
                            k: raw[k]
                            for k in ("raw_path", "raw_sha256", "raw_bytes")
                            if k in raw
                        },
                    },
                )
            if isinstance(error, SourceError):
                raise
            raise SourceError(str(error)) from error
        if invoked:
            atomic_write(
                attempt_file,
                {
                    **attempt,
                    "status": "complete",
                    "finished_at": now(),
                    "sha256": stored["sha256"],
                    "path": stored["path"],
                },
            )
        result = replace(
            document,
            local_path=stored["path"],
            sha256=stored["sha256"],
            provenance={
                **document.provenance,
                "material_authority": self.authority.to_dict(),
            },
        )
        register_document(self.root, result)
        self._local = None
        return result

    def _download_sec(self, url, target):
        from src.instruments.providers import SecCompanyFactsProvider

        agent = SecCompanyFactsProvider(self._config()).sec_user_agent
        if not agent:
            raise SourceError("SEC_USER_AGENT_required")
        response = self._request(
            "get",
            url,
            headers={"User-Agent": agent},
            timeout=(20, 60),
            stream=True,
            allow_redirects=False,
        )
        try:
            response.raise_for_status()
            if response.status_code != 200:
                raise SourceError("sec_original_redirect_or_incomplete")
            expected = response.headers.get("Content-Length")
            size, deadline = 0, time.monotonic() + 600
            with target.open("xb") as stream:
                for block in response.iter_content(1024 * 1024):
                    if time.monotonic() >= deadline:
                        raise SourceError("sec_original_download_timed_out")
                    size += len(block)
                    stream.write(block)
            if expected and size != int(expected):
                raise SourceError("sec_original_incomplete_body")
        finally:
            response.close()

    def quote(self, instrument: Instrument, as_of: date) -> dict:
        if self.quoter:
            result = dict(self.quoter(instrument, as_of))
        elif self.spec.market_snapshot:
            result = self._snapshot_quote(instrument)
        elif instrument.market == "sg":
            from src.data.sgx import SGXSource

            config = self._config()
            config = {**config, "storage": {
                **config.get("storage", {}), "authority_root": str(self.authority.root)
            }}
            result = SGXSource(config).quote(instrument.code + ".SI")
            result.update(price=result["close"], price_basis="unadjusted")
        else:
            from src.data.quote_time import parse_quote_time

            symbol = {
                "a_share": (
                    "sh" if instrument.code.startswith(("5", "6", "9")) else "sz"
                )
                + instrument.code,
                "hk": "hk" + instrument.code,
                "us": "us" + instrument.code,
            }[instrument.market]
            response = self._request(
                "get", "https://qt.gtimg.cn/q=" + symbol, timeout=15
            )
            response.raise_for_status()
            from src.data_hub.quotes import record_tencent, require_tencent_quote

            response = record_tencent(
                response, "https://qt.gtimg.cn/q=" + symbol, self.authority
            )
            parts = require_tencent_quote(response, symbol, min_fields=31).fields
            if len(parts) <= 30:
                raise SourceError("live_quote_incomplete")
            clock = parse_quote_time(
                parts[30], instrument.code, market=instrument.market
            )
            if clock is None:
                raise SourceError("live_quote_source_time_missing")
            result = {
                "price": parts[3],
                "quote_time": clock.isoformat(),
                "date": clock.date().isoformat(),
                "currency": {"a_share": "CNY", "hk": "HKD", "us": "USD"}[
                    instrument.market
                ],
                "source": "qq_realtime",
                "source_url": "https://qt.gtimg.cn/q=" + symbol,
                "price_basis": "unadjusted",
            }
        price = result.get("price", result.get("raw_close", result.get("close")))
        if price is None or not math.isfinite(float(price)) or float(price) <= 0:
            raise SourceError("quote_price_unavailable")
        clock = result.get("quote_time") or result.get("date")
        if not clock or str(clock)[:10] > as_of.isoformat():
            raise SourceError("quote_source_time_missing_or_future")
        if not result.get("currency"):
            raise SourceError("quote_currency_not_declared")
        return {
            **result,
            "instrument": instrument.key,
            "market": instrument.market,
            "code": instrument.code,
            "issuer_id": self._issuer(instrument),
            "price": str(price),
            "date": str(clock)[:10],
        }

    def _snapshot_quote(self, instrument):
        path = Path(self.spec.market_snapshot)
        if path.suffix.lower() == ".json":
            value = load(path)
            rows = (
                value
                if isinstance(value, list)
                else value.get("quotes", value.get("rows", [value]))
            )
            matches = [
                r
                for r in rows
                if r.get("instrument") == instrument.key
                or (
                    str(r.get("code")) == instrument.code
                    and r.get("market", instrument.market) == instrument.market
                )
            ]
        else:
            matches = []
            for line in path.read_text("utf-8").splitlines():
                cells = [v.strip() for v in line.strip().strip("|").split("|")]
                if len(cells) >= 4 and cells[0] in {instrument.key, instrument.code}:
                    matches.append(
                        {
                            "price": cells[2],
                            "date": cells[1],
                            "currency": cells[3],
                            "source": str(path),
                        }
                    )
        if len(matches) != 1:
            raise SourceError("market_snapshot_instrument_missing_or_ambiguous")
        return dict(matches[0])
