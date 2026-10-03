"""Dated cash distributions and supported SSE fund share splits.

The exchange index supplies the disclosure date; each linked implementation
notice supplies the fund code, cash unit, ex-date, record date and payment date.
Day-end splits additionally require a prior notice, explicit next-session date,
share-rounding terms and independent raw/qfq market-basis confirmation.
An incomplete index or an ambiguous document is an error, never an empty-success
fallback. These actions do not by themselves certify market-history coverage.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import urljoin, urlparse

import requests

if TYPE_CHECKING:
    from .market_history import CorporateAction


SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_ORIGIN = "https://www.sse.com.cn"
_DATE = r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?"
_MAX_PDF_BYTES = 10 * 1024 * 1024


class FundActionEvidenceError(ValueError):
    """Official evidence is unavailable, incomplete or ambiguous."""


@dataclass(frozen=True)
class FundSplitEvidence:
    code: str
    published_at: date
    record_date: date
    first_post_split_session: date
    share_multiplier: float
    share_rounding: str
    source_url: str


def parse_split_announcement(
    text: str, *, code: str, disclosed_at: date, source_url: str
) -> FundSplitEvidence:
    """Read a stated end-of-day split; never infer its next session by calendar."""
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    codes = set(re.findall(r"基金(?:主)?代码:?(\d{6})(?!\d)", compact))
    if codes != {str(code)}:
        raise FundActionEvidenceError("Split notice fund code is missing or ambiguous")
    records = re.findall(r"份额拆分日(?::|\()?" + _DATE, compact)
    sessions = re.findall(r"份额拆分日(?:的下一|后的第一个)工作日\(" + _DATE, compact)
    try:
        record_dates = {date(*(int(part) for part in match)) for match in records}
        session_dates = {date(*(int(part) for part in match)) for match in sessions}
    except ValueError as exc:
        raise FundActionEvidenceError("Invalid split date") from exc
    if len(record_dates) != 1 or len(session_dates) != 1:
        raise FundActionEvidenceError("Split record/session dates are not explicit")
    record_date = next(iter(record_dates))
    first_session = next(iter(session_dates))
    if record_date >= first_session:
        raise FundActionEvidenceError("Split session does not follow the record date")
    if not re.search(r"份额拆分日(?:\(" + _DATE + r"\))?日终", compact):
        raise FundActionEvidenceError("Split does not explicitly occur at day-end")
    ratios = set(re.findall(r"份额拆分比例为(\d+(?:\.\d+)?)", compact))
    if len(ratios) != 1:
        raise FundActionEvidenceError("Split ratio is missing or ambiguous")
    multiplier = float(next(iter(ratios)))
    if not math.isfinite(multiplier) or multiplier <= 0 or multiplier == 1:
        raise FundActionEvidenceError("Invalid split multiplier")
    if "上进位" not in compact or "小数点后基金份额均进位为1份" not in compact:
        raise FundActionEvidenceError("Split share rounding is unsupported or unstated")
    return FundSplitEvidence(
        code=str(code),
        published_at=disclosed_at,
        record_date=record_date,
        first_post_split_session=first_session,
        share_multiplier=multiplier,
        share_rounding="ceil",
        source_url=source_url,
    )


def verify_split_market_basis(evidence: FundSplitEvidence, prices) -> None:
    """Confirm the explicit next session against unmodified source raw/qfq bars."""
    import pandas as pd

    required = {"date", "raw_close", "qfq_close"}
    if prices is None or not required.issubset(prices.columns):
        raise FundActionEvidenceError("Split needs independent raw/qfq price evidence")
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    if frame["date"].isna().any() or frame["date"].duplicated().any():
        raise FundActionEvidenceError("Split price dates are missing or duplicated")
    frame = frame.sort_values("date").reset_index(drop=True)
    selected = frame.index[frame["date"] == evidence.first_post_split_session]
    if len(selected) != 1 or selected[0] == 0:
        raise FundActionEvidenceError("Split session is absent from market evidence")
    index = int(selected[0])
    before = frame.iloc[index - 1]
    after = frame.iloc[index]
    if before["date"] != evidence.record_date:
        raise FundActionEvidenceError(
            "Split record date is not the prior source session"
        )
    values = [float(r[c]) for r in (before, after) for c in ("raw_close", "qfq_close")]
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise FundActionEvidenceError("Invalid split market prices")
    raw_before, adjusted_before, raw_after, adjusted_after = values
    observed = (adjusted_after / raw_after) / (adjusted_before / raw_before)
    if not math.isclose(observed, evidence.share_multiplier, rel_tol=1e-3):
        raise FundActionEvidenceError(
            "Source raw/qfq basis does not confirm the disclosed split session"
        )


def _label_date(text: str, label: str, *, required: bool = True) -> date | None:
    matches = re.findall(label + r":?" + _DATE, text)
    try:
        values = {date(*(int(part) for part in match)) for match in matches}
    except ValueError as exc:
        raise FundActionEvidenceError(f"Invalid {label} date") from exc
    if len(values) > 1 or (required and not values):
        raise FundActionEvidenceError(f"Missing or ambiguous {label}")
    return next(iter(values)) if values else None


def _non_cash_effective_date(text: str) -> date | None:
    label = r"(?:折算|拆分|合并)(?:基准|生效|实施)?日"
    matches = re.findall(label + r"(?::|为|\()?" + _DATE, text)
    matches += re.findall(
        r"(?:确定|以)" + _DATE + r"(?:为|作为)[^。；;]{0,100}?" + label,
        text,
    )
    try:
        values = {date(*(int(part) for part in match)) for match in matches}
    except ValueError as exc:
        raise FundActionEvidenceError("Invalid non-cash effective date") from exc
    if len(values) > 1:
        raise FundActionEvidenceError("Ambiguous non-cash effective date")
    return next(iter(values)) if values else None


def parse_distribution_announcement(
    text: str,
    *,
    code: str,
    disclosed_at: date,
    source_url: str,
) -> CorporateAction:
    """Parse one fund's implementation notice, requiring explicit cash units.

    The later of the exchange disclosure and the printed announcement date is
    used when they differ. An ex-date is never substituted for publication.
    Multi-class tables are rejected rather than assigning another class's cash.
    """
    from .market_history import CorporateAction

    code = str(code)
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    fund_codes = set(re.findall(r"基金(?:主)?代码[:：]?(\d{6})(?!\d)", compact))
    if fund_codes != {code}:
        raise FundActionEvidenceError(f"Fund code mismatch or ambiguity: {code}")
    if "分红公告" not in compact and "收益分配公告" not in compact:
        raise FundActionEvidenceError("Document is not a distribution notice")
    cash_matches = re.findall(
        r"(?:本次)?分红方案\((?:单位:)?(?:人民币)?元/"
        r"(\d+(?:\.\d+)?)份(?:基金份额)?\)(\d+(?:\.\d+)?)",
        compact,
    )
    cash_values: set[Decimal] = set()
    for denominator, amount in cash_matches:
        divisor = Decimal(denominator)
        if divisor <= 0:
            raise FundActionEvidenceError("Invalid distribution unit")
        cash_values.add(Decimal(amount) / divisor)
    if len(cash_matches) != 1 or len(cash_values) != 1:
        raise FundActionEvidenceError("Missing or ambiguous distribution amount/unit")
    cash = float(next(iter(cash_values)))
    if not math.isfinite(cash) or cash <= 0:
        raise FundActionEvidenceError("Distribution cash must be positive")
    ex_date = _label_date(compact, "除息日")
    record_date = _label_date(compact, "权益登记日")
    payable_date = _label_date(compact, "现金红利发放日")
    printed = _label_date(compact, "公告送出日期", required=False)
    published = max(disclosed_at, printed) if printed else disclosed_at
    if not (published <= record_date <= ex_date <= payable_date):
        raise FundActionEvidenceError("Distribution dates are not causal")
    diagnostics = [f"sse_disclosed_at={disclosed_at.isoformat()}"]
    if printed:
        diagnostics.append(f"document_announced_at={printed.isoformat()}")
    diagnostics.append("cash_unit=explicit_per_fund_share")
    return CorporateAction(
        code=code,
        action_type="cash_dividend",
        ex_date=ex_date,
        published_at=published,
        record_date=record_date,
        payable_date=payable_date,
        cash_per_share=cash,
        source="sse_fund_distribution",
        source_url=source_url,
        currency="CNY",
        diagnostics=diagnostics,
    )


class SseFundCorporateActionProvider:
    """Fetch official SSE implementation notices without touching price stores."""

    def __init__(
        self,
        *,
        http_get: Callable = requests.get,
        evidence_dir: str | Path | None = None,
        timeout: float = 20,
        page_size: int = 100,
        max_pages: int = 20,
    ) -> None:
        if not 1 <= page_size <= 100 or max_pages < 1:
            raise ValueError("Invalid announcement pagination limits")
        self._http_get = http_get
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.timeout = timeout
        self.page_size = page_size
        self.max_pages = max_pages
        self.headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": SSE_ORIGIN + "/",
        }

    def _archive(self, content: bytes, suffix: str) -> str:
        digest = hashlib.sha256(content).hexdigest()
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            target = self.evidence_dir / f"{digest}.{suffix}"
            if not target.exists():
                target.write_bytes(content)
        return digest

    def _index(self, code: str, end: date) -> list[dict]:
        """Read every matching page, including notices before the price start."""
        rows: list[dict] = []
        seen_urls: set[str] = set()
        total: int | None = None
        for page in range(1, self.max_pages + 1):
            params = {
                "isPagination": "true",
                "pageHelp.pageSize": self.page_size,
                "pageHelp.pageNo": page,
                "pageHelp.beginPage": page,
                "pageHelp.endPage": page,
                "pageHelp.cacheSize": 1,
                "type": "inParams",
                "sqlId": "COMMON_PL_JJXX_JJGG_NEW_L",
                "TITLE": "",
                "SECURITY_CODE": code,
                "BULLETIN_TYPE": "",
                "START_DATE": "",
                "END_DATE": end.isoformat(),
                "DATE_DESC": 1,
            }
            response = self._http_get(
                SSE_QUERY_URL,
                params=params,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            self._archive(
                json.dumps(
                    {"url": SSE_QUERY_URL, "params": params, "response": data},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8"),
                "index.json",
            )
            if not isinstance(data, dict) or any(
                data.get(key) for key in ("actionErrors", "fieldErrors")
            ):
                raise FundActionEvidenceError("SSE index returned an error")
            pagination = data.get("pageHelp") or {}
            try:
                reported_total = int(pagination["total"])
                returned_page = int(pagination["pageNo"])
            except (TypeError, ValueError, KeyError) as exc:
                raise FundActionEvidenceError(
                    "SSE pagination metadata missing"
                ) from exc
            if returned_page != page or reported_total < 0:
                raise FundActionEvidenceError("SSE pagination metadata invalid")
            if total is None:
                total = reported_total
            elif total != reported_total:
                raise FundActionEvidenceError("SSE index changed during pagination")
            batch = data.get("result")
            if not isinstance(batch, list) or len(rows) + len(batch) > total:
                raise FundActionEvidenceError("SSE index row count is invalid")
            for row in batch:
                if (
                    not isinstance(row, dict)
                    or str(row.get("SECURITY_CODE", "")) != code
                    or not row.get("URL")
                ):
                    raise FundActionEvidenceError("SSE index returned a different fund")
                if row["URL"] in seen_urls:
                    raise FundActionEvidenceError("SSE repeated a page or document")
                seen_urls.add(row["URL"])
                rows.append(row)
            if len(rows) == total:
                return rows
            if not batch:
                raise FundActionEvidenceError(
                    "SSE index ended before the declared total"
                )
        raise FundActionEvidenceError("SSE index exceeds the pagination limit")

    @staticmethod
    def _document_url(raw_url: str) -> str:
        url = urljoin(SSE_ORIGIN + "/", raw_url)
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.sse.com.cn", "static.sse.com.cn"}
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or not parsed.path.lower().endswith(".pdf")
        ):
            raise FundActionEvidenceError("Distribution document URL is not official")
        return url

    @staticmethod
    def _pdf_text(content: bytes) -> str:
        import pdfplumber

        if not content.startswith(b"%PDF") or len(content) > _MAX_PDF_BYTES:
            raise FundActionEvidenceError("Invalid or oversized distribution PDF")
        with pdfplumber.open(io.BytesIO(content)) as document:
            if not 1 <= len(document.pages) <= 30:
                raise FundActionEvidenceError("Unexpected distribution PDF page count")
            return "\n".join(page.extract_text() or "" for page in document.pages)

    def _document(self, url: str) -> tuple[str, str]:
        """Reuse a hash-verified PDF only for its exact official archive URL."""
        cache_path = None
        if self.evidence_dir is not None:
            url_digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
            cache_path = self.evidence_dir / f"{url_digest}.url.json"
            if cache_path.exists():
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                    digest = str(cached["sha256"])
                    if cached["url"] != url or not re.fullmatch(
                        r"[0-9a-f]{64}", digest
                    ):
                        raise ValueError("Invalid cache identity")
                    content = (self.evidence_dir / f"{digest}.pdf").read_bytes()
                    if hashlib.sha256(content).hexdigest() != digest:
                        raise ValueError("Cached PDF hash mismatch")
                    return self._pdf_text(content), digest
                except (OSError, ValueError, KeyError) as exc:
                    raise FundActionEvidenceError("Corrupt official PDF cache") from exc
        response = self._http_get(url, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        final_url = getattr(response, "url", url) or url
        self._document_url(final_url)
        content = response.content
        text = self._pdf_text(content)
        digest = self._archive(content, "pdf")
        self._archive(text.encode("utf-8"), "txt")
        if cache_path is not None:
            cache_path.write_text(
                json.dumps({"url": url, "sha256": digest}, sort_keys=True),
                encoding="utf-8",
            )
        return text, digest

    def fetch_actions(
        self, code: str, start: date, end: date, *, market_prices=None
    ) -> list[CorporateAction]:
        """Return cash and supported split events within the ex-session interval.

        ``market_prices`` must be the unmodified source raw/qfq history when a
        split is present. The price reconstruction being built from these actions
        cannot independently verify them. Unknown conversions/mergers fail closed.
        """
        code = str(code)
        if not re.fullmatch(r"5\d{5}", code):
            raise ValueError("SSE fund code must contain six digits beginning with 5")
        if start > end:
            raise ValueError("Action interval is reversed")
        selected: dict[date, CorporateAction] = {}
        evidence: list[dict] = []
        non_cash_notices: list[dict] = []
        split_evidence: dict[date, list[FundSplitEvidence]] = {}
        index_rows = self._index(code, end)
        for row in index_rows:
            title = re.sub(r"\s+", "", str(row.get("TITLE", "")))
            non_cash = any(word in title for word in ("拆分", "折算", "合并"))
            if not non_cash and "分红公告" not in title and "收益分配公告" not in title:
                continue
            try:
                disclosed_at = date.fromisoformat(str(row["SSEDATE"])[:10])
            except (KeyError, ValueError) as exc:
                raise FundActionEvidenceError(
                    "Distribution disclosure date missing"
                ) from exc
            if disclosed_at > end:
                raise FundActionEvidenceError("SSE returned a future disclosure")
            url = self._document_url(str(row["URL"]))
            text, digest = self._document(url)
            compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
            if non_cash:
                effective = _non_cash_effective_date(compact)
                resumed = None
                if effective is None and all(
                    word in title for word in ("暂停", "申购", "赎回")
                ):
                    resumes = re.findall(
                        r"(?:并于|自)" + _DATE + r"起恢复(?:办理)?申购(?:和|、)?赎回",
                        compact,
                    )
                    resume_dates = {
                        date(*(int(part) for part in match)) for match in resumes
                    }
                    if len(resume_dates) == 1:
                        resumed = next(iter(resume_dates))
                before_window = effective or resumed
                if before_window and before_window < start and disclosed_at < start:
                    non_cash_notices.append(
                        {
                            "url": url,
                            "sha256": digest,
                            "disclosed_at": disclosed_at.isoformat(),
                            "effective_date": effective.isoformat()
                            if effective
                            else None,
                            "business_resumption_date": resumed.isoformat()
                            if resumed
                            else None,
                            "status": "explicitly_before_requested_window",
                        }
                    )
                    continue
                if "拆分" not in title:
                    raise FundActionEvidenceError(
                        f"Unsupported non-cash fund action: {code} {title} {url}"
                    )
                try:
                    split = parse_split_announcement(
                        text, code=code, disclosed_at=disclosed_at, source_url=url
                    )
                except FundActionEvidenceError as exc:
                    raise FundActionEvidenceError(
                        f"Unsupported non-cash fund action: {code} {url}: {exc}"
                    ) from exc
                split_evidence.setdefault(split.record_date, []).append(split)
                non_cash_notices.append(
                    {
                        "url": url,
                        "sha256": digest,
                        "disclosed_at": disclosed_at.isoformat(),
                        "record_date": split.record_date.isoformat(),
                        "effective_session": split.first_post_split_session.isoformat(),
                        "share_multiplier": split.share_multiplier,
                        "share_rounding": split.share_rounding,
                        "status": "requires_prior_notice_and_market_basis_confirmation",
                    }
                )
                continue
            document_ex_date = _label_date(compact, "除息日")
            if not start <= document_ex_date <= end:
                continue
            action = parse_distribution_announcement(
                text,
                code=code,
                disclosed_at=disclosed_at,
                source_url=url,
            )
            previous = selected.get(action.ex_date)
            if previous:
                comparable = ("cash_per_share", "record_date", "payable_date")
                if any(
                    getattr(previous, field) != getattr(action, field)
                    for field in comparable
                ):
                    raise FundActionEvidenceError("Conflicting distribution notices")
                if previous.published_at <= action.published_at:
                    continue
            selected[action.ex_date] = action
            evidence.append({"url": url, "sha256": digest, "exchange_row": row})
        if split_evidence:
            from .market_history import CorporateAction

            fields = getattr(
                CorporateAction,
                "model_fields",
                getattr(CorporateAction, "__fields__", {}),
            )
            if "share_rounding" not in fields:
                raise FundActionEvidenceError(
                    "Execution model lacks split share rounding"
                )
            for notices in split_evidence.values():
                facts = {
                    (n.first_post_split_session, n.share_multiplier, n.share_rounding)
                    for n in notices
                }
                if len(facts) != 1:
                    raise FundActionEvidenceError("Split notices conflict")
                prior = [n for n in notices if n.published_at <= n.record_date]
                if not prior:
                    raise FundActionEvidenceError("Split has no causal prior notice")
                split = min(prior, key=lambda item: item.published_at)
                if not start <= split.first_post_split_session <= end:
                    continue
                verify_split_market_basis(split, market_prices)
                if split.first_post_split_session in selected:
                    raise FundActionEvidenceError(
                        "Same-day split/cash needs composition"
                    )
                selected[split.first_post_split_session] = CorporateAction(
                    code=code,
                    action_type="split",
                    ex_date=split.first_post_split_session,
                    published_at=split.published_at,
                    record_date=split.record_date,
                    share_multiplier=split.share_multiplier,
                    share_rounding=split.share_rounding,
                    source="sse_fund_split",
                    source_url=split.source_url,
                    currency="CNY",
                    diagnostics=[
                        "split_registered_at_record_day_end",
                        "effective_session_explicit_in_prior_announcement",
                        "raw_qfq_basis_confirms_effective_session",
                        "fractional_split_shares_rounded_up_per_holder",
                    ],
                )
                for notice in non_cash_notices:
                    if notice.get("record_date") == split.record_date.isoformat():
                        notice["status"] = "prior_notice_and_market_basis_confirmed"
        self._archive(
            json.dumps(
                {
                    "code": code,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "exchange_index_rows": len(index_rows),
                    "exchange_index_complete": True,
                    "non_cash_notices": non_cash_notices,
                    "coverage_scope": "recognized cash and non-cash notice titles; cross-check market adjustment events",
                    "documents": evidence,
                    "actions": [
                        json.loads(selected[key].json()) for key in sorted(selected)
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8"),
            "manifest.json",
        )
        return [selected[key] for key in sorted(selected)]
