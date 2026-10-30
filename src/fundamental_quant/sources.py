"""Bounded, provenance-preserving discovery and research input acquisition.

Parsed PIT values are *not* primary evidence. Their serialized provider record
supports mechanical screening, while original-report verification remains open.
Discovery never substitutes the manually configured watchlist for the broad pool.
"""

# ruff: noqa: BLE001 -- every provider/parser exception becomes a visible envelope error

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import requests

logger = logging.getLogger(__name__)
DOCUMENT_PARSER_VERSION = "fq-pdf-text-4"
# Visually verified CNINFO publication watermark, not a financial-data override.
# Exact decoded image bytes are required; size/tag alone never establish that
# an image contains no financial information. See the real-PDF replay evidence.
_CNINFO_FOOTER_IMAGE_SHA256 = (
    "cb4b79012d9b1a5864d1bdebbfee0a4be298af263bbf46775af816a0775cf656"
)
CSI_A500_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
    "file/autofile/cons/000510cons.xls"
)
CNINFO_LIST_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
TENCENT_URL = "https://qt.gtimg.cn/q="
SSE_LIST_URL = "https://query.sse.com.cn/sseQuery/commonQuery.do"
SZSE_LIST_URL = "https://www.szse.cn/api/report/ShowReport"
BSE_LIST_URL = "https://www.bse.cn/nqxxController/nqxxCnzq.do"
SSE_REIT_CATALOG_URL = "https://star.sse.com.cn/js/common/ssesuggestreitsdata.js"
TEMPLATES = {"ordinary", "cyclical", "bank", "insurance", "reit"}


class SourceRetryBlocked(RuntimeError):
    """An unchanged source failure has exhausted its one automatic retry."""


