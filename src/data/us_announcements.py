"""SEC disclosures for US companies and individual ETF share classes."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import date
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

FORM_LABELS = {
    "10-K": "年度报告",
    "10-Q": "季度报告",
    "8-K": "重大事项报告",
    "20-F": "年度报告",
    "40-F": "年度报告",
    "6-K": "境外发行人报告",
    "N-CSR": "基金年度股东报告",
    "N-CSRS": "基金半年度股东报告",
    "NPORT-P": "基金投资组合披露",
    "497": "基金招募说明书补充",
    "485BPOS": "基金注册文件更新",
    "DEF 14A": "股东大会委托书",
}
IMPORTANT_FORMS = frozenset(FORM_LABELS)


class SecAnnouncementProvider:
    MAX_ARCHIVES = 16
    MAX_FUND_PAGES = 20

    def __init__(self, config: dict | None = None, http=None):
        from ..instruments.providers import SecCompanyFactsProvider

        self.provider = SecCompanyFactsProvider(config or {}, http=http)
        self._funds = None
        self._last_request = 0.0

    def _get(self, url: str):
        # Stay comfortably below SEC's shared 10 requests/second ceiling.
        delay = 0.2 - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        try:
            return self.provider._sec_get(url)
        finally:
            self._last_request = time.monotonic()

    def _identity(self, symbol: str) -> tuple[str, str | None]:
        if self.provider._tickers is None:
            payload = self._get(self.provider.TICKERS_URL).json()
            self.provider._tickers = {
                str(row["ticker"]).upper().replace(".", "-"): str(row["cik_str"]).zfill(
                    10
                )
                for row in payload.values()
            }
        cik = self.provider._tickers.get(symbol)
        if cik:
            return cik, None
        if self._funds is None:
            data = self._get("https://www.sec.gov/files/company_tickers_mf.json").json()
            fields = data["fields"]
            self._funds = {}
            for values in data["data"]:
                if len(values) != len(fields):
                    raise ValueError("Incomplete SEC fund ticker directory")
                row = dict(zip(fields, values))
                ticker = str(row["symbol"]).upper().replace(".", "-")
                identity = (str(row["cik"]).zfill(10), str(row["classId"]))
                if ticker in self._funds and self._funds[ticker] != identity:
                    raise ValueError(f"Ambiguous SEC fund identity: {ticker}")
                self._funds[ticker] = identity
        if symbol not in self._funds:
            raise ValueError(f"SEC issuer not found for {symbol}")
        return self._funds[symbol]

    def fetch(self, code: str, start: date, end: date) -> list[dict]:
        symbol = code.strip().upper().replace(".", "-")
        if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,14}", symbol) or start > end:
            raise ValueError(f"Invalid SEC announcement request: {code}")
        cik, fund_class = self._identity(symbol)
        if not re.fullmatch(r"\d{10}", cik):
            raise ValueError(f"Invalid SEC issuer identity for {code}")
        if fund_class:
            if not re.fullmatch(r"C\d{9}", fund_class):
                raise ValueError(f"Invalid SEC fund class for {code}")
            records = self._fund_filings(code, cik, fund_class, start, end)
        else:
            payload = self._get(
                f"https://data.sec.gov/submissions/CIK{cik}.json"
            ).json()
            tickers = [
                str(t).upper().replace(".", "-") for t in payload.get("tickers", [])
            ]
            if str(payload.get("cik", "")).zfill(10) != cik or symbol not in tickers:
                raise ValueError(f"SEC submission issuer mismatch for {code}")
            filings = payload["filings"]
            records = self._company_rows(filings["recent"], code, cik, start, end)
            archives = [
                item
                for item in filings.get("files", [])
                if date.fromisoformat(item["filingTo"]) >= start
                and date.fromisoformat(item["filingFrom"]) <= end
            ]
            if len(archives) > self.MAX_ARCHIVES:
                raise ValueError(f"{code}: SEC archive query limit exceeded")
            for archive in archives:
                name = archive["name"]
                if not re.fullmatch(rf"CIK{cik}-submissions-\d+\.json", name):
                    raise ValueError(f"Invalid SEC archive filename: {name}")
                older = self._get(f"https://data.sec.gov/submissions/{name}").json()
                records.extend(self._company_rows(older, code, cik, start, end))
        unique = {row["url"]: row for row in records}
        return sorted(
            unique.values(), key=lambda r: (r["date"], r["url"]), reverse=True
        )

    @staticmethod
    def _record(
        code: str, form: str, filed: date, url: str, description: str = ""
    ) -> dict:
        label = FORM_LABELS.get(form.removesuffix("/A"), "SEC 披露")
        return {
            "stock_code": code,
            "exchange": "sec",
            "date": filed.isoformat(),
            "title": f"{form} · {label}" + (f" · {description}" if description else ""),
            "type": form,
            "url": url,
        }

    def _company_rows(
        self, columns: dict, code: str, cik: str, start: date, end: date
    ) -> list[dict]:
        keys = ("accessionNumber", "filingDate", "form", "primaryDocument")
        count = len(columns["accessionNumber"])
        if any(
            not isinstance(columns.get(k), list) or len(columns[k]) != count
            for k in keys
        ):
            raise ValueError(f"{code}: incomplete SEC filing columns")
        descriptions = columns.get("primaryDocDescription", [""] * count)
        if len(descriptions) != count:
            raise ValueError(f"{code}: incomplete SEC filing descriptions")
        report_dates = columns.get("reportDate", [""] * count)
        if not isinstance(report_dates, list) or len(report_dates) != count:
            raise ValueError(f"{code}: incomplete SEC report dates")
        result = []
        for index in range(count):
            filed = date.fromisoformat(columns["filingDate"][index])
            if not start <= filed <= end:
                continue
            accession = columns["accessionNumber"][index]
            document = columns["primaryDocument"][index]
            if (
                not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)
                or not document
                or document.startswith("/")
                or any(p in {".", ".."} for p in document.split("/"))
                or "\\" in document
                or ":" in document
            ):
                raise ValueError(f"{code}: invalid SEC document path")
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{accession.replace('-', '')}/{quote(document, safe='/')}"
            )
            record = self._record(
                code, columns["form"][index], filed, url, descriptions[index]
            )
            if report_dates[index]:
                report_date = date.fromisoformat(report_dates[index])
                if report_date > filed:
                    raise ValueError(f"{code}: SEC report date after filing")
                record["reportDate"] = report_date.isoformat()
            result.append(record)
        return result

    def _fund_filings(
        self, code: str, cik: str, fund_class: str, start: date, end: date
    ) -> list[dict]:
        records = []
        for page in range(self.MAX_FUND_PAGES):
            # A trust's submissions include other funds. Query the exact class.
            query = urlencode(
                {
                    "action": "getcompany",
                    "CIK": fund_class,
                    "owner": "exclude",
                    "count": 100,
                    "start": page * 100,
                    "dateb": end.strftime("%Y%m%d"),
                    "output": "atom",
                }
            )
            response = self._get(f"https://www.sec.gov/cgi-bin/browse-edgar?{query}")
            soup = BeautifulSoup(response.content, "xml")
            issuer = soup.find("company-info")
            found = issuer.find("cik") if issuer else None
            if found is None or found.get_text(strip=True).zfill(10) != cik:
                raise ValueError(f"{code}: SEC fund feed issuer mismatch")
            entries = soup.find_all("entry")
            dates = []
            for entry in entries:
                filed = date.fromisoformat(
                    entry.find("filing-date").get_text(strip=True)
                )
                dates.append(filed)
                if not start <= filed <= end:
                    continue
                url = entry.find("filing-href").get_text(strip=True)
                parsed = urlsplit(url)
                prefix = f"/Archives/edgar/data/{int(cik)}/"
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != "www.sec.gov"
                    or not parsed.path.startswith(prefix)
                    or "/../" in parsed.path
                ):
                    raise ValueError(f"{code}: invalid SEC fund document URL")
                form = entry.find("filing-type").get_text(strip=True)
                records.append(self._record(code, form, filed, url))
            if len(entries) < 100 or (dates and min(dates) < start):
                return records
        raise ValueError(f"{code}: SEC fund query limit exceeded")

    def resolve_fund_report(self, record: dict, cik: str, fund_class: str) -> dict:
        """Resolve an exact-class filing index to its dated N-CSR original.

        The ordinary announcement feed stays lightweight. Report consumers ask
        for this metadata explicitly, through the same SEC transport and policy.
        """
        index_url = record["url"]
        index = urlsplit(index_url)
        prefix = f"/Archives/edgar/data/{int(cik)}/"
        if (
            record["type"].removesuffix("/A") != "N-CSR"
            or index.scheme != "https"
            or index.netloc != "www.sec.gov"
            or not re.fullmatch(
                re.escape(prefix) + r"\d{18}/\d{10}-\d{2}-\d{6}-index\.html?",
                index.path,
            )
        ):
            raise ValueError("Invalid SEC fund annual index")
        response = self._get(index_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        # A filing may contain several funds. Confirm the requested class and
        # ticker on the same class row, rather than accepting its parent trust.
        classes = [
            row
            for row in soup.select("tr")
            if any(a.get_text(strip=True) == fund_class for a in row.select("a"))
            and record["stock_code"].upper().replace(".", "-")
            in [
                cell.get_text(strip=True).upper().replace(".", "-")
                for cell in row.find_all("td", recursive=False)
            ]
        ]
        if len(classes) != 1:
            raise ValueError("SEC fund annual class identity missing or ambiguous")
        periods = []
        for heading in soup.select(".infoHead"):
            if heading.get_text(strip=True) == "Period of Report":
                value = heading.find_next_sibling("div", class_="info")
                if value:
                    periods.append(date.fromisoformat(value.get_text(strip=True)))
        if len(periods) != 1 or periods[0] > date.fromisoformat(record["date"]):
            raise ValueError("SEC fund annual report period missing or invalid")
        documents = []
        for row in soup.select('table[summary="Document Format Files"] tr'):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 4 or cells[3].get_text(strip=True) != record["type"]:
                continue
            link = cells[2].find("a", href=True)
            if link is None:
                continue
            url = urlsplit(urljoin(index_url, link["href"]))
            if url.path in {"/ix", "/ixviewer/doc/action"}:
                targets = parse_qs(url.query).get("doc", [])
                if (
                    url.scheme != "https"
                    or url.netloc != "www.sec.gov"
                    or len(targets) != 1
                ):
                    raise ValueError("Invalid SEC fund annual viewer URL")
                url = urlsplit(urljoin(index_url, targets[0]))
            if (
                url.scheme != "https"
                or url.netloc != "www.sec.gov"
                or not url.path.startswith(index.path.rsplit("/", 1)[0] + "/")
                or any(part in {".", ".."} for part in url.path.split("/"))
                or not url.path.lower().endswith((".htm", ".html", ".pdf"))
                or url.query
                or url.fragment
            ):
                raise ValueError("Invalid SEC fund annual document URL")
            documents.append(url.geturl())
        if len(documents) != 1:
            raise ValueError("SEC fund annual original missing or ambiguous")
        return {
            **record,
            "url": documents[0],
            "reportDate": periods[0].isoformat(),
            "filing_index_url": index_url,
            "fund_class": fund_class,
            "filing_index_sha256": hashlib.sha256(response.content).hexdigest(),
        }
