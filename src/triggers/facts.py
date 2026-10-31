"""Minimal, provenance-bearing inputs for the pure trigger evaluator.

Providers are injectable and do no report generation, optimization or LLM work.
Missing observations remain unknown; fetch time never replaces a source clock.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from zoneinfo import ZoneInfo

from .catalog import (
    HISTORY_FIELDS,
    VALUATION_FIELDS,
    build_report_field_catalog,
    data_mapping,
    iter_safe_leaves,
    safe_path,
)

LOCAL_ZONE = ZoneInfo("Asia/Shanghai")
Q3_KIND = "announcement.q3_report"


@dataclass(frozen=True)
class FactValue:
    value: Any = None
    status: str = "unknown"
    as_of: datetime | None = None
    observed_at: datetime | None = None
    source: str | None = None
    error: str | None = None
    security: str | None = None
    path: str = ""


@dataclass(frozen=True)
class AnnouncementEvent:
    id: str
    kind: str
    symbol: str
    occurred_at: datetime
    published_at: datetime
    report_period: str
    source: str
    title: str
    url: str
    publication_date: date
    observed_at: datetime
    source_sha256: str | None = None
    source_urls: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    source_documents: tuple[dict, ...] = ()


@dataclass
class FactSnapshot:
    as_of: datetime
    observed_at: datetime
    facts: dict[str, FactValue] = field(default_factory=dict)
    events: list[AnnouncementEvent] = field(default_factory=list)
    wildcard_symbols: tuple[str, ...] | None = None

    def resolve(self, path: str) -> FactValue:
        if not safe_path(path):
            return FactValue(error="unsafe fact path", path=str(path))
        if path in self.facts:
            return self.facts[path]
        if "*" in path:
            pattern = path.split(".")
            matches = [
                value
                for key, value in sorted(self.facts.items())
                if len(key.split(".")) == len(pattern)
                and not (
                    self.wildcard_symbols is not None
                    and len(pattern) > 1
                    and pattern[0] in {"stocks_data", "quotes"}
                    and pattern[1] == "*"
                    and key.split(".")[1] not in self.wildcard_symbols
                )
                and all(a == b or a == "*" for a, b in zip(pattern, key.split(".")))
            ]
            if matches:
                return FactValue(
                    value=matches,
                    status="available",
                    observed_at=self.observed_at,
                    source="fact_snapshot",
                    path=path,
                )
        return FactValue(
            observed_at=self.observed_at, error="fact not prepared", path=path
        )


@dataclass
class TriggerFactProviders:
    quote: Callable | None = None  # quote(code) -> source dict or FactValue
    history: Callable | None = None  # history(code, days) -> dated DataFrame
    valuation: Callable | None = None  # valuation(code) -> mapping
    announcements: Callable | None = None  # (codes, start, end) -> strict statuses
    report: Callable | None = None  # (paths, symbols, as_of) -> prepared report mapping
    market_session: Callable | None = None  # (code, *, as_of) -> MarketSession


def _clock(value, *, zone=LOCAL_ZONE, date_only=True):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        if not date_only:
            return None
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        if not date_only and len(value.strip()) <= 10:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _symbol(value):
    code = str(value).strip().upper().replace(".", "-")
    if not re.fullmatch(r"(?:\d{5,6}|[A-Z][A-Z0-9-]{0,14})", code):
        raise ValueError(f"invalid security identity: {value}")
    return code


def monitored_symbols(config: dict) -> list[str]:
    stocks = config.get("stocks", [])
    return list(
        dict.fromkeys(
            _symbol(row.get("code", row.get("stock_code")))
            if isinstance(row, dict)
            else _symbol(row)
            for row in stocks
        )
    )


def normalize_q3_event(row: dict, code: str, *, observed_at: datetime, as_of=None):
    """Recognize an actual third-quarter report, not forecasts or generic 10-Q."""
    if not isinstance(row, dict):
        raise TypeError("announcement row is not an object")
    found_code = row.get("stock_code", row.get("symbol"))
    if found_code is None or _symbol(found_code) != code:
        raise ValueError("announcement security identity mismatch")
    title = re.sub(r"<[^>]*>", "", str(row.get("title", ""))).strip()
    text = re.sub(r"\s+", "", title).lower()
    numerals = str.maketrans("〇零一二三四五六七八九", "00123456789")
    period_text = re.sub(
        r"[二〇零一三四五六七八九]{4}(?=年)",
        lambda match: match.group().translate(numerals),
        text,
    )
    if re.search(
        r"年度報告|年度报告|年报|年報|半年度|半年報|半年报|中期|"
        r"业绩预告|業績預告|业绩快报|業績快報|董事会|董事會|监事会|監事會|"
        r"说明会|說明會|業績會|业绩会|提示性|取消|撤销|撤銷|"
        r"annualreport|interimreport|earningsforecast|earningscall|webcast|conference",
        text,
    ):
        return None
    explicit = str(row.get("report_period", "")).upper()
    q3 = re.search(
        r"(?:第三|三|第3|3)季(?:度)?(?:报告|報告|报|報|"
        r"(?:业绩|業績)(?:公告|公佈|公布|報告|报告|$))|"
        r"thirdquarter(?:ly)?.{0,8}(?:report|results)|"
        r"(?:report.*q3|q3.*report)",
        text,
    )
    september_ytd = (
        re.search(r"20\d{2}年(?:九|9)月(?:三十|30)日止.*九[個个]月", period_text)
        or re.search(r"ninemonthsended(?:september30|30september)", text)
    ) and re.search(r"業績|业绩|報告|报告|results|report", text)
    report_end = str(row.get("report_date", row.get("reportDate", "")))
    if str(row.get("type", "")).upper() in {"10-Q", "10-Q/A"} and not (
        report_end or re.fullmatch(r"20\d{2}Q[1-4]", explicit)
    ):
        raise ValueError("SEC quarterly report period unavailable")
    if (
        not q3
        and not september_ytd
        and not (
            str(row.get("type", "")).upper() in {"10-Q", "10-Q/A"}
            and (
                re.fullmatch(r"20\d{2}-09-30", report_end)
                or re.fullmatch(r"20\d{2}Q3", explicit)
            )
        )
    ):
        return None
    years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", period_text)
    period = explicit if re.fullmatch(r"20\d{2}Q3", explicit) else None
    if explicit and not period:
        raise ValueError("Q3 title conflicts with disclosed report period")
    if period and years and any(year != period[:4] for year in years):
        raise ValueError("Q3 report year conflicts with disclosed report period")
    if period is None and len(set(years)) == 1:
        period = f"{years[0]}Q3"
    if period is None and re.fullmatch(r"20\d{2}-09-30", report_end):
        period = f"{report_end[:4]}Q3"
    if period is None:
        raise ValueError("Q3 announcement report year unavailable")
    zone = ZoneInfo("America/New_York") if row.get("exchange") == "sec" else LOCAL_ZONE
    published = _clock(row.get("published_at", row.get("date")), zone=zone)
    if published is None or published > (as_of or observed_at):
        raise ValueError("announcement publication time missing or in the future")
    url = str(row.get("url", ""))
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("announcement document URL missing")
    canonical_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )
    # Full report, summary and translations are one business publication event.
    identity = f"{code}|{Q3_KIND}|{period}|{published.date().isoformat()}"
    document_id = str(row.get("announcement_id", canonical_url))
    return AnnouncementEvent(
        id=hashlib.sha256(identity.encode()).hexdigest(),
        kind=Q3_KIND,
        symbol=code,
        occurred_at=published,
        published_at=published,
        report_period=period,
        source=str(row.get("exchange", "disclosure")),
        title=title,
        url=canonical_url,
        publication_date=published.date(),
        observed_at=observed_at,
        source_sha256=row.get("source_sha256"),
        source_urls=(canonical_url,),
        document_ids=(document_id,),
        source_documents=(
            {
                "id": document_id,
                "url": canonical_url,
                "published_at": published.isoformat(),
                "source_sha256": row.get("source_sha256"),
            },
        ),
    )


def _get_provider(providers, name):
    return (
        providers.get(name) if isinstance(providers, dict) else getattr(providers, name)
    )


def _quote_facts(code, quote, *, now, observed, session_provider, max_age):
    from ..data.quote_time import parse_quote_time
    from ..instruments.classifier import detect_market

    if not isinstance(quote, dict):
        raise TypeError("live quote unavailable")
    identity = quote.get("stock_code", quote.get("symbol", code))
    if _symbol(identity) != code:
        raise ValueError("quote security identity mismatch")
    zone = ZoneInfo(
        {
            "a_share": "Asia/Shanghai",
            "hk": "Asia/Hong_Kong",
            "us": "America/New_York",
            "sg": "Asia/Singapore",
        }[detect_market(code)]
    )
    source_time = _clock(quote.get("quote_time"), zone=zone, date_only=False)
    if source_time is None:
        source_time = parse_quote_time(quote.get("time", ""), code)
    if source_time is None:
        raise ValueError("quote source timestamp unavailable")
    market_session = session_provider(code, as_of=now)
    fresh = (
        market_session is not None
        and market_session.is_open(now)
        and market_session.is_open(source_time)
        and 0 <= (now - source_time).total_seconds() <= max_age
    )
    reason = (
        None if fresh else "quote is stale, future, or outside the open market session"
    )
    result = {}
    for name in ("last", "open", "high", "low", "prev_close", "change_pct", "fresh"):
        value = (
            fresh
            if name == "fresh"
            else quote.get(name, quote.get("close") if name == "last" else None)
        )
        valid = name == "fresh" or _finite(value)
        if name in {"last", "open", "high", "low", "prev_close"} and valid:
            valid = value > 0
        path = f"quotes.{code}.{name}"
        result[path] = FactValue(
            value=value if valid else None,
            status=("available" if fresh or name == "fresh" else "stale")
            if valid
            else "unknown",
            as_of=source_time,
            observed_at=observed,
            source="qq_realtime_quote",
            error=reason if valid else "quote field unavailable",
            security=code,
            path=path,
        )
    return result


def _snapshot_fields(snapshot, source, *, observed):
    """Capture only explicitly prepared sections, without treating defaults as known."""
    mapping = data_mapping(source)
    from pydantic import BaseModel

    fields_set = (
        source.__fields_set__ if isinstance(source, BaseModel) else set(mapping)
    )
    metadata = mapping.get("_fact_metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    prepared = {
        key: value
        for key, value in mapping.items()
        if key in fields_set
        or (
            value is not None
            and not (isinstance(value, (list, dict, tuple)) and not value)
        )
    }
    rows = {
        str(code).replace(".", "-"): value
        for code, value in data_mapping(prepared.get("stocks_data", {})).items()
    }
    created = _clock(mapping.get("created_at"))
    for path, value in iter_safe_leaves(prepared):
        parts = path.split(".")
        code = parts[1] if parts[0] == "stocks_data" and len(parts) > 2 else None
        row = data_mapping(rows.get(code)) if code else {}
        clock = _clock(row.get("quote_time", row.get("date"))) if code else created
        info = metadata.get(path, {})
        if not isinstance(info, dict):
            info = {}
        if "as_of" in info:
            clock = _clock(info["as_of"])
        missing = value is None or (
            isinstance(value, float) and not math.isfinite(value)
        )
        snapshot.facts[path] = FactValue(
            value=None if missing else value,
            status="unknown" if missing else "available",
            as_of=clock,
            observed_at=_clock(info.get("observed_at")) or observed,
            source=info.get("source", "report_snapshot"),
            security=code,
            error="report field unavailable" if missing else None,
            path=path,
        )
    # Top-level collections support non_empty/exists without exposing internal config.
    for key, value in prepared.items():
        if safe_path(key) and isinstance(value, (dict, list, tuple)):
            public = {path: leaf for path, leaf in iter_safe_leaves(value, key)}
            info = metadata.get(key, {})
            if not isinstance(info, dict):
                info = {}
            snapshot.facts.setdefault(
                key,
                FactValue(
                    value=public if value else value,
                    status="available",
                    as_of=_clock(info.get("as_of")) if "as_of" in info else created,
                    observed_at=_clock(info.get("observed_at")) or observed,
                    source=info.get("source", "report_snapshot"),
                    path=key,
                ),
            )


def _history_row(config, code, history, as_of):
    import pandas as pd

    from ..core.data_fetcher import StockDataFetcher
    from ..data.technical_indicators import TechnicalIndicators

    if not isinstance(history, pd.DataFrame) or history.empty or "date" not in history:
        raise ValueError("dated price history unavailable")
    history = history.copy()
    dates = pd.to_datetime(history["date"], utc=True, errors="coerce")
    history = history.loc[dates.notna() & (dates <= pd.Timestamp(as_of))]
    if history.empty:
        raise ValueError("history has no point-in-time prices")
    history = history.sort_values("date").drop_duplicates("date", keep="last")
    close = pd.to_numeric(history.get("close"), errors="coerce")
    history = history.loc[close.notna() & (close > 0)].reset_index(drop=True)
    if history.empty:
        raise ValueError("history has no valid closing prices")
    indicators = TechnicalIndicators(
        config_path=config.get("alerts", {}).get("config_path")
    )
    history = indicators.calculate_indicators(history, stock_code=code)
    history = StockDataFetcher._add_extended_indicators(history)
    row = history.iloc[-1].to_dict()
    # The shared report calculator intentionally supports partial windows. A
    # trigger must not call a one-quote fallback a 60-day MA or a mature MACD.
    windows = {
        "rsi": 15,
        "rsi_14": 15,
        "macd": 26,
        "macd_signal": 34,
        "macd_hist": 34,
        "adx": 28,
        "plus_di": 14,
        "minus_di": 14,
        "boll_ma": 20,
        "boll_middle": 20,
        "boll_upper": 20,
        "boll_lower": 20,
        "boll_pct_b": 20,
        "boll_bandwidth": 20,
        "vol_ratio": 20,
        "volume_ratio": 20,
    }
    for name, window in windows.items():
        if len(history) < window:
            row[name] = None
    weekly_count = None
    for anchor in indicators.anchors_config:
        name, window = anchor.get("name"), anchor.get("window")
        if not name or type(window) is not int or window <= 0:
            continue
        count = len(history)
        if anchor.get("type") == "weekly_ma":
            if weekly_count is None:
                dated = history.set_index(pd.to_datetime(history["date"]))["close"]
                weekly = dated.resample("W-FRI").last().dropna()
                weekly_count = len(weekly.loc[weekly.index <= dated.index.max()])
            count = weekly_count
        if count < window:
            row[name] = None
    return row


def load_trigger_facts(
    dependencies,
    config: dict,
    *,
    as_of: datetime,
    providers=None,
    symbols=None,
    session=None,
    max_quote_age_seconds=120,
    clock: Callable[[], datetime] | None = None,
    cancelled: Callable[[], bool] | None = None,
    budget_seconds: float | None = None,
) -> FactSnapshot:
    """Load free producers; optionally finalize live freshness at collection end.

    Without ``clock`` as_of is a strict historical cutoff. Runtime polls pass an
    aware clock, so quotes fetched after the poll began share one completion time.
    Expensive fields always require a prepared report snapshot.
    """
    if as_of.tzinfo is None:
        raise ValueError("trigger as_of must include a timezone")
    if not _finite(max_quote_age_seconds) or max_quote_age_seconds <= 0:
        raise ValueError("quote max age must be a positive finite number")
    if budget_seconds is not None and (
        not _finite(budget_seconds) or budget_seconds <= 0
    ):
        raise ValueError("fact collection budget must be positive and finite")
    deadline = time.monotonic() + budget_seconds if budget_seconds is not None else None

    def remaining_budget():
        if cancelled is not None and cancelled():
            raise ValueError("fact collection cancelled")
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise ValueError("fact collection budget exhausted")
        return remaining

    observed = datetime.now(LOCAL_ZONE)
    snapshot = FactSnapshot(as_of=as_of, observed_at=observed)
    providers = providers or TriggerFactProviders()
    dependencies = set(dependencies)
    if any(not safe_path(path) for path in dependencies):
        raise ValueError("unsafe trigger fact dependency")
    codes = (
        monitored_symbols(config)
        if symbols in (None, "monitored")
        else [_symbol(code) for code in symbols]
    )
    snapshot.wildcard_symbols = tuple(codes)
    explicit_codes = {
        _symbol(parts[1])
        for path in dependencies
        if len(parts := path.split(".")) >= 3
        and parts[0] in {"stocks_data", "quotes"}
        and parts[1] != "*"
    }
    if session is not None:
        _snapshot_fields(snapshot, session, observed=observed)
    report = _get_provider(providers, "report")
    pending = {
        path for path in dependencies if snapshot.resolve(path).status == "unknown"
    }
    if report and any(not path.startswith(("quotes.", "events.")) for path in pending):
        try:
            remaining_budget()
            _snapshot_fields(snapshot, report(pending, codes, as_of), observed=observed)
        except Exception as exc:  # noqa: BLE001 - isolate unavailable source facts
            for path in pending:
                snapshot.facts[path] = FactValue(
                    error=str(exc), path=path, observed_at=observed
                )
    crawler = None

    def crawler_instance():
        nonlocal crawler
        if crawler is None:
            from ..data.web_crawler import StockWebCrawler

            crawler = StockWebCrawler(config)
        remaining = remaining_budget()
        if remaining is not None:
            crawler.timeout = max(0.001, min(30, remaining))
        return crawler

    def quote_provider(code):
        callback = _get_provider(providers, "quote")
        return (
            callback(code)
            if callback
            else crawler_instance().fetch_realtime_quote(code)
        )

    def session_provider(code, *, as_of):
        callback = _get_provider(providers, "market_session")
        if callback:
            return callback(code, as_of=as_of)
        from ..data.market_calendar import resolve_market_session

        return resolve_market_session(code, as_of=as_of)

    requested, collected_quotes = {}, {}
    for path in dependencies:
        parts = path.split(".")
        if len(parts) >= 3 and parts[0] in {"stocks_data", "quotes"}:
            selected = codes if parts[1] == "*" else [_symbol(parts[1])]
            for code in selected:
                requested.setdefault(code, set()).add(
                    ".".join((parts[0], code, *parts[2:]))
                )
    for code, paths in requested.items():
        quote_paths = {path for path in paths if path.startswith("quotes.")}
        if quote_paths:
            try:
                remaining_budget()
                collected_quotes[code] = quote_provider(code)
            except Exception as exc:  # noqa: BLE001 - isolate unavailable source facts
                for path in quote_paths:
                    snapshot.facts[path] = FactValue(
                        error=str(exc), path=path, security=code, observed_at=observed
                    )
        missing = {
            path
            for path in paths
            if snapshot.resolve(path).status == "unknown"
            and path.startswith("stocks_data.")
        }
        for group, field_names in (
            ("history", HISTORY_FIELDS),
            ("valuation", VALUATION_FIELDS),
        ):
            needed = {path for path in missing if path.split(".")[-1] in field_names}
            if not needed:
                continue
            try:
                remaining_budget()
                callback = _get_provider(providers, group)
                if group == "history":
                    history = (
                        callback(code, 400)
                        if callback
                        else crawler_instance().fetch_stock_data(code, days=400)
                    )
                    row = _history_row(config, code, history, as_of)
                    source_time = _clock(row.get("date"))
                else:
                    from ..data.etf_valuation import ETFValuationResolver

                    if callback:
                        row = callback(code)
                    elif ETFValuationResolver.supports(code):
                        row = {
                            "_error": "ETF look-through valuation awaiting completed report snapshot"
                        }
                    else:
                        row = crawler_instance().fetch_valuation_data(code)
                    if not isinstance(row, dict):
                        raise ValueError("valuation unavailable")
                    source_time = _clock(row.get("as_of", row.get("quote_time")))
                for path in needed:
                    value = row.get(path.split(".")[-1])
                    if hasattr(value, "item"):
                        value = value.item()
                    missing_value = value is None or (
                        isinstance(value, float) and not math.isfinite(value)
                    )
                    snapshot.facts[path] = FactValue(
                        value=None if missing_value else value,
                        status="unknown" if missing_value else "available",
                        as_of=source_time,
                        observed_at=observed,
                        source=f"free_{group}",
                        security=code,
                        path=path,
                        error=row.get("_error", "source field unavailable")
                        if missing_value
                        else None,
                    )
            except Exception as exc:  # noqa: BLE001 - isolate unavailable source facts
                for path in needed:
                    snapshot.facts[path] = FactValue(
                        error=str(exc), path=path, security=code, observed_at=observed
                    )
    if any(path == "events" or path.startswith("events.") for path in dependencies):
        start, end = (
            as_of.astimezone(LOCAL_ZONE).date() - timedelta(days=1),
            as_of.astimezone(LOCAL_ZONE).date(),
        )
        try:
            remaining_budget()
            callback = _get_provider(providers, "announcements")
            if callback:
                statuses = callback(codes, start, end)
            else:
                from ..data.announcement_fetcher import AnnouncementFetcher

                safe_config = {
                    **config,
                    "announcements": {
                        **config.get("announcements", {}),
                        "enable_content_fetching": False,
                        "enable_llm_extraction": False,
                    },
                }
                statuses = AnnouncementFetcher(safe_config).fetch_announcements_status(
                    codes,
                    start,
                    end,
                    observed_at=observed,
                    cancelled=cancelled,
                    deadline=deadline,
                )
        except Exception as exc:  # noqa: BLE001 - isolate unavailable source facts
            statuses = {code: {"available": False, "error": str(exc)} for code in codes}
        errors = []
        for code in codes:
            status = statuses.get(code, {}) if isinstance(statuses, dict) else {}
            try:
                if status.get("available") is not True:
                    raise ValueError(
                        status.get("error") or "announcement coverage unavailable"
                    )
                if (
                    status.get("coverage_start") != start
                    or status.get("coverage_end") != end
                ):
                    raise ValueError("announcement window coverage unproven")
                items = status.get("items")
                if not isinstance(items, list):
                    raise TypeError("announcement response schema missing")
                events = []
                for item in items:
                    event = normalize_q3_event(
                        item,
                        code,
                        observed_at=observed,
                        as_of=clock() if clock else as_of,
                    )
                    if event and start <= event.publication_date <= end:
                        events.append(event)
                snapshot.events.extend(events)
                fact = FactValue(
                    value=True,
                    status="available",
                    as_of=as_of,
                    observed_at=observed,
                    source=status.get("source"),
                    security=code,
                )
            except Exception as exc:  # noqa: BLE001 - isolate unavailable source facts
                errors.append(f"{code}: {exc}")
                fact = FactValue(error=str(exc), observed_at=observed, security=code)
            snapshot.facts[f"announcements.{code}.coverage"] = fact
        publications = {}
        for event in snapshot.events:
            previous = publications.get(event.id)
            if previous is None:
                publications[event.id] = event
                continue
            documents = {
                item["url"]: item
                for item in (*previous.source_documents, *event.source_documents)
            }
            publications[event.id] = replace(
                previous,
                source_urls=tuple(documents),
                document_ids=tuple(
                    dict.fromkeys(item["id"] for item in documents.values())
                ),
                source_documents=tuple(documents.values()),
                published_at=min(previous.published_at, event.published_at),
                occurred_at=min(previous.occurred_at, event.occurred_at),
            )
        snapshot.events = list(publications.values())
        snapshot.facts[f"events.{Q3_KIND}"] = FactValue(
            value=snapshot.events,
            status="unknown" if errors else "available",
            as_of=as_of,
            observed_at=observed,
            source="official_disclosures",
            error="; ".join(errors) or None,
        )
    finished = clock() if clock else as_of
    if not isinstance(finished, datetime) or finished.tzinfo is None:
        raise ValueError("trigger collection clock must return an aware datetime")
    snapshot.as_of = finished
    if clock:
        snapshot.observed_at = finished
    for code, quote in collected_quotes.items():
        try:
            snapshot.facts.update(
                _quote_facts(
                    code,
                    quote,
                    now=finished,
                    observed=finished if clock else observed,
                    session_provider=session_provider,
                    max_age=max_quote_age_seconds,
                )
            )
        except Exception as exc:  # noqa: BLE001 - isolate unavailable source facts
            for path in requested[code]:
                if path.startswith("quotes."):
                    snapshot.facts[path] = FactValue(
                        error=str(exc),
                        path=path,
                        security=code,
                        observed_at=finished if clock else observed,
                    )
    for code, paths in requested.items():
        for path in paths:
            snapshot.facts.setdefault(
                path,
                FactValue(
                    error="report data awaiting update",
                    path=path,
                    security=code,
                    observed_at=observed,
                ),
            )
    catalog = build_report_field_catalog(session)
    # Last reports may contain removed securities. Explicit dependencies can
    # still name one, while wildcard resolution remains confined to this poll's
    # selected universe, even when both appear in the same snapshot.
    allowed_codes = set(codes) | explicit_codes
    snapshot.facts = {
        path: fact
        for path, fact in snapshot.facts.items()
        if not (
            len(parts := path.split(".")) >= 3
            and parts[0] in {"stocks_data", "quotes"}
            and parts[1] not in allowed_codes
        )
    }
    for root in ("stocks_data", "quotes"):
        if root in snapshot.facts:
            retained = {
                path: fact.value
                for path, fact in snapshot.facts.items()
                if path.startswith(root + ".")
            }
            original = snapshot.facts[root]
            snapshot.facts[root] = replace(
                original,
                value=retained,
                status=original.status if retained or not codes else "unknown",
            )
    for path in dependencies:
        if "*" not in path and path not in snapshot.facts:
            definition = catalog.resolve(path)
            snapshot.facts[path] = FactValue(
                error="report data awaiting update"
                if definition
                else "unknown report field",
                observed_at=observed,
                path=path,
            )
    return snapshot