# CSRC/CAPCO industry codes, never company-name heuristics. The broad PB groups
# follow the user's financial/property/utility/transport/resource/industrial
# routes. Financial subclasses lacking a dedicated template stay unclassified.
PB_INDUSTRY_SECTORS = {"B", "D", "E", "G", "J", "K"}
PB_INDUSTRY_CODES = {
    "C25",
    "C26",
    "C28",
    "C30",
    "C31",
    "C32",
    "C33",
    "C34",
    "C35",
    "C37",
}
CYCLICAL_INDUSTRY_CODES = {"C25", "C26", "C28", "C30", "C31", "C32", "C33", "G55"}
PIT_METRICS = {
    "total_shares",
    "parent_equity",
    "average_parent_equity",
    "revenue",
    "net_income_parent",
    "adjusted_net_income_parent",
    "basic_eps",
    "operating_cash_flow",
    "capital_expenditures",
    "free_cash_flow",
    "reported_roe",
    "cash_and_cash_equivalents",
    "restricted_cash",
    "short_term_borrowings",
    "current_portion_noncurrent_debt",
    "long_term_borrowings",
    "bonds_payable",
    "lease_liabilities",
    "interest_expense",
    "income_tax_expense",
    "profit_before_tax",
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _error(
    code: str,
    company: str,
    field: str,
    message: str,
    stage: str = "data_check",
    retry: str = "source_or_parser_change",
) -> dict:
    return {
        "code": code,
        "stage": stage,
        "field": field,
        "message": message,
        "company": company,
        "affected_conclusions": ["candidate_eligibility"],
        "retry_condition": retry,
    }


def _envelope(
    data: Any,
    sources: list,
    as_of: date,
    errors: list,
    coverage: dict,
    *,
    retryable: bool = False,
) -> dict:
    available = bool(data)
    if isinstance(data, dict) and "companies" in data:
        available = bool(data["companies"])
    return {
        "status": "partial"
        if errors and available
        else "unavailable"
        if errors
        else "success",
        "data": data,
        "source_refs": sources,
        "as_of": as_of.isoformat(),
        "coverage": coverage,
        "errors": errors,
        "retryable": retryable
        or any(
            e.get("retry_condition") in {"provider_recovery", "next_batch"}
            for e in errors
        ),
        "next_action": "resolve_input_gaps" if errors else "advance",
    }


class ResearchSources:
    """Cache-first adapter. Remote access is opt-in and bounded per invocation.

    ``sources.manifest_paths`` accept dated JSON manifests with ``companies``
    and ``coverage`` (a500/shanghai/shenzhen/beijing/reit boolean flags).
    Every manifest must record ``source_refs`` and ``as_of``; a manifest declaring
    complete A500 coverage must actually contain 500 distinct A500 members.

    Service callers supply ``reserve_discovery_issuer`` so discovering a quote
    consumes the same daily issuer allowance as financial collection. Standalone
    callers without it retain HTTP-bounded discovery and are explicitly reported
    as having no shared issuer accounting.
    """

    def __init__(
        self,
        config: dict,
        *,
        reserve_discovery_issuer: Callable[[str], bool] | None = None,
        discovery_priority: Callable[[str], int] | None = None,
    ):
        self.config = config
        self.reserve_discovery_issuer = reserve_discovery_issuer
        self.discovery_priority = discovery_priority
        self.settings = config.get("fundamental_quant", {}) or {}
        self.options = self.settings.get("sources", {}) or {}
        self.project_root = Path(self.options.get("project_root", ".")).resolve()
        self.root = Path(
            self.settings.get(
                "root", self.settings.get("output_dir", "data/fundamental_quant")
            )
        )
        self.cache = self.root / "source_cache"
        pit = config.get("point_in_time_data", {}) or {}
        self.pit_roots = [
            Path(p)
            for p in self.options.get(
                "pit_roots", [pit.get("output_dir", "data/point_in_time")]
            )
        ]
        if self.options.get("include_cached_reference_pit", True):
            reference_root = self.project_root / "data/reference_universe"
            # Discover once per adapter, not once per issuer. Restrict to the
            # repository's established dataset/fundamentals structure.
            inherited = sorted(
                path.parent
                for path in reference_root.glob("**/dataset/fundamentals")
                if path.is_dir()
                and path.resolve().is_relative_to(reference_root.resolve())
            )
            self.pit_roots = list(dict.fromkeys(self.pit_roots + inherited))
        self.remote = bool(self.options.get("allow_remote", False))
        self.max_pdf_pages = max(
            1, min(1000, int(self.options.get("max_pdf_pages", 600)))
        )
        self._industry_cache: dict[str, dict] = {}
        self._industry_errors: list[dict] = []
        self._industry_date: date | None = None
        self._discovery_requests_left: int | None = None
        self._document_errors: list[dict] = []
        self._pdf_page_cache: dict[tuple[str, str], list[tuple[str, bool]]] = {}

    def _load_industries(self, as_of: date) -> None:
        """Reuse the existing publication-dated official classification history."""
        if self._industry_date == as_of:
            return
        self._industry_date = as_of
        self._industry_cache = {}
        self._industry_errors = []
        defaults = sorted(
            (self.project_root / "data/reference_universe").glob(
                "industry_classification_history_official*.json"
            )
        )
        paths = [Path(p) for p in self.options.get("industry_paths", defaults)]
        from ..fundamental_embedding.industry_history import (
            IndustryClassificationHistoryStore,
        )

        for path in paths:
            try:
                labels = IndustryClassificationHistoryStore(path).labels_as_of(as_of)
                for code, label in labels.items():
                    match = re.match(r"^([A-Z]\d{2})", label.industry_code or "")
                    if not match:
                        continue
                    industry = match[1]
                    # Reject unauthoritative history masquerading as an official
                    # classification file. Provider snapshots can be configured
                    # explicitly as classifications rather than called official.
                    host = urlsplit(label.source).hostname or ""
                    if not any(
                        host == domain or host.endswith("." + domain)
                        for domain in ("csrc.gov.cn", "capco.org.cn")
                    ):
                        raise ValueError(
                            "official industry history has an untrusted origin"
                        )
                    old = self._industry_cache.get(code)
                    if (
                        old
                        and old["classification_as_of"]
                        > label.effective_from.isoformat()
                    ):
                        continue
                    template = (
                        "bank"
                        if industry == "J66"
                        else "insurance"
                        if industry == "J68"
                        else "cyclical"
                        if (
                            industry.startswith("B")
                            or industry in CYCLICAL_INDUSTRY_CODES
                        )
                        else "ordinary"
                    )
                    self._industry_cache[code] = {
                        "industry": label.industry_name,
                        "industry_code": industry,
                        "template": template,
                        "valuation_path": "pb"
                        if industry[0] in PB_INDUSTRY_SECTORS
                        or industry in PB_INDUSTRY_CODES
                        else "pe",
                        # J66 also includes monetary financial holding/leasing
                        # businesses. The official broad class alone does not
                        # establish that the bank-specific template is valid.
                        "classification_verified": industry
                        not in {"J66", "J67", "J69"},
                        "classification_warning": "financial_subtype_required"
                        if industry in {"J66", "J67", "J69"}
                        else "",
                        "classification_as_of": label.effective_from.isoformat(),
                        "classification_source": label.source,
                    }
            except Exception as exc:
                self._industry_errors.append(
                    _error("industry_history_invalid", "", str(path), str(exc))
                )

    def _read_json(self, path: Path) -> dict:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise TypeError("expected JSON object")
        return payload

    def _save_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Root workflow serializes source invocations; replace protects crash reads.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _company(self, row: dict) -> dict:
        raw_code = str(row.get("code", "")).strip()
        code = raw_code.zfill(6)
        if not raw_code or not re.fullmatch(r"\d{6}", code) or code == "000000":
            raise ValueError(f"invalid domestic issuer code: {code!r}")
        result = dict(row)
        is_reit = row.get("template") == "reit" or row.get("instrument_type") == "reit"
        market = "cn_reit" if is_reit else "a_share"
        result.update(code=code, id=f"{market}:{code}", market=market)
        result.setdefault("name", code)
        result.setdefault("template", "reit" if is_reit else "ordinary")
        result.setdefault("valuation_path", "pe")
        official = self._industry_cache.get(code)
        if official and not is_reit:
            result.update(official)
        classification = self.options.get("classifications", {}).get(code)
        if classification:
            result.update(
                {
                    k: classification[k]
                    for k in ("template", "valuation_path", "industry")
                    if k in classification
                }
            )
        result["classification_verified"] = bool(
            is_reit or classification or result.get("classification_verified")
        )
        if result["template"] not in TEMPLATES:
            raise ValueError("unknown industry template")
        if result["valuation_path"] not in {"pb", "pe"}:
            raise ValueError("unknown valuation path")
        return result

    def discover(self, as_of: date) -> dict:
        logger.info("FQ discovery begin as_of=%s remote=%s", as_of, self.remote)
        self._load_industries(as_of)
        errors, refs, members, exclusions = [], [], {}, {}
        quote_cache = {}
        errors.extend(self._industry_errors)
        coverage = dict.fromkeys(
            ("a500", "shanghai", "shenzhen", "beijing", "reit"), False
        )
        coverage_dates = {}
        active_listing_codes = {}
        paths = [Path(p) for p in self.options.get("manifest_paths", [])]
        if self.options.get("include_cached_reference_manifests", True):
            # Existing official CSI universes are supplemental issuer discoveries,
            # never a substitute for the required A500/full-market coverage.
            reference_root = self.project_root / "data/reference_universe"
            existing = sorted(reference_root.glob("index_constituents*.json"))
            existing.extend(sorted(reference_root.glob("*/index_constituents*.json")))
            paths = list(dict.fromkeys(existing + paths))
        cached = self.cache / "universe.json"
        if cached.exists():
            paths.insert(0, cached)
        manifests = []
        for path in paths:
            try:
                manifest = self._read_json(path)
                if manifest.get("contract") == "index-reference-universe-1":
                    from ..data.reference_universe_backfill import (
                        load_reference_manifest,
                    )

                    manifest = load_reference_manifest(path)
                    manifest["source_refs"] = [
                        {
                            "url": item["constituent_source"],
                            "hash": item.get("constituent_source_sha256"),
                            "as_of": item.get("member_as_of"),
                            "kind": "official_constituents",
                        }
                        for item in manifest.get("indices", [])
                        if item.get("constituent_source")
                    ]
                    manifest["coverage"] = {}
                    for row in manifest["companies"]:
                        row["a500"] = "csi_a500" in row.get("memberships", [])
                manifests.append(manifest)
            except Exception as exc:
                errors.append(
                    _error("manifest_unreadable", "", str(path), str(exc), "discovery")
                )
        if self.remote:
            try:
                live = self._discover_remote(as_of)
                manifests.append(live)
                errors.extend(live.get("errors", []))
            except Exception as exc:
                errors.append(
                    _error(
                        "discovery_failed",
                        "",
                        "universe",
                        str(exc),
                        "discovery",
                        "provider_recovery",
                    )
                )
            finally:
                self._discovery_requests_left = None
        for manifest in manifests:
            try:
                effective = date.fromisoformat(str(manifest.get("as_of", ""))[:10])
                if effective > as_of:
                    raise ValueError("future membership cannot enter current universe")
                manifest_refs = manifest.get("source_refs", [])
                if not manifest_refs:
                    raise ValueError("manifest lacks source references")
                for excluded in manifest.get("excluded_companies", []):
                    row = self._company(excluded)
                    if row.get("source_refs") and row.get("listing_status") in {
                        "inactive",
                        "out_of_universe",
                    }:
                        exclusions[row["id"]] = row
                rows = []
                for raw_row in manifest.get("companies", []):
                    try:
                        rows.append(self._company(raw_row))
                    except (ValueError, TypeError, AttributeError) as exc:
                        errors.append(
                            _error(
                                "manifest_company_invalid",
                                "",
                                "company",
                                str(exc),
                                "discovery",
                            )
                        )
                declared = manifest.get("coverage", {})
                for exchange, codes in manifest.get("active_listing_codes", {}).items():
                    listing_date = date.fromisoformat(
                        manifest.get("coverage_as_of", {}).get(
                            exchange, effective.isoformat()
                        )
                    )
                    if (
                        declared.get(exchange)
                        and codes
                        and 0
                        <= (as_of - listing_date).days
                        <= int(self.options.get("manifest_max_age_days", 7))
                    ):
                        active_listing_codes[exchange] = set(codes)
                a500 = {
                    row["id"]
                    for row in rows
                    if row.get("a500") or "csi_a500" in row.get("indices", [])
                }
                if declared.get("a500") and len(a500) != 500:
                    errors.append(
                        _error(
                            "a500_incomplete",
                            "",
                            "a500",
                            f"expected 500 members; got {len(a500)}",
                            "discovery",
                        )
                    )
                if declared.get("a500") and len(a500) == 500:
                    for existing in members.values():
                        existing["a500"] = existing["id"] in a500
                for key in coverage:
                    complete = bool(declared.get(key))
                    if key == "a500":
                        complete = complete and len(a500) == 500
                    dated = date.fromisoformat(
                        str(
                            manifest.get("coverage_as_of", {}).get(
                                key, effective.isoformat()
                            )
                        )
                    )
                    if (
                        not 0
                        <= (as_of - dated).days
                        <= int(self.options.get("manifest_max_age_days", 7))
                    ):
                        complete = False
                    coverage[key] = coverage[key] or complete
                    if complete:
                        coverage_dates[key] = max(
                            coverage_dates.get(key, ""), dated.isoformat()
                        )
                refs.extend(manifest_refs)
                for row in rows:
                    row.setdefault("membership_as_of", effective.isoformat())
                    old = members.get(row["id"], {})
                    # Supplemental older inventories must not overwrite a newer
                    # observed cap or unset established A500 membership.
                    if str(row.get("quote_as_of", "")) < str(
                        old.get("quote_as_of", "")
                    ):
                        for key in ("market_cap", "quote_as_of"):
                            if key in old:
                                row[key] = old[key]
                    row = {**old, **row}
                    code = row["code"]
                    if code not in quote_cache:
                        quote_cache[code] = {}
                        quote_path = self.cache / "quotes" / f"{code}.json"
                        if quote_path.exists():
                            try:
                                quote_cache[code] = self._read_json(quote_path)
                            except Exception as exc:
                                errors.append(
                                    _error(
                                        "cached_quote_invalid",
                                        row["id"],
                                        "market_cap",
                                        str(exc),
                                        "discovery",
                                    )
                                )
                    quote = quote_cache[code]
                    if quote.get("as_of") and str(quote["as_of"]) >= str(
                        row.get("quote_as_of", "")
                    ):
                        row.update(
                            market_cap=quote.get("market_cap"),
                            quote_as_of=quote["as_of"],
                        )
                    cap = _number(row.get("market_cap"))
                    quote_date = str(row.get("quote_as_of", ""))[:10]
                    fresh_cap = False
                    if quote_date:
                        quoted = date.fromisoformat(quote_date)
                        fresh_cap = 0 <= (as_of - quoted).days <= 7
                    a500_member = row["id"] in a500 or old.get("a500", False)
                    row["a500"] = a500_member
                    if (
                        row["template"] == "reit"
                        or a500_member
                        or not fresh_cap
                        or cap is None
                        or cap > 1_000_000_000
                    ):
                        row["size_status"] = (
                            "known" if cap is not None and fresh_cap else "pending"
                        )
                        members[row["id"]] = row
                    else:
                        # A genuine fresh below-threshold observation can remove
                        # a supplemental member; a failed/missing quote cannot.
                        members.pop(row["id"], None)
                        exclusions[row["id"]] = self._exclusion(
                            row,
                            as_of,
                            "market_cap_not_above_1b",
                            manifest_refs,
                            row.get("quote_as_of", effective.isoformat()),
                        )
            except Exception as exc:
                errors.append(
                    _error("manifest_invalid", "", "universe", str(exc), "discovery")
                )
        for key, complete in coverage.items():
            if not complete:
                errors.append(
                    _error(
                        "universe_coverage_gap",
                        "",
                        key,
                        f"current {key} membership is not established",
                        "discovery",
                        "fresh_official_manifest",
                    )
                )
        # A complete new A500 snapshot may remove a former member absent from
        # its rows; reapply the size gate after membership flags have settled.
        for issuer, row in list(members.items()):
            active = active_listing_codes.get(self._domestic_exchange(row["code"]))
            if row.get("template") != "reit" and active is not None:
                if row["code"] not in active and not row.get("a500"):
                    members.pop(issuer)
                    exclusions[issuer] = self._exclusion(
                        row,
                        as_of,
                        "absent_from_official_active_listing",
                        refs,
                        coverage_dates.get(
                            self._domestic_exchange(row["code"]), as_of.isoformat()
                        ),
                    )
                    continue
                row["listing_status"] = "active"
                row["listing_verified"] = True
                row["listing_basis"] = "official_exchange_active_listing"
            cap = _number(row.get("market_cap"))
            quoted = str(row.get("quote_as_of", ""))[:10]
            fresh = bool(quoted) and 0 <= (as_of - date.fromisoformat(quoted)).days <= 7
            if (
                row.get("template") != "reit"
                and not row.get("a500")
                and fresh
                and cap is not None
                and cap <= 1e9
            ):
                members.pop(issuer)
                exclusions[issuer] = self._exclusion(
                    row,
                    as_of,
                    "market_cap_not_above_1b",
                    refs,
                    quoted,
                )
        for issuer in members:
            exclusions.pop(issuer, None)
        companies = sorted(members.values(), key=lambda row: row["id"])
        result = _envelope(
            {"companies": companies, "excluded_companies": list(exclusions.values())},
            refs,
            as_of,
            errors,
            {
                **coverage,
                "company_count": len(companies),
                "listing_pending": sum(
                    c.get("listing_status") == "pending" for c in companies
                ),
                "size_pending": sum(c["size_status"] == "pending" for c in companies),
                "discovery_quote_accounting": "shared_issuer_budget"
                if self.reserve_discovery_issuer is not None
                else "standalone_http_budget_only",
                "quote_budget_deferred": sum(
                    c.get("quote_refresh_status") == "budget_deferred"
                    for c in companies
                ),
            },
        )
        if self.remote and companies:
            self._save_json(
                cached,
                {
                    "companies": companies,
                    "as_of": as_of.isoformat(),
                    "coverage": coverage,
                    "coverage_as_of": coverage_dates,
                    "active_listing_codes": {
                        exchange: sorted(codes)
                        for exchange, codes in active_listing_codes.items()
                    },
                    "source_refs": refs,
                    "excluded_companies": list(exclusions.values()),
                },
            )
        logger.info(
            "FQ discovery finished companies=%s errors=%s",
            len(companies),
            len(errors),
        )
        return result

    @staticmethod
    def _exclusion(
        row: dict, as_of: date, reason: str, refs: list, effective: str
    ) -> dict:
        return {
            **row,
            "listing_status": "inactive"
            if reason == "absent_from_official_active_listing"
            else "out_of_universe",
            "exclusion_reason": reason,
            "as_of": as_of.isoformat(),
            "effective_date": effective,
            "source_refs": refs,
        }

    def _get(self, url: str) -> requests.Response:
        self._consume_discovery_request()
        headers = {"User-Agent": "Mozilla/5.0"}
        if urlsplit(url).hostname == "query.sse.com.cn":
            headers["Referer"] = "https://www.sse.com.cn/assortment/stock/list/share/"
        response = requests.get(url, timeout=(5, 25), headers=headers)
        response.raise_for_status()
        if len(response.content) > 25 * 1024 * 1024:
            raise ValueError("source response exceeds 25 MiB")
        return response

    def _consume_discovery_request(self) -> None:
        if self._discovery_requests_left is not None:
            if self._discovery_requests_left <= 0:
                raise RuntimeError("discovery request budget exhausted")
            self._discovery_requests_left -= 1

    def _post(self, url: str, data: dict) -> requests.Response:
        self._consume_discovery_request()
        response = requests.post(
            url, data=data, timeout=(5, 25), headers={"User-Agent": "Mozilla/5.0"}
        )
        response.raise_for_status()
        if len(response.content) > 25 * 1024 * 1024:
            raise ValueError("source response exceeds 25 MiB")
        return response

    def _exchange_listing(self, exchange: str, as_of: date) -> tuple[list, list]:
        """Fetch full official exchange lists and prove pagination completion.

        A500/CNINFO remain independent fallbacks. A failing exchange cannot erase
        another exchange's rows or claim that an inventory is complete.
        """
        import pandas as pd

        companies, refs = {}, []

        def record(code, name, response, url):
            normalized = self._company(
                {"code": str(code).removesuffix(".0"), "name": str(name)}
            )
            code = normalized["code"]
            if code in companies:
                raise ValueError(f"duplicate {exchange} listing code: {code}")
            companies[code] = {
                "code": code,
                "name": str(name),
                "exchange": exchange,
                "membership_as_of": as_of.isoformat(),
            }

        def reference(response, url, count):
            refs.append(
                {
                    "url": url,
                    "hash": hashlib.sha256(response.content).hexdigest(),
                    "kind": "official_full_listing",
                    "as_of": as_of.isoformat(),
                    "exchange": exchange,
                    "row_count": count,
                }
            )

        if exchange == "shanghai":
            for board in ("1", "8"):
                url = (
                    SSE_LIST_URL
                    + "?"
                    + urlencode(
                        {
                            "STOCK_TYPE": board,
                            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
                            "COMPANY_STATUS": "2,4,5,7,8",
                            "type": "inParams",
                            "isPagination": "true",
                            "pageHelp.pageSize": "10000",
                            "pageHelp.pageNo": "1",
                            "pageHelp.beginPage": "1",
                            "pageHelp.endPage": "1",
                            "pageHelp.cacheSize": "1",
                        }
                    )
                )
                response = self._get(url)
                payload = response.json()
                rows = payload.get("result", [])
                total = (payload.get("pageHelp") or {}).get("total")
                if total is None or not rows or int(total) != len(rows):
                    raise ValueError(
                        "SSE listing count missing or pagination incomplete"
                    )
                for row in rows:
                    record(row["A_STOCK_CODE"], row["SEC_NAME_CN"], response, url)
                reference(response, url, len(rows))
        elif exchange == "shenzhen":
            url = (
                SZSE_LIST_URL
                + "?"
                + urlencode({"SHOWTYPE": "xlsx", "CATALOGID": "1110", "TABKEY": "tab1"})
            )
            response = self._get(url)
            frame = pd.read_excel(io.BytesIO(response.content), dtype=str)
            if not {"A股代码", "A股简称"}.issubset(frame.columns) or frame.empty:
                raise ValueError("SZSE full workbook columns missing or empty")
            for _, row in frame.iterrows():
                record(row["A股代码"], row["A股简称"], response, url)
            reference(response, url, len(frame))
        elif exchange == "beijing":
            page, total_pages, total_elements = 0, None, None
            while total_pages is None or page < total_pages:
                response = self._post(
                    BSE_LIST_URL,
                    {
                        "page": str(page),
                        "typejb": "T",
                        "xxfcbj[]": "2",
                        "xxzqdm": "",
                        "sortfield": "xxzqdm",
                        "sorttype": "asc",
                    },
                )
                text = response.text
                data = json.loads(text[text.find("[") : text.rfind("]") + 1])[0]
                pages, elements = int(data["totalPages"]), int(data["totalElements"])
                if pages <= 0 or pages > 100 or elements <= 0:
                    raise ValueError("invalid BSE pagination totals")
                if total_pages is not None and (pages, elements) != (
                    total_pages,
                    total_elements,
                ):
                    raise ValueError("BSE inventory changed during pagination")
                total_pages, total_elements = pages, elements
                rows = data.get("content", [])
                if not rows:
                    raise ValueError("empty BSE listing page")
                for row in rows:
                    record(row["xxzqdm"], row["xxzqjc"], response, BSE_LIST_URL)
                reference(response, BSE_LIST_URL + f"?page={page}", len(rows))
                page += 1
            if len(companies) != total_elements:
                raise ValueError("BSE listing count does not match source total")
        else:
            raise ValueError(f"unsupported exchange {exchange}")
        return list(companies.values()), refs

    def _reit_discovery(self, as_of: date) -> tuple[list, dict]:
        """Discover SSE REITs from the exchange's dated security search catalog.

        This is useful automatic membership evidence, not an attestation that
        both exchanges' listed REIT universe has been completely collected.
        """
        response = self._get(SSE_REIT_CATALOG_URL)
        timestamp = re.search(r"staticDate=(\d{4}-\d{2}-\d{2})", response.text)
        if not timestamp or date.fromisoformat(timestamp[1]) > as_of:
            raise ValueError("REIT catalog date missing or future")
        matches = re.findall(
            r'_t\.push\(\{val:"(508\d{3})",val2:"[^"]*",val3:"([^"]+)"\}\)',
            response.text,
        )
        if not matches or len({code for code, _ in matches}) != len(matches):
            raise ValueError("REIT catalog empty or duplicate securities")
        return (
            [
                {
                    "code": code,
                    "name": name,
                    "template": "reit",
                    "membership_as_of": timestamp[1],
                    "exchange": "shanghai",
                }
                for code, name in matches
            ],
            {
                "url": SSE_REIT_CATALOG_URL,
                "hash": hashlib.sha256(response.content).hexdigest(),
                "as_of": timestamp[1],
                "kind": "official_reit_search_catalog",
                "row_count": len(matches),
                "complete_domestic_reit_universe": False,
            },
        )

    def _discover_remote(self, as_of: date) -> dict:
        import pandas as pd

        companies, refs, errors, excluded_companies = {}, [], [], []
        coverage = dict.fromkeys(
            ("a500", "shanghai", "shenzhen", "beijing", "reit"), False
        )
        self._discovery_requests_left = max(
            2, min(100, int(self.options.get("max_discovery_requests", 64)))
        )
        try:
            response = self._get(CSI_A500_URL)
            frame = pd.read_excel(io.BytesIO(response.content), dtype=str)

            def column(fragment):
                matches = [
                    c
                    for c in frame
                    if fragment.lower() in str(c).lower()
                    and "(eng)" not in str(c).lower()
                ]
                if len(matches) != 1:
                    raise ValueError(f"ambiguous A500 column: {fragment}")
                return matches[0]

            codes, names, dates = (
                column("Constituent Code"),
                column("Constituent Name"),
                column("Date"),
            )
            effective_dates = {pd.Timestamp(str(v)).date() for v in frame[dates]}
            if len(effective_dates) != 1 or next(iter(effective_dates)) > as_of:
                raise ValueError("invalid A500 effective date")
            effective = next(iter(effective_dates)).isoformat()
            for _, row in frame.iterrows():
                code = str(row[codes]).removesuffix(".0").zfill(6)
                companies[code] = {
                    "code": code,
                    "name": row[names],
                    "a500": True,
                    "membership_as_of": effective,
                }
            coverage["a500"] = len(companies) == 500
            refs.append(
                {
                    "url": CSI_A500_URL,
                    "hash": hashlib.sha256(response.content).hexdigest(),
                    "kind": "official_constituents",
                    "as_of": effective,
                }
            )
        except Exception as exc:
            errors.append(
                _error("a500_download_failed", "", "a500", str(exc), "discovery")
            )
        try:
            response = self._get(CNINFO_LIST_URL)
            rows = response.json().get("stockList", [])
            if not rows:
                raise ValueError("CNINFO inventory empty")
            for row in rows:
                code = str(row.get("code", ""))
                # Inventory is securities, not automatically equities. Only domestic
                # equity code families and identified public REIT units enter.
                reit = code.startswith(("508", "180"))
                if not re.fullmatch(r"\d{6}", code) or not (
                    code.startswith(
                        (
                            "000",
                            "001",
                            "002",
                            "003",
                            "300",
                            "301",
                            "600",
                            "601",
                            "603",
                            "605",
                            "688",
                            "920",
                        )
                    )
                    or reit
                ):
                    continue
                companies[code] = {
                    **companies.get(code, {}),
                    "code": code,
                    "name": row.get("zwjc", code),
                    "inventory_source": CNINFO_LIST_URL,
                    "inventory_as_of": as_of.isoformat(),
                    **({"template": "reit"} if reit else {}),
                }
            # CNINFO's file has no exchange-level completeness attestation. Keep
            # official exchange manifests required rather than infer completeness
            # from seeing one member of each exchange.
            refs.append(
                {
                    "url": CNINFO_LIST_URL,
                    "kind": "official_security_inventory",
                    "hash": hashlib.sha256(response.content).hexdigest(),
                    "as_of": as_of.isoformat(),
                }
            )
        except Exception as exc:
            errors.append(
                _error(
                    "inventory_download_failed", "", "inventory", str(exc), "discovery"
                )
            )
        active_listing_codes = {}
        for exchange in ("shanghai", "shenzhen", "beijing"):
            try:
                rows, official_refs = self._exchange_listing(exchange, as_of)
                if not rows:
                    raise ValueError("official active listing is empty")
                active_listing_codes[exchange] = {row["code"] for row in rows}
                for row in rows:
                    companies[row["code"]] = {**companies.get(row["code"], {}), **row}
                refs.extend(official_refs)
                coverage[exchange] = True
            except Exception as exc:
                errors.append(
                    _error(
                        "exchange_listing_failed",
                        "",
                        exchange,
                        str(exc),
                        "discovery",
                        "provider_recovery",
                    )
                )
        try:
            rows, reit_reference = self._reit_discovery(as_of)
            for row in rows:
                companies[row["code"]] = {**companies.get(row["code"], {}), **row}
            refs.append(reit_reference)
        except Exception as exc:
            errors.append(
                _error(
                    "reit_discovery_failed",
                    "",
                    "reit",
                    str(exc),
                    "discovery",
                    "provider_recovery",
                )
            )
        quote_dates = {}
        for code, company in list(companies.items()):
            active = active_listing_codes.get(self._domestic_exchange(code))
            if company.get("template") == "reit" or company.get("a500"):
                company["listing_status"] = "active"
                company["listing_verified"] = True
                company["listing_basis"] = (
                    "official_a500_membership"
                    if company.get("a500")
                    else "official_reit_listing"
                )
            elif active is not None:
                if code not in active:
                    companies.pop(code)
                    excluded_companies.append(
                        self._exclusion(
                            self._company(company),
                            as_of,
                            "absent_from_official_active_listing",
                            refs,
                            as_of.isoformat(),
                        )
                    )
                    continue
                company["listing_status"] = "active"
                company["listing_verified"] = True
                company["listing_basis"] = "official_exchange_active_listing"
            else:
                company["listing_status"] = "pending"
                company["listing_verified"] = False
                company["quote_refresh_status"] = "listing_unverified"
            path = self.cache / "quotes" / f"{code}.json"
            if not path.exists():
                continue
            try:
                saved = self._read_json(path)
                quoted = date.fromisoformat(saved["as_of"])
                if quoted > as_of:
                    raise ValueError("future cached quote")
                company.update(
                    market_cap=saved.get("market_cap"), quote_as_of=saved["as_of"]
                )
                if (
                    company["listing_status"] == "pending"
                    and company.get("inventory_source") == CNINFO_LIST_URL
                    and 0 <= (as_of - quoted).days <= 7
                    and _number(saved.get("price")) is not None
                    and saved["price"] > 0
                    and saved.get("code") == code
                    and saved.get("raw")
                ):
                    company.update(
                        listing_status="provisional",
                        listing_verified=False,
                        listing_basis="cninfo_inventory_and_recent_quote",
                        listing_uncertainty="full exchange active listing unavailable",
                    )
                quote_dates[code] = saved["as_of"]
            except Exception as exc:
                errors.append(
                    _error(
                        "cached_quote_invalid",
                        code,
                        "market_cap",
                        str(exc),
                        "discovery",
                    )
                )
        # Missing/oldest quotes first; a bounded run must not eternally fetch the
        # same lexicographic prefix while starving newly discovered issuers.
        codes = sorted(
            (
                code
                for code in companies
                if companies[code]["listing_status"] in {"active", "provisional"}
            ),
            key=lambda code: (
                self.discovery_priority(self._company(companies[code])["id"])
                if self.discovery_priority is not None
                else 0,
                quote_dates.get(code, ""),
                not companies[code].get("a500", False),
                code,
            ),
        )
        deferred_issuers = 0
        for offset in range(0, len(codes), 100):
            if self._discovery_requests_left <= 0:
                errors.append(
                    _error(
                        "discovery_budget_exhausted",
                        "",
                        "quotes",
                        "remaining market caps retained as pending",
                        "discovery",
                        "next_batch",
                    )
                )
                break
            authorized = []
            for code in codes[offset : offset + 100]:
                company = companies[code]
                allowed = self.reserve_discovery_issuer is None
                if self.reserve_discovery_issuer is not None:
                    issuer = self._company(company)["id"]
                    try:
                        allowed = self.reserve_discovery_issuer(issuer) is True
                    except Exception as exc:
                        errors.append(
                            _error(
                                "issuer_reservation_failed",
                                issuer,
                                "quote",
                                str(exc),
                                "discovery",
                                "budget_store_recovery",
                            )
                        )
                if allowed:
                    authorized.append(code)
                    company["quote_refresh_status"] = "reserved"
                else:
                    deferred_issuers += 1
                    company["quote_refresh_status"] = "budget_deferred"
            if not authorized:
                continue
            try:
                quotes = self._quotes(authorized, as_of)
                for code, quote in quotes.items():
                    if code not in authorized:
                        raise ValueError("quote provider returned an unreserved issuer")
                    self._save_json(self.cache / "quotes" / f"{code}.json", quote)
                    companies[code].update(
                        market_cap=quote.get("market_cap"),
                        quote_as_of=quote["as_of"],
                        quote_refresh_status="refreshed",
                    )
            except Exception as exc:
                errors.append(
                    _error(
                        "quote_batch_failed",
                        "",
                        f"quotes:{offset}",
                        str(exc),
                        "discovery",
                        "provider_recovery",
                    )
                )
        if deferred_issuers:
            errors.append(
                _error(
                    "discovery_issuer_budget_exhausted",
                    "",
                    "quotes",
                    f"{deferred_issuers} issuers retained without quote refresh; daily issuer budget not reserved",
                    "discovery",
                    "next_batch",
                )
            )
        return {
            "companies": list(companies.values()),
            "excluded_companies": excluded_companies,
            "coverage": coverage,
            "source_refs": refs,
            "errors": errors,
            "as_of": as_of.isoformat(),
            "active_listing_codes": {
                exchange: sorted(codes)
                for exchange, codes in active_listing_codes.items()
            },
        }

    @staticmethod
    def _domestic_exchange(code: str) -> str:
        if code.startswith(("4", "8", "92")):
            return "beijing"
        return "shanghai" if code.startswith(("6", "5")) else "shenzhen"

    def _quotes(self, codes: list[str], as_of: date) -> dict:
        symbols = [
            (
                "sh"
                if c.startswith(("6", "5"))
                else "bj"
                if c.startswith(("4", "8", "92"))
                else "sz"
            )
            + c
            for c in codes
        ]
        response = self._get(TENCENT_URL + ",".join(symbols))
        response.encoding = "gbk"
        result = {}
        for symbol, raw in re.findall(
            r'v_((?:sh|sz|bj)\d{6})="([^"]*)"', response.text
        ):
            fields = raw.split("~")
            if len(fields) < 47 or fields[2] != symbol[2:]:
                continue
            timestamp = fields[30]
            if not re.fullmatch(r"\d{14}", timestamp):
                continue
            quoted = date(int(timestamp[:4]), int(timestamp[4:6]), int(timestamp[6:8]))
            if quoted > as_of:
                continue
            item = {
                "code": symbol[2:],
                "as_of": quoted.isoformat(),
                "raw": raw,
                "url": TENCENT_URL + symbol,
                "fields": {},
            }
            for metric, index, multiplier in (
                ("price", 3, 1),
                ("pe_ttm", 39, 1),
                ("market_cap", 45, 1e8),
                ("pb", 46, 1),
            ):
                value = _number(fields[index])
                if value is not None:
                    item[metric] = value * multiplier
                    item["fields"][metric] = {
                        "raw_value": value,
                        "multiplier": multiplier,
                        "quote": fields[index],
                        "index": index,
                    }
            result[symbol[2:]] = item
        if not result:
            raise ValueError("no valid, dated Tencent quotes")
        return result

    def collect(self, company: dict, as_of: date, refresh: bool = False) -> dict:
        self._load_industries(as_of)
        company = (
            self._company(company)
            if company.get("market", "a_share") in {"a_share", "cn_reit"}
            else dict(company)
        )
        code, issuer = company["code"], company["id"]
        packet = {
            "company": company,
            "as_of": as_of.isoformat(),
            "facts": [],
            "sources": [],
            "peers": [],
        }
        errors = list(self._industry_errors)
        if refresh and self.remote:
            try:
                self._refresh(company, as_of)
            except SourceRetryBlocked as exc:
                errors.append(
                    _error(
                        "source_retry_blocked",
                        issuer,
                        "financials",
                        str(exc),
                        retry="relevant_evidence_or_contract_changes",
                    )
                )
            except Exception as exc:
                errors.append(
                    _error(
                        "issuer_refresh_failed",
                        issuer,
                        "financials",
                        str(exc),
                        retry="provider_recovery",
                    )
                )
        elif refresh:
            errors.append(
                _error(
                    "remote_disabled",
                    issuer,
                    "refresh",
                    "remote collection disabled; using existing sources",
                    retry="remote_enabled",
                )
            )
        quote_path = self.cache / "quotes" / f"{code}.json"
        if quote_path.exists():
            try:
                quote = self._read_json(quote_path)
                quote_date = date.fromisoformat(quote["as_of"])
                if quote_date > as_of or (as_of - quote_date).days > 7:
                    raise ValueError("quote is future-dated or older than seven days")
                sid = "quote:" + _digest(quote["raw"])
                packet["sources"].append(
                    {
                        "id": sid,
                        "issuer_id": issuer,
                        "period_end": quote["as_of"],
                        "published_at": quote["as_of"],
                        "kind": "quote",
                        "text": quote["raw"],
                        "url": quote["url"],
                        "page": 0,
                        "hash": _digest(quote["raw"]),
                        "origin_id": quote["url"],
                    }
                )
                for metric, info in quote.get("fields", {}).items():
                    raw = str(info["quote"])
                    decimals = len(raw.split(".")[1]) if "." in raw else 0
                    packet["facts"].append(
                        {
                            "id": f"{sid}:{metric}",
                            "metric": metric,
                            "value": info["raw_value"] * info["multiplier"],
                            "period_end": quote["as_of"],
                            "published_at": quote["as_of"],
                            "period_type": "instant",
                            "unit": "per_share"
                            if metric == "price"
                            else "currency"
                            if metric == "market_cap"
                            else "ratio",
                            "currency": "CNY",
                            "source_id": sid,
                            "quote": info["quote"],
                            "raw_value": info["raw_value"],
                            "multiplier": info["multiplier"],
                            "precision": 0.5 * 10**-decimals,
                        }
                    )
            except Exception as exc:
                errors.append(_error("quote_invalid", issuer, "quote", str(exc)))
        else:
            errors.append(
                _error("quote_missing", issuer, "quote", "no dated raw quote")
            )
        from ..instruments.point_in_time import PointInTimeFundamentalStore

        seen = set()
        for root in self.pit_roots:
            try:
                statements = PointInTimeFundamentalStore(root).as_of(code, as_of)
                for statement in statements:
                    raw = json.loads(statement.json())
                    provider_text = json.dumps(raw, sort_keys=True, ensure_ascii=False)
                    signature = _digest(provider_text)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    sid = "pit:" + signature
                    period = statement.period_end.isoformat()
                    published = statement.published_at.isoformat()
                    packet["sources"].append(
                        {
                            "id": sid,
                            "issuer_id": issuer,
                            "period_end": period,
                            "published_at": published,
                            "kind": "statement",
                            "text": provider_text,
                            "page": 0,
                            "url": statement.source_url or "",
                            "hash": signature,
                            "origin_id": statement.source_url or statement.source,
                            "verification_level": "provider_parsed",
                            "primary_verified": False,
                        }
                    )
                    for metric in sorted(PIT_METRICS):
                        value = _number(raw.get(metric))
                        if value is None:
                            continue
                        packet["facts"].append(
                            {
                                "id": f"{sid}:{metric}",
                                "metric": metric,
                                "value": value / 100.0
                                if metric == "reported_roe"
                                else value,
                                "period_end": period,
                                "published_at": published,
                                "period_type": "annual"
                                if statement.period_type == "year"
                                else "interim",
                                "is_cumulative": statement.is_cumulative,
                                "unit": "ratio"
                                if metric == "reported_roe"
                                else "shares"
                                if metric == "total_shares"
                                else "per_share"
                                if metric == "basic_eps"
                                else "currency",
                                "currency": statement.currency,
                                "source_id": sid,
                                "quote": json.dumps(metric)
                                + ": "
                                + json.dumps(raw[metric]),
                                "raw_value": value,
                                "multiplier": 0.01 if metric == "reported_roe" else 1,
                                "precision": 0,
                                "precision_scope": "provider_serialization",
                                "verification_level": "provider_parsed",
                                "primary_verified": False,
                            }
                        )
            except Exception as exc:
                errors.append(_error("pit_read_failed", issuer, str(root), str(exc)))
        if seen:
            errors.append(
                _error(
                    "primary_fact_evidence_required",
                    issuer,
                    "financials",
                    "PIT provider records support screening only; original report extraction must corroborate them",
                )
            )
        else:
            errors.append(
                _error(
                    "statements_missing",
                    issuer,
                    "financials",
                    "no disclosed PIT statements available",
                )
            )
        self._document_errors = []
        try:
            packet["sources"].extend(self._documents(company, as_of))
        except Exception as exc:
            errors.append(_error("document_read_failed", issuer, "reports", str(exc)))
        errors.extend(self._document_errors)
        if not any(s["kind"] in {"annual", "interim"} for s in packet["sources"]):
            errors.append(
                _error(
                    "original_documents_missing",
                    issuer,
                    "reports",
                    "no original report pages available; do not run narrative analysis",
                )
            )
        if not company.get("classification_verified"):
            errors.append(
                _error(
                    "industry_classification_required",
                    issuer,
                    "template",
                    company.get("classification_warning")
                    or "industry and valuation route require sourced classification",
                )
            )
        return _envelope(
            packet,
            [s["id"] for s in packet["sources"]],
            as_of,
            errors,
            {
                "facts": len(packet["facts"]),
                "statements": len(seen),
                "original_pages": sum(s["page"] > 0 for s in packet["sources"]),
            },
        )

    def _refresh(self, company: dict, as_of: date) -> None:
        code = company["code"]
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("remote adapter requires a domestic six-digit code")
        failures = []
        fresh_quote = {}
        try:
            quotes = self._quotes([code], as_of)
            if code in quotes:
                self._save_json(self.cache / "quotes" / f"{code}.json", quotes[code])
                fresh_quote = quotes[code]
        except Exception as exc:
            failures.append(f"quote: {exc}")
        pe = _number(fresh_quote.get("pe_ttm"))
        quoted = fresh_quote.get("as_of")
        if (
            company.get("template") == "ordinary"
            and company.get("classification_verified") is True
            and pe is not None
            and (pe <= 0 or pe >= 25)
            and quoted
            and 0 <= (as_of - date.fromisoformat(quoted)).days <= 7
        ):
            self._save_json(
                self.cache / "receipts" / f"{code}.json",
                {
                    "status": "partial",
                    "acquisition_phase": "quote_screen_rejected",
                    "company": company["id"],
                    "as_of": as_of.isoformat(),
                    "quote_as_of": quoted,
                    "pe_ttm": pe,
                    "source_url": fresh_quote.get("url"),
                    "financial_refresh": "not_requested",
                    "reason": "ordinary PE TTM must be strictly positive and below 25",
                },
            )
            logger.info(
                "FQ quote screen rejected issuer=%s pe_ttm=%s", company["id"], pe
            )
            return
        if company.get("template") == "reit":
            if failures:
                raise RuntimeError("; ".join(failures))
            return
        state_path = self.cache / "refresh_state" / f"{code}.json"
        prior = self._read_json(state_path) if state_path.exists() else {}
        signature = self._refresh_signature(company, as_of)
        attempts = (
            int(prior.get("attempts", 0)) if prior.get("signature") == signature else 0
        )
        if attempts >= 2 and prior.get("status") == "failed":
            raise SourceRetryBlocked(
                "unchanged financial source failure exhausted one retry; "
                "waiting for changed PIT evidence, parser/source revision, or disclosure quarter"
            )
        try:
            self._refresh_financials(company, as_of)
        except Exception as exc:
            updated_signature = self._refresh_signature(company, as_of)
            self._save_json(
                state_path,
                {
                    "signature": updated_signature,
                    "attempts": attempts + 1 if updated_signature == signature else 1,
                    "status": "failed",
                    "error": str(exc),
                    "as_of": as_of.isoformat(),
                },
            )
            raise
        self._save_json(
            state_path,
            {
                "signature": self._refresh_signature(company, as_of),
                "attempts": 0,
                "status": "success",
                "as_of": as_of.isoformat(),
            },
        )
        if failures:
            raise RuntimeError("; ".join(failures))

    def _refresh_signature(self, company: dict, as_of: date) -> str:
        evidence = []
        for root in self.pit_roots:
            path = root / "fundamentals" / f"{company['code']}.statements.json"
            if path.is_file():
                evidence.append(
                    [str(path), hashlib.sha256(path.read_bytes()).hexdigest()]
                )
        documents = self.cache / "verified_documents" / f"{company['code']}.json"
        if documents.is_file():
            evidence.append(
                [str(documents), hashlib.sha256(documents.read_bytes()).hexdigest()]
            )
        return _digest(
            json.dumps(
                {
                    "source_contract": "fq-source-4",
                    "document_parser": DOCUMENT_PARSER_VERSION,
                    "max_pdf_pages": self.max_pdf_pages,
                    "revision": self.options.get("source_revision", "1"),
                    "company": company["id"],
                    "template": company.get("template"),
                    "disclosure_quarter": [as_of.year, (as_of.month - 1) // 3],
                    "evidence": evidence,
                },
                sort_keys=True,
            )
        )

    def _refresh_financials(self, company: dict, as_of: date) -> None:
        code = company["code"]
        logger.info("FQ statements begin issuer=%s as_of=%s", company["id"], as_of)
        failures = []
        from ..data.point_in_time_backfill import PointInTimeBackfillService

        config = copy.deepcopy(self.config)
        settings = config.setdefault("point_in_time_data", {})
        settings["history_years"] = self._original_years(company)
        settings["output_dir"] = str(self.pit_roots[0])
        settings["official_statement_crawlers"] = False
        # The existing provider owns Baostock sessions, shared rate limits, and
        # circuit state. Never create a second independent acquisition client.
        try:
            service = PointInTimeBackfillService(config)
            receipt = service._backfill_one(
                code,
                as_of - timedelta(days=int(settings["history_years"] * 365.25)),
                as_of,
                include_market_history=False,
            )
            self._save_json(self.cache / "receipts" / f"{code}.json", receipt)
            statements = receipt.get("statements", {})
            if statements.get("status") not in {"success", "cached"}:
                failures.append(f"statement acquisition incomplete: {statements}")
        except Exception as exc:
            failures.append(f"financials: {exc}")
        logger.info(
            "FQ statements finished issuer=%s failures=%s", company["id"], len(failures)
        )
        logger.info("FQ originals begin issuer=%s", company["id"])
        failures.extend(self._refresh_reports(company, as_of, config))
        logger.info(
            "FQ originals finished issuer=%s failures=%s", company["id"], len(failures)
        )
        if failures:
            raise RuntimeError("; ".join(failures))

    def _original_years(self, company: dict) -> int:
        if company.get("template") == "cyclical":
            return 11
        # Seven growth intervals and average-capital calculations need an
        # opening annual observation. Narrative review still uses latest three.
        return 8

    def _reserve_document_slot(self, code: str, as_of: date, limit: int) -> bool:
        campaign = self.options.get("acceptance_document_budget")
        if campaign:
            if (
                not isinstance(campaign, dict)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(campaign.get("id", "")))
                or type(campaign.get("limit")) is not int
                or not 1 <= campaign["limit"] <= 12
            ):
                raise ValueError("invalid acceptance document budget")
            expiry = datetime.fromisoformat(campaign["expires_at"])
            if expiry.tzinfo is None or datetime.now(timezone.utc) >= expiry:
                return False
            campaign_path = (
                self.cache
                / "document_budget"
                / "campaigns"
                / campaign["id"]
                / f"{code}.json"
            )
            saved = self._read_json(campaign_path) if campaign_path.exists() else {}
            used = int(saved.get("used", 0))
            if used >= campaign["limit"]:
                return False
            # Reserve first; a crash may lose capacity but cannot refund it.
            self._save_json(campaign_path, {"used": used + 1})
        path = self.cache / "document_budget" / f"{code}.json"
        saved = self._read_json(path) if path.exists() else {}
        used = int(saved.get("used", 0)) if saved.get("day") == as_of.isoformat() else 0
        if not campaign and used >= limit:
            return False
        # Charge before the worker call: failures and process restarts must not
        # turn a four-document daily bound into an unbounded download loop.
        self._save_json(path, {"day": as_of.isoformat(), "used": used + 1})
        return True

    def _refresh_reports(self, company: dict, as_of: date, config: dict) -> list[str]:
        # Archive originals independently of parsed-value success. No vision LLM
        # fallback runs here: scanned pages stay visibly missing pending extraction.
        from ..data.cninfo_reports import CninfoReportService
        from ..data.dataset_catalog import DatasetCatalog
        from ..data.dataset_documents import DatasetDocumentWorker
        from ..instruments.point_in_time import CninfoAnnualReportProvider

        catalog = DatasetCatalog(self.project_root)
        provider = CninfoAnnualReportProvider(config)
        code = company["code"]
        failures = []
        try:
            original_pages = self._documents(company, as_of)
        except Exception as exc:
            original_pages = []
            failures.append(f"cached originals: {exc}")
        complete = [
            p
            for p in original_pages
            if p.get("document_status") == "complete_text"
            and p.get("kind") in {"annual", "interim"}
        ]
        complete_ids = {p["id"].rsplit(":p", 1)[0] for p in complete}
        complete_periods = {(p["kind"], int(p["period_end"][:4])) for p in complete}
        annual_year = max(
            (year for kind, year in complete_periods if kind == "annual"), default=None
        )
        annual_count = self._original_years(company)
        wanted = [
            ("annual", None),
            ("annual", None),
            ("annual", None),
            ("half_year", None),
        ] + [("annual", None)] * (annual_count - 3)
        document_limit = max(
            0, min(4, int(self.options.get("max_documents_per_issuer", 4)))
        )
        if self.options.get("acceptance_document_budget"):
            document_limit = 12
        downloaded = []
        for index in range(len(wanted)):
            report_type, year = wanted[index]
            try:
                if report_type == "annual" and index != 0:
                    if annual_year is None:
                        raise ValueError("latest annual report year unresolved")
                    year = annual_year - (index if index < 3 else index - 1)
                    if ("annual", year) in complete_periods:
                        continue
                report = provider.discover_report(code, report_type, year)
                if date.fromisoformat(report["publication_date"]) > as_of:
                    raise ValueError("report disclosed after evaluation date")
                if index == 0:
                    annual_year = int(report["report_year"])
                document_id = CninfoReportService._document_id(report)
                if document_id in complete_ids:
                    continue
                if not self._reserve_document_slot(code, as_of, document_limit):
                    # Remaining years stay pending; budget exhaustion is not a
                    # persistent source failure and must not lock the next day.
                    break
                catalog.register_document_source(
                    document_id,
                    report["source_url"],
                    report["title"],
                    report["publication_date"],
                    code,
                )
                result = DatasetDocumentWorker(
                    catalog,
                    max_document_bytes=50 * 1024 * 1024,
                    max_pdf_pages=self.max_pdf_pages,
                ).run(max_documents=1, max_seconds=180, job_ids=[document_id])
                self._save_json(
                    self.cache / "receipts" / f"{code}-{report_type}-{year}.json",
                    result,
                )
                job = catalog.document_jobs(job_ids=[document_id], limit=1)["items"]
                if not job or not any(
                    a.get("kind") == "pdf" for a in job[0].get("artifact_paths", [])
                ):
                    raise ValueError("report archive has no original artifact")
                downloaded.append(document_id)
            except Exception as exc:
                failures.append(f"{report_type}/{year}: {exc}")
        if downloaded:
            try:
                checked = self._documents(company, as_of)
                checked_ids = {
                    p["id"].rsplit(":p", 1)[0]
                    for p in checked
                    if p.get("document_status") == "complete_text"
                    and p.get("kind") in {"annual", "interim"}
                }
                verified_hashes = sorted(
                    {
                        (p["id"].rsplit(":p", 1)[0], p.get("document_sha256", ""))
                        for p in checked
                        if p.get("document_status") == "complete_text"
                        and p.get("kind") in {"annual", "interim"}
                    }
                )
                self._save_json(
                    self.cache / "verified_documents" / f"{code}.json",
                    {"documents": verified_hashes},
                )
                failures.extend(
                    f"original text incomplete: {document_id}"
                    for document_id in downloaded
                    if document_id not in checked_ids
                )
            except Exception as exc:
                failures.append(f"downloaded originals: {exc}")
        return failures

    def _documents(self, company: dict, as_of: date) -> list[dict]:
        catalog_path = self.project_root / "data/dataset_catalog/catalog.sqlite3"
        if not catalog_path.exists():
            return []
        # The regular catalog constructor may migrate schema. Collection without
        # refresh must remain read-only, so use its existing indexed schema via a
        # read-only connection and never schedule document work here.
        with sqlite3.connect(catalog_path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT DISTINCT d.* FROM documents d JOIN document_sources s "
                "ON s.document_id=d.document_id WHERE s.code=? "
                "AND (d.title LIKE '%年度报告%' OR d.title LIKE '%中期报告%') "
                "ORDER BY d.publication_date DESC LIMIT 200",
                (company["code"],),
            ).fetchall()
            jobs = []
            for row in rows:
                job = dict(row)
                job["artifact_paths"] = [
                    dict(a)
                    for a in db.execute(
                        "SELECT a.kind,f.relative_path,f.sha256 FROM document_artifacts a "
                        "JOIN files f ON f.file_id=a.file_id "
                        "WHERE a.document_id=? AND f.present=1",
                        (row["document_id"],),
                    ).fetchall()
                ]
                jobs.append(job)
        jobs.sort(key=lambda item: str(item.get("publication_date", "")), reverse=True)
        sources = []
        report_counts = {"annual": 0, "interim": 0}
        annual_count = self._original_years(company)
        periods = set()
        for job in jobs:
            try:
                published = str(job.get("publication_date", ""))[:10]
                if not published or date.fromisoformat(published) > as_of:
                    continue
                title = job.get("title", "")
                identity = self._report_identity(title)
                if identity is None:
                    continue
                kind, year = identity
                if year < as_of.year - annual_count - 1 or year > as_of.year:
                    continue
                if (kind, year) in periods:
                    continue
                if report_counts[kind] >= (annual_count if kind == "annual" else 1):
                    continue
                artifact = next(
                    (a for a in job.get("artifact_paths", []) if a["kind"] == "pdf"),
                    None,
                )
                if not artifact:
                    raise ValueError("original PDF artifact missing")
                path = (self.project_root / artifact["relative_path"]).resolve()
                if not path.is_relative_to(self.project_root):
                    raise ValueError("document artifact escapes project root")
                if (
                    not artifact.get("sha256")
                    or hashlib.sha256(path.read_bytes()).hexdigest()
                    != artifact["sha256"]
                ):
                    raise ValueError(
                        "original PDF hash does not match catalog artifact"
                    )
                pages = self._extract_pdf_pages(path, artifact["sha256"])
                if pages:
                    document_sources, incomplete_pages = [], []
                    for page_number, (text, incomplete) in enumerate(pages, 1):
                        if incomplete:
                            incomplete_pages.append(page_number)
                        if not text.strip():
                            continue
                        document_sources.append(
                            {
                                "id": f"{job['document_id']}:p{page_number}",
                                "issuer_id": company["id"],
                                "period_end": f"{year}-06-30"
                                if kind == "interim"
                                else f"{year}-12-31",
                                "published_at": published,
                                "kind": kind,
                                "text": text,
                                "url": job.get("source_url", ""),
                                "page": page_number,
                                "hash": _digest(text),
                                "origin_id": job.get("source_url")
                                or job["document_id"],
                                "document_sha256": artifact["sha256"],
                                "document_path": path.relative_to(
                                    self.project_root
                                ).as_posix(),
                                "document_title": title,
                                "document_pages": len(pages),
                                "parser_version": DOCUMENT_PARSER_VERSION,
                                "page_status": "incomplete_text"
                                if page_number in incomplete_pages
                                else "complete_text",
                            }
                        )
                    if incomplete_pages or not document_sources:
                        for source in document_sources:
                            source.update(
                                kind="statement",
                                document_kind=kind,
                                document_status="incomplete_text",
                                incomplete_pages=incomplete_pages,
                            )
                        self._document_errors.append(
                            _error(
                                "original_text_incomplete",
                                company["id"],
                                job["document_id"],
                                f"report has unextracted/image pages {incomplete_pages}; cannot establish full original coverage",
                                stage="evidence",
                                retry="verified_ocr_or_original_changes",
                            )
                        )
                    else:
                        for source in document_sources:
                            source["document_status"] = "complete_text"
                    sources.extend(document_sources)
                    report_counts[kind] += 1
                    periods.add((kind, year))
            except Exception as exc:
                self._document_errors.append(
                    _error(
                        "original_document_parse_failed",
                        company["id"],
                        str(job.get("document_id", "unknown")),
                        f"{type(exc).__name__}: {exc}",
                        stage="evidence",
                        retry="original_or_parser_changes",
                    )
                )
                logger.warning(
                    "FQ PDF rejected issuer=%s document=%s error=%s",
                    company["id"],
                    job.get("document_id"),
                    type(exc).__name__,
                )
        return sources

    def _extract_pdf_pages(self, path: Path, digest: str) -> list[tuple[str, bool]]:
        import pdfplumber

        key = (
            digest,
            f"{DOCUMENT_PARSER_VERSION}:{pdfplumber.__version__}:{self.max_pdf_pages}",
        )
        if key not in self._pdf_page_cache:
            logger.info("FQ PDF parse begin file=%s", path.name)
            with pdfplumber.open(path) as pdf:
                if not pdf.pages or len(pdf.pages) > self.max_pdf_pages:
                    raise ValueError(
                        f"report must contain 1-{self.max_pdf_pages} extractable PDF pages"
                    )
                extracted = []
                for page in pdf.pages:
                    try:
                        text = page.extract_text() or ""
                        extracted.append((text, self._page_text_incomplete(page, text)))
                    finally:
                        flush = getattr(page, "flush_cache", None)
                        if callable(flush):
                            flush()
            # Bounded, in-process only. Every caller still verifies original SHA256;
            # a new PDF/parser cannot reuse previous extraction or completeness.
            if len(self._pdf_page_cache) >= 24:
                self._pdf_page_cache.pop(next(iter(self._pdf_page_cache)))
            self._pdf_page_cache[key] = extracted
            logger.info(
                "FQ PDF parse finished file=%s pages=%s", path.name, len(extracted)
            )
        return self._pdf_page_cache[key]

    @staticmethod
    def _page_text_incomplete(page, text: str) -> bool:
        substantive_images = [
            image
            for image in getattr(page, "images", [])
            if not ResearchSources._verified_publication_footer(page, image)
        ]
        return bool(
            (not text.strip() and getattr(page, "objects", {}))
            or (substantive_images and len(text.strip()) < 100)
        )

    @staticmethod
    def _verified_publication_footer(page, image: dict) -> bool:
        """Recognize one known decorative asset, never arbitrary small images."""
        try:
            width, height = float(page.width), float(page.height)
            if (
                image.get("tag") != "Artifact"
                or width <= 0
                or height <= 0
                or not 0 < float(image["width"]) <= width * 0.12
                or not 0 < float(image["height"]) <= height * 0.04
                or float(image["x0"]) < width * 0.85
                or float(image["top"]) < height * 0.95
                or float(image["bottom"]) > height
            ):
                return False
            return (
                hashlib.sha256(image["stream"].get_data()).hexdigest()
                == _CNINFO_FOOTER_IMAGE_SHA256
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _report_identity(title: str) -> tuple[str, int] | None:
        # A profit-allocation notice and a report summary are not full reports.
        if "摘要" in title:
            return None
        match = re.search(
            r"(20\d{2})年?(半年度|年度|中期)报告(?:全文)?(?:[（(][^）)]*[）)])?\s*$",
            title,
        )
        if not match:
            return None
        return ("annual" if match[2] == "年度" else "interim", int(match[1]))
