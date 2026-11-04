"""Official SGX disclosures shared by reports, triggers and annual research."""

from __future__ import annotations

import codecs
import json
import math
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from ..data_hub import DataHub
from ..data_hub.http import AuthorityHTTP
from ..platform_composition import authority_for

ZONE = ZoneInfo("Asia/Singapore")
CONFIG_URL = "https://www.sgx.com/config/appconfig.json"
LINK_HOST = "links.sgx.com"


def security_code(value: str) -> str:
    code = str(value).strip().upper().removeprefix("SG:")
    code = code.removesuffix(".SI").removesuffix("-SI")
    if not re.fullmatch(r"[A-Z0-9]{1,10}", code):
        raise ValueError("invalid_sgx_security_code")
    return code


def official_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != LINK_HOST
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith(("/1.0.0/corporate-announcements/", "/FileOpen/"))
    ):
        raise ValueError("nonofficial_sgx_document_url")
    return url


def fields(soup: BeautifulSoup) -> dict[str, list[str]]:
    result = {}
    for term in soup.find_all("dt"):
        value = term.find_next_sibling("dd")
        if value is not None:
            key = re.sub(r"\s+", " ", term.get_text(" ", strip=True)).casefold()
            result.setdefault(key, []).append(value.get_text(" ", strip=True))
    return result


def unique(values: list[str], label: str, *, required=True) -> str:
    values = list(dict.fromkeys(v for v in values if v))
    if len(values) != 1:
        if not values and not required:
            return ""
        raise ValueError(f"sgx_missing_or_conflicting_{label}")
    return values[0]


class SGXDisclosureProvider:
    """A bounded directory reader; a failed/truncated window is never empty success.

    ``cache_until`` is supplied by application composition for a collection
    batch. Without it, explicit research discovery performs a new live check.
    Originals and failures still share the existing material authority.
    """

    def __init__(
        self,
        config=None,
        *,
        http=None,
        authority=None,
        hub=None,
        timeout=30,
        cache_until=None,
        deadline=None,
        cancelled=None,
    ):
        self.config = config or {}
        self.authority = authority or authority_for(
            Path(__file__).resolve().parents[2], self.config
        )
        self.hub = hub or DataHub(self.authority)
        self.http = http or AuthorityHTTP(self.authority)
        self.timeout = timeout
        self.deadline = deadline if deadline is not None else time.monotonic() + 180
        self.cancelled = cancelled
        observed = datetime.now(ZONE)
        self.observed_at = observed
        self.version = (
            cache_until(observed).isoformat() if cache_until else observed.isoformat()
        )
        self._endpoints = None
        self._token = None

    def _get(self, url, **kwargs):
        if self.cancelled and self.cancelled():
            raise ValueError("sgx_collection_cancelled")
        timeout = self.timeout
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("sgx_collection_deadline_exhausted")
        if isinstance(self.http, AuthorityHTTP):
            kwargs.update(deadline=self.deadline, cancel=self.cancelled)
        response = self.http.get(url, timeout=timeout, allow_redirects=False, **kwargs)
        response.raise_for_status()
        return response

    def _public_session(self):
        if self._endpoints is None:
            response = self._get(CONFIG_URL)
            if response.status_code != 200:
                raise ValueError("sgx_public_configuration_unavailable")
            config = response.json()
            endpoints = config["endpoints"]
            if (
                endpoints.get("ANNOUNCEMENTS_API_URL")
                != "https://api.sgx.com/announcements/v1.1/"
                or endpoints.get("CMS_API_URL") != "https://api2.sgx.com/content-api"
            ):
                raise ValueError("sgx_public_endpoints_changed")
            url = (
                endpoints["CMS_API_URL"]
                + "/?"
                + urlencode(
                    {"queryId": str(config["CMS_VERSION"]) + ":we_chat_qr_validator"}
                )
            )
            response = self._get(url)
            if response.status_code != 200:
                raise ValueError("sgx_public_session_unavailable")
            value = response.json().get("data", {}).get("qrValidator")
            if not isinstance(value, str) or not value:
                raise ValueError("sgx_public_session_unavailable")
            # Same published anonymous-session format as SGX's website client.
            # Never persist session values or retry rejected credentials.
            self._token = codecs.decode(value, "rot_13")
            self._endpoints = endpoints
        return self._endpoints, {"authorizationToken": self._token}

    def _json(self, path: str, params: dict):
        url = "https://api.sgx.com/announcements/v1.1/" + path + "?" + urlencode(params)

        def download(target):
            _, headers = self._public_session()
            response = self._get(url, headers=headers)
            if response.status_code != 200:
                raise ValueError("sgx_directory_http_not_200")
            payload = response.json()
            if str(payload.get("meta", {}).get("code")) != "200":
                raise ValueError("sgx_directory_business_error")
            target.write_bytes(response.content)

        stored = self.hub.ensure_original(
            url,
            download,
            version="sgx-directory:" + self.version,
            content_type="application/json",
        )
        return json.loads(Path(stored["path"]).read_bytes())

    def fetch(self, code: str, start: date, end: date, *, max_pages=32) -> list[dict]:
        symbol = security_code(code)
        if start > end or type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("invalid_sgx_disclosure_window")
        params = {
            "value": symbol,
            "periodstart": start.strftime("%Y%m%d_000000"),
            "periodend": end.strftime("%Y%m%d_235959"),
        }
        total = self._json("securitycode/count", params).get("data")
        if type(total) is not int or total < 0:
            raise ValueError("sgx_directory_count_missing")
        pages = max(1, math.ceil(total / 100))
        if pages > max_pages:
            raise ValueError("sgx_directory_page_limit_exceeded")
        records = {}
        for page in range(pages):
            rows = self._json(
                "securitycode", {**params, "pagestart": page, "pagesize": 100}
            ).get("data")
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError("sgx_directory_invalid_rows")
            for row in rows:
                matches = [
                    x for x in row.get("issuers", []) if x.get("stock_code") == symbol
                ]
                if len(matches) != 1 or not matches[0].get("isin_code"):
                    raise ValueError("sgx_directory_security_identity_mismatch")
                identity = matches[0]
                expected = (
                    self.config.get("instrument_catalog", {}).get(symbol + ".SI", {})
                    or {}
                ).get("isin")
                if expected and expected != identity["isin_code"]:
                    raise ValueError("sgx_directory_isin_mismatch")
                published = datetime.fromtimestamp(
                    float(row["broadcast_date_time"]) / 1000, timezone.utc
                ).astimezone(ZONE)
                if not start <= published.date() <= end or published > self.observed_at:
                    raise ValueError("sgx_directory_publication_outside_window")
                identifier = row.get("id")
                url = official_url(row["url"])
                if not re.fullmatch(r"[A-Z0-9]{10,32}", str(identifier)):
                    raise ValueError("sgx_directory_invalid_id")
                if identifier in records:
                    raise ValueError("sgx_directory_duplicate_id")
                records[identifier] = {
                    "stock_code": symbol + ".SI",
                    "exchange": "sgx",
                    "title": row["title"],
                    "type": row.get("category_name", ""),
                    "summary": row["title"],
                    "date": published.date().isoformat(),
                    "published_at": published.isoformat(),
                    "url": url,
                    "document_id": identifier,
                    "source": "sgx_official",
                    "security_identity": identity,
                    "source_record": row,
                }
        if len(records) != total:
            raise ValueError("sgx_directory_incomplete_or_changed")
        return sorted(
            records.values(),
            key=lambda x: (x["published_at"], x["document_id"]),
            reverse=True,
        )

    def detail(self, item: dict) -> dict:
        url = official_url(item["url"])

        def download(target):
            response = self._get(url)
            if response.status_code != 200 or not response.content:
                raise ValueError("sgx_disclosure_body_unavailable")
            target.write_bytes(response.content)

        stored = self.hub.ensure_original(
            url,
            download,
            version="sgx-body:" + item["document_id"],
            content_type="text/html",
        )
        soup = BeautifulSoup(Path(stored["path"]).read_bytes(), "html.parser")
        values = fields(soup)
        security = values.get("security", values.get("securities", []))
        identity = item["security_identity"]
        text = " ".join(security)
        if identity["isin_code"] not in text or not re.search(
            r"(?<![A-Z0-9])" + re.escape(identity["stock_code"]) + r"(?![A-Z0-9])", text
        ):
            raise ValueError("sgx_disclosure_body_identity_mismatch")
        published = datetime.strptime(
            unique(values.get("date &time of broadcast", []), "broadcast"),
            "%d-%b-%Y %H:%M:%S",
        ).replace(tzinfo=ZONE)
        if published.isoformat() != item["published_at"]:
            raise ValueError("sgx_disclosure_publication_mismatch")
        attachments = []
        for link in soup.select("a[href]"):
            href = urljoin(url, link["href"])
            if urlsplit(href).hostname == LINK_HOST and (
                ".pdf" in href.lower() or "/FileOpen/" in href
            ):
                attachments.append(
                    {"name": link.get_text(" ", strip=True), "url": official_url(href)}
                )
        return {
            **item,
            "fields": values,
            "attachments": attachments,
            "source_document": stored,
        }

    def annuals(self, code: str, start: date, end: date) -> list[dict]:
        result = []
        for item in self.fetch(code, start, end):
            if not re.search(
                r"annual report|full yearly results", item["title"], re.IGNORECASE
            ):
                continue
            detail = self.detail(item)
            values = detail["fields"]
            report_type = unique(
                values.get("report type", []), "report_type", required=False
            )
            description = " ".join(
                v
                for k, values in values.items()
                if k.startswith("description")
                for v in values
            )
            if report_type and report_type.casefold() != "annual report":
                continue
            if not report_type and not re.search(
                r"\bannual report\b", description, re.IGNORECASE
            ):
                continue
            period = unique(
                values.get(
                    "period ended", values.get("for financial period ended", [])
                ),
                "annual_period",
            )
            period_end = (
                datetime.strptime(period, "%d/%m/%Y").replace(tzinfo=ZONE).date()
            )
            candidates = [
                a
                for a in detail["attachments"]
                if re.search(
                    r"(?:^|[^A-Za-z])annual[ _-]?report(?:[^A-Za-z]|$)|(?:^|[^A-Za-z])AR[ _-]+20\d{2}",
                    a["name"],
                    re.IGNORECASE,
                )
                and not re.search(
                    r"sustainab|market review|letter|proxy", a["name"], re.IGNORECASE
                )
            ]
            if len(candidates) != 1:
                raise ValueError("sgx_annual_attachment_ambiguous")
            result.append(
                {
                    **detail,
                    "annual_attachment": candidates[0],
                    "period_end": period_end.isoformat(),
                }
            )
        return result
