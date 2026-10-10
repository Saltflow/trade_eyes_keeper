"""Stream companies through fail-closed research gates without a batch barrier."""

from __future__ import annotations

# Provider and model boundaries must turn every unexpected failure into a
# persisted, user-visible blocker; exceptions are never converted to success.
# ruff: noqa: BLE001
import copy
import hashlib
import json
import logging
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

import pytz

from ..core.process_lock import exclusive_process_lock
from . import CONTRACT_VERSION
from .calculations import (
    ANNUAL_CORE,
    SPECIAL,
    VERSION,
    calculate,
    evaluate_breakers,
    qualification,
    readiness,
    scenarios,
    screen,
    source_crosschecks,
)
from .cashflow_bridge import (
    missing_requirements,
    validate_bridge_review,
    validate_proposal,
)
from .contracts import (
    canonical,
    envelope,
    fingerprint,
    issue,
    prepare_extracted_facts,
    validate_analysis,
    validate_packet,
    validate_review,
)
from .currency_definitions import find_currency_definitions
from .prompts import PROMPT_VERSION, messages_for
from .store import ResearchStore

logger = logging.getLogger(__name__)


class ResearchBlocked(RuntimeError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(item["code"] for item in errors))


def evidence_hash(packet, *, exclude_prices=False):
    """Wall-clock/as-of refresh alone is not new evidence or a retry permit."""
    value = {
        key: packet.get(key)
        for key in (
            "company",
            "facts",
            "sources",
            "peers",
            "peer_candidates",
            "peer_packets",
            "page_reviews",
        )
    }
    value["page_reviews"] = value["page_reviews"] or []
    if exclude_prices:
        value = copy.deepcopy(value)
        for company in [value.get("company"), *(value.get("peer_candidates") or [])]:
            if isinstance(company, dict):
                for field in (
                    "market_cap",
                    "quote_as_of",
                    "size_status",
                    "membership_as_of",
                ):
                    company.pop(field, None)
        value["peer_packets"] = [
            evidence_hash(p, exclude_prices=True)
            for p in value.get("peer_packets") or []
        ]
        quote_ids = {
            s["id"] for s in value.get("sources") or [] if s.get("kind") == "quote"
        }
        value["sources"] = [
            s for s in value.get("sources") or [] if s["id"] not in quote_ids
        ]
        value["facts"] = [
            f for f in value.get("facts") or [] if f.get("source_id") not in quote_ids
        ]
    return fingerprint(value)


def compact_calculation_refs(value):
    """Losslessly intern repeated input-ID lists; no evidence is summarized away."""
    groups = {}
    for metric in value.get("calculations", {}).values():
        if not isinstance(metric, dict) or "fact_refs" not in metric:
            continue
        refs = metric.pop("fact_refs")
        key = fingerprint(refs)
        groups[key] = refs
        metric["fact_ref_group"] = key
    if groups:
        value["calculation_ref_groups"] = groups
    return value


class FundamentalQuantService:
    def __init__(
        self,
        config: dict,
        *,
        sources=None,
        llm=None,
        today=None,
        budget_store=None,
        budget_day=None,
    ):
        self.config = config
        self.settings = config.get("fundamental_quant", {}) or {}
        self.root = Path(self.settings.get("root", "data/fundamental_quant"))
        self.today = today or datetime.now(pytz.timezone("Asia/Shanghai")).date()
        self._live_calendar = (
            today is None
            and budget_day is None
            and not self.settings.get("evaluation_run_id")
        )
        self.budget_day = budget_day or (
            datetime.now(pytz.timezone("Asia/Shanghai")).date()
            if self.settings.get("evaluation_run_id")
            else self.today
        )
        self._sources, self._llm = sources, llm
        self._vision_llm = llm
        self._extraction_llm = llm
        self._store = None
        self._budget_store = budget_store
        self._packets = {}
        self._usage = {}
        self._discovery_reservations = set()
        self._continuation_ids = set()
        self._continuation_ranks = {}
        self._bootstrap = None
        self._acceptance = None
        self._acceptance_dependency_before = {}

    @property
    def acceptance(self):
        if self._acceptance is None:
            from .acceptance_campaign import AcceptanceCampaign

            self._acceptance = AcceptanceCampaign(self.settings, self.budget_store)
        return self._acceptance

    @property
    def bootstrap(self):
        if self._bootstrap is None:
            from .bootstrap import BootstrapAcquisition

            self._bootstrap = BootstrapAcquisition(
                self.settings, self.store, self._limit("daily_acquisition_limit", 50)
            )
        return self._bootstrap

    def _acquisition_limit(self):
        return self.bootstrap.state["daily_acquisition_limit"]

    def _roll_budget_day(self):
        if self._live_calendar:
            current = datetime.now(pytz.timezone("Asia/Shanghai")).date()
            if current != self.budget_day:
                previous = self.budget_day
                self.today = self.budget_day = current
                self._discovery_reservations.clear()
                self._event(
                    "",
                    "budget_day_changed",
                    {"previous": str(previous), "current": str(current)},
                )

    def _progress(self, phase, issuer=""):
        from .worker import _atomic_json

        _atomic_json(
            self.root / "worker" / "runtime" / "progress.json",
            {
                "pid": os.getpid(),
                "phase": phase,
                "company_id": issuer,
                "updated_at": datetime.now(pytz.UTC).isoformat(),
                "budget_day": self.budget_day.isoformat(),
            },
        )

    @property
    def store(self):
        if self._store is None:
            self._store = ResearchStore(self.root)
        return self._store

    @property
    def sources(self):
        if self._sources is None:
            from .sources import ResearchSources

            self._sources = ResearchSources(
                self.config,
                reserve_discovery_issuer=self._reserve_discovery_issuer,
                discovery_priority=self.bootstrap.priority,
            )
        return self._sources

    def _reserve_discovery_issuer(self, issuer):
        # A slot prepaid by continuation/bootstrap may fund this batch's quote
        # discovery too. Across restarts it is still charged only once.
        if issuer in self._discovery_reservations:
            return True
        accepted = self.budget_store.reserve(
            self.budget_day.isoformat(),
            "acquisition",
            self._acquisition_limit(),
            key=issuer,
        )
        if accepted:
            self._discovery_reservations.add(issuer)
        return accepted

    def _reserve_bootstrap_pending(self):
        if self.bootstrap.state["phase"] != "bootstrap":
            return
        remaining = self._acquisition_limit() - self.budget_store.budgets(
            self.budget_day.isoformat()
        ).get("acquisition", 0)
        if remaining <= 0:
            return
        rows = sorted(
            self.store.companies(include_packet=False),
            key=lambda r: (
                self.bootstrap.priority(r["id"]),
                not r["company"].get("a500", False),
                r["updated_at"],
                r["id"],
            ),
        )
        for row in rows:
            if (
                row["id"] in self.bootstrap.pending
                and row["id"] not in self._discovery_reservations
                and self._reserve_discovery_issuer(row["id"])
            ):
                remaining -= 1
                if remaining == 0:
                    break

    def _continuation_priority(self, packet):
        """Rank frozen, validated evidence; this never grants a gate exception."""
        accepted, errors = validate_packet(packet, self.today)
        if packet.get("company", {}).get("peer_only"):
            return None
        validated = {**packet, "facts": accepted}
        result = screen(validated, self.today, self.settings)
        if result["status"] == "fail":
            return None
        missing = readiness(validated, self.today)
        # A history issue represents several fiscal-year cells; counting it as
        # one would favor an empty series over partially verified statements.
        missing_cells = sum(
            len(json.loads(problem["message"].split(": ", 1)[1]))
            if problem["code"] == "history_incomplete"
            else 1
            for problem in missing
        )
        originals = {
            source["id"]
            for source in packet.get("sources", [])
            if source.get("kind") in {"annual", "interim"}
            and source.get("verification_level") != "provider_parsed"
        }
        primary_cells = {
            (fact["metric"], fact["period_end"], fact["period_type"])
            for fact in accepted
            if fact.get("source_id") in originals
        }
        cells = {
            (fact["metric"], fact["period_end"], fact["period_type"])
            for fact in accepted
        }
        return (
            len(errors),
            {"pass": 0, "cycle_exception": 1, "unknown": 2}[result["status"]],
            missing_cells,
            sum(value is None for value in result["checks"].values()),
            -len(primary_cells),
            -len(cells),
        )

    def _reserve_continuations(self, *, reserve=True):
        """Spend scarce continuation slots on the nearest verifiable companies."""
        limit = min(
            self._limit("daily_research_limit", 20),
            self._acquisition_limit(),
        )
        if not limit:
            return
        with self.store.transaction() as db:
            rows = db.execute(
                """SELECT id,updated_at FROM companies WHERE packet IS NOT NULL
                AND stage NOT IN ('published','screened','excluded') AND screen<>'fail'
                AND EXISTS (SELECT 1 FROM json_each(companies.packet,'$.sources') s
                    WHERE json_extract(s.value,'$.page') > 0
                    AND (json_extract(s.value,'$.kind') IN ('annual','interim')
                         OR json_extract(s.value,'$.document_kind') IN ('annual','interim')))
                ORDER BY updated_at,id""",
            ).fetchall()
        ranked = []
        for row in rows:
            # Load one large original packet at a time, never the whole pool.
            with self.store.transaction() as db:
                saved = db.execute(
                    "SELECT packet FROM companies WHERE id=?", (row["id"],)
                ).fetchone()
            try:
                priority = self._continuation_priority(json.loads(saved[0]))
            except (KeyError, TypeError, ValueError) as exc:
                self._event(
                    row["id"],
                    "continuation_priority_error",
                    issue("invalid_saved_evidence", message=type(exc).__name__),
                )
                continue
            if priority is not None:
                ranked.append((priority, row["updated_at"], row["id"]))
        self._continuation_ids.clear()
        self._continuation_ranks.clear()
        for rank, (priority, _, issuer) in enumerate(sorted(ranked)[:limit]):
            self._continuation_ids.add(issuer)
            self._continuation_ranks[issuer] = rank
            if reserve:
                self._reserve_discovery_issuer(issuer)
        self._event(
            "",
            "continuation_queue",
            {
                "ordering": "validation_screen_readiness_primary_cells",
                "eligible": len(ranked),
                "selected": [
                    {"company_id": issuer, "priority": list(priority)}
                    for priority, _, issuer in sorted(ranked)[:limit]
                ],
            },
        )

    @property
    def budget_store(self):
        return self._budget_store if self._budget_store is not None else self.store

    def _limit(self, key, default):
        value = self.settings.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"invalid fundamental_quant.{key}")
        return value

    def _model_budget_state(self):
        if self.acceptance.enabled:
            state = self.acceptance.state()
            if state.get("config"):
                return {
                    "day": self.budget_day.isoformat(),
                    "campaign_id": state["id"],
                    "status": state["status"],
                    "limits": self.acceptance.limits(state),
                    "usage": state["usage"],
                }
        return {
            "day": self.budget_day.isoformat(),
            "limits": {
                "model_calls": self._limit("daily_model_call_limit", 80),
                "input_token_reservations": self._limit(
                    "daily_input_token_limit", 10000000
                ),
                "output_token_reservations": self._limit(
                    "daily_output_token_limit", 500000
                ),
            },
            "usage": self.budget_store.budgets(self.budget_day.isoformat()),
        }

    def _versions(self):
        from .llm import TRANSPORT_VERSION
        from .page_evidence import PAGE_EVIDENCE_VERSION
        from .research_calculations import VERSION as RESEARCH_CALCULATION_VERSION
        from .research_prompts import PROMPT_REVISION
        from .retrieval import RETRIEVAL_VERSION

        return {
            "rules": VERSION,
            "prompt": PROMPT_VERSION,
            "research_prompt": PROMPT_REVISION,
            "research_calculations": RESEARCH_CALCULATION_VERSION,
            "retrieval": RETRIEVAL_VERSION,
            "research_context": "recent-interims-and-cited-evidence/1",
            "parser": CONTRACT_VERSION,
            "page_review": PAGE_EVIDENCE_VERSION,
            "transport": TRANSPORT_VERSION,
            "vision_model": self.settings.get("llm", {}).get(
                "vision_model", "deepseek-flash"
            ),
            "model": self.settings.get("llm", {}).get(
                "model", self.config.get("llm", {}).get("model")
            ),
            "parameters": {
                "temperature": self.settings.get("llm", {}).get("temperature", 0),
                "max_tokens": self.settings.get("llm", {}).get("max_tokens", 8192),
                "extract_max_tokens": self.settings.get("llm", {}).get(
                    "extract_max_tokens", 8192
                ),
                "thinking_mode": self.settings.get("llm", {}).get(
                    "thinking_mode", "enabled"
                ),
                "extraction_thinking_mode": "disabled",
                "request_timeout_seconds": self.settings.get("llm", {}).get(
                    "request_timeout_seconds", 60
                ),
                "max_context_chars": self._limit("max_request_chars", 450000),
                "source_context_chars": self._limit("max_context_chars", 360000),
                "transport_context_chars": self.settings.get("llm", {}).get(
                    "max_context_chars", self._limit("max_request_chars", 450000)
                ),
            },
            "provider_config_hash": fingerprint(
                {
                    "base_url": self.config.get("llm", {}).get("base_url")
                    or "https://api.deepseek.com/v1",
                    # Match ResearchLLM's exact effective credential precedence.
                    # Only this irreversible digest ever reaches persisted data.
                    "credential_fingerprint": hashlib.sha256(
                        str(
                            self.config.get("llm", {}).get("api_key")
                            or os.getenv("DEEPSEEK_API_KEY", "")
                        ).encode()
                    ).hexdigest(),
                }
            ),
            "hurdle": self.settings.get("hurdle", 0.07),
            "terminal_growth": self.settings.get("terminal_growth", 0.02),
        }

    def _event(self, company, kind, payload):
        self.store.event(company, kind, payload)

    def _cached_call(self, issuer, digest):
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT kind,payload FROM events WHERE company_id=? AND kind IN ('model_response','model_error','model_request') ORDER BY id DESC",
                (issuer,),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("request_hash") == digest:
                if row["kind"] == "model_response":
                    return payload["content"]
                raise ResearchBlocked(
                    [
                        issue(
                            "same_model_input_previously_failed_or_interrupted",
                            stage="model",
                        )
                    ]
                )
        return None

    def _legacy_session_cache_matches(self, issuer, digest, execution):
        """Reuse old PDF receipts only when their recorded execution matches.

        The old hash omitted PDF output limits and role. A hash match alone
        therefore cannot authorize reusing either a success or a failure.
        """
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT payload FROM events WHERE company_id=? "
                "AND kind='model_request' "
                "AND json_extract(payload,'$.request_hash')=? ORDER BY id DESC",
                (issuer, digest),
            ).fetchall()
        if not rows:
            return False
        request = json.loads(rows[0]["payload"])
        return all(
            request.get(key) == execution[key]
            for key in ("role", "requested_model", "max_output_tokens", "thinking_mode")
        ) and bool(request.get("session_id"))

    def _context(self, packet, *, research=False, extra=None):
        """Bound original pages explicitly; retain immutable full packet in SQLite.

        All cited pages are mandatory. Additional pages use deterministic risk
        term retrieval. Coverage records exclusions, not a claim of full reading.
        """
        result = copy.deepcopy(packet)
        compact_calculation_refs(result)
        explicit_refs = set()
        if research:

            def collect_refs(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key in {"fact_refs", "source_refs"} and isinstance(
                            item, list
                        ):
                            explicit_refs.update(x for x in item if isinstance(x, str))
                        else:
                            collect_refs(item)
                elif isinstance(value, list):
                    for item in value:
                        collect_refs(item)

            collect_refs(extra or {})
            collect_refs(result.get("calculations", {}))
            collect_refs(result.get("calculation_ref_groups", {}))
            # Old provider interim snapshots do not establish annual history.
            # Keep annuals, all primary facts, recent interim snapshots and every
            # explicitly cited fact. Full raw history stays in the frozen packet.
            cutoff = str(self.today.year - 1)
            original_facts = result.get("facts", [])
            result["facts"] = [
                f
                for f in original_facts
                if f.get("period_type") != "interim"
                or str(f.get("period_end", ""))[:4] >= cutoff
                or f.get("primary_verified") is True
                or f.get("id") in explicit_refs
                or f.get("verification_level") != "provider_parsed"
            ]
            result.setdefault("coverage", {})["omitted_old_provider_interim_facts"] = (
                len(original_facts) - len(result["facts"])
            )
        # Peer arithmetic is already deterministic and audited. Send current
        # primary cells plus their original pages, not five copies of every
        # seven-year observation. Full histories remain in frozen storage.
        for peer in result.get("peer_packets", []):
            compact_calculation_refs(peer)
            latest = {}
            histories = {}
            for fact in peer.get("facts", []):
                name = fact["metric"]
                if (
                    name not in latest
                    or fact["period_end"] > latest[name]["period_end"]
                ):
                    latest[name] = fact
                if fact.get("period_type") == "annual":
                    histories.setdefault(name, []).append(fact)
            selected = {f["id"]: f for f in latest.values()}
            for name, annuals in histories.items():
                annuals.sort(key=lambda f: f["period_end"])
                # Endpoints and the whole normalizing window must remain
                # inspectable; a computed median alone is not source evidence.
                window = 10 if peer["company"].get("template") == "cyclical" else 5
                for fact in [annuals[0], *annuals[-window:]]:
                    selected[fact["id"]] = fact
            peer["facts"] = list(selected.values())
            ids = {f["source_id"] for f in peer["facts"]}
            ids.update(
                f.get(key)
                for f in peer["facts"]
                for key in ("unit_source_id", "currency_source_id")
            )
            ids.update(
                ref
                for entry in (peer.get("cashflow_bridge") or {})
                .get("proposal", {})
                .get("entries", [])
                for ref in entry.get("source_refs", [])
            )
            ids.update(entry.get("source_id") for entry in peer.get("page_reviews", []))
            peer["sources"] = [s for s in peer.get("sources", []) if s["id"] in ids]
            peer["coverage"] = {
                "context": "normalization_window_and_growth_endpoints_with_original_sources",
                "full_document_read": False,
            }
        referenced = {f.get("source_id") for f in result.get("facts", [])}
        referenced.update(explicit_refs)
        referenced.update(
            f.get(key)
            for f in result.get("facts", [])
            for key in ("unit_source_id", "currency_source_id")
        )
        referenced.update(
            ref
            for entry in (packet.get("cashflow_bridge") or {})
            .get("proposal", {})
            .get("entries", [])
            for ref in entry.get("source_refs", [])
        )
        referenced.update(
            entry.get("source_id") for entry in packet.get("page_reviews", [])
        )
        required, optional = [], []
        for source in result.get("sources", []):
            (required if source["id"] in referenced else optional).append(source)
        terms = (
            "风险",
            "减值",
            "资本",
            "收购",
            "现金",
            "非经常",
            "借款",
            "租赁",
            "关联",
            "股权激励",
            "risk",
            "capital",
            "cash",
            "acquisition",
            "lease",
        )
        optional.sort(
            key=lambda s: (
                -sum(s.get("text", "").lower().count(term) for term in terms),
                s["id"],
            )
        )
        if research:
            # Stratify recent statement/risk pages before filling with generic
            # keyword hits. Counting "cash" alone favored long old note pages.
            recent = sorted(
                optional,
                key=lambda s: (str(s.get("period_end", "")), s["id"]),
                reverse=True,
            )
            selected = []
            topics = (
                "合并现金流量表",
                "合并利润表",
                "合并资产负债表",
                "非经常性损益",
                "风险因素",
                "重大风险",
                "股份支付",
                "租赁",
                "现金流量表补充资料",
            )
            for year in (str(self.today.year), str(self.today.year - 1)):
                for term in topics:
                    match = next(
                        (
                            s
                            for s in recent
                            if str(s.get("period_end", "")).startswith(year)
                            and term in re.sub(r"\s+", "", s.get("text", ""))
                            and s not in selected
                        ),
                        None,
                    )
                    if match:
                        selected.append(match)
            chosen = {s["id"] for s in selected}
            optional = selected + [s for s in optional if s["id"] not in chosen]
        limit = self._limit("max_context_chars", 360000)
        if research:
            limit = min(limit, 120000)
        result["sources"] = required
        required_count = len(required)
        result["coverage"] = {
            **result.get("coverage", {}),
            "available_source_pages": len(packet.get("sources", [])),
            "selection_method": "all_fact_sources_plus_risk_term_retrieval",
            "full_document_read": False,
        }
        if research and len(canonical(result)) > limit:
            limit = min(
                self._limit("max_context_chars", 360000),
                180000,
                len(canonical(result)) + 10000,
            )
        if len(canonical(result)) > limit:
            raise ResearchBlocked(
                [issue("required_evidence_exceeds_context_budget", stage="evidence")]
            )
        for source in optional:
            if len(canonical(result)) + len(canonical(source)) <= limit:
                result["sources"].append(source)
        result["coverage"]["selected_source_pages"] = len(result["sources"])
        result["coverage"]["omitted_source_pages"] = (
            len(optional) + required_count - len(result["sources"])
        )
        return result

    def _output_token_limit(self, role):
        settings = self.settings.get("llm", {})
        maximum = settings.get("max_tokens", 8192)
        if role in {"pdf_research", "pdf_review"}:
            maximum = self.settings.get("pdf_sessions", {}).get(
                "max_output_tokens", maximum
            )
            if type(maximum) is not int or not 128 <= maximum <= 393216:
                raise ResearchBlocked(
                    [issue("invalid_output_token_limit", stage="model")]
                )
            return maximum
        if type(maximum) is not int or not 128 <= maximum <= 32768:
            raise ResearchBlocked([issue("invalid_output_token_limit", stage="model")])
        if role == "pdf_compact":
            compact = self.settings.get("pdf_sessions", {}).get(
                "compact_max_output_tokens", 32768
            )
            if type(compact) is not int or not 128 <= compact <= 393216:
                raise ResearchBlocked(
                    [issue("invalid_compaction_output_token_limit", stage="model")]
                )
            return compact
        if role == "extract":
            extraction = settings.get("extract_max_tokens", 8192)
            if type(extraction) is not int or not 128 <= extraction <= 32768:
                raise ResearchBlocked(
                    [issue("invalid_extraction_output_token_limit", stage="model")]
                )
            return min(maximum, extraction)
        if role in {"page_transcribe", "page_review"}:
            return min(maximum, 2048)
        return maximum

    def _call(self, role, packet, extra=None, *, session_messages=None,
              session_images=None, session_limits=None, session_token_counter=None):
        self._roll_budget_day()
        self._progress(role, packet.get("company", {}).get("id", ""))
        issuer = packet["company"]["id"]
        context = packet if session_messages is not None else self._context(
            packet, research=role.startswith("research_"), extra=extra
        )
        compact_extra = (
            compact_calculation_refs(copy.deepcopy(extra)) if extra else extra
        )
        vision = role in {"page_transcribe", "page_review"}
        request_prompt_version = PROMPT_VERSION
        model = self.settings.get("llm", {}).get(
            "model", self.config.get("llm", {}).get("model", "deepseek-chat")
        )
        if session_messages is not None:
            from .session_prompts import VERSION as session_prompt_version

            request_prompt_version = session_prompt_version
            messages = session_messages
        elif vision:
            from .page_evidence import PAGE_PROMPT_VERSION, build_image_messages

            request_prompt_version = PAGE_PROMPT_VERSION
            model = self.settings.get("llm", {}).get("vision_model", "deepseek-flash")
            messages = build_image_messages(role, context, compact_extra)
        else:
            messages = messages_for(role, context, compact_extra)
        if session_messages is None and len(canonical(messages)) > self._limit("max_request_chars", 450000):
            raise ResearchBlocked(
                [issue("model_request_exceeds_budget", stage="model")]
            )
        input_reservation = len(canonical(messages).encode("utf-8"))
        if session_messages is not None:
            from .session_context import ContextManager

            manager = ContextManager(session_limits, token_counter=session_token_counter)
            measurement = manager.measure(messages, images=session_images or {})
            plan = manager.plan(messages, [], images=session_images or {})
            if plan.needed:
                raise ResearchBlocked([issue("session_compaction_required_before_call")])
            input_reservation = measurement.estimated_input_tokens
        output_reservation = self._output_token_limit(role)
        thinking_mode = (
            "disabled"
            if vision or role in {"extract", "pdf_compact"}
            else self.settings.get("llm", {}).get("thinking_mode", "enabled")
        )
        versions = self._versions()
        execution = {
            "role": role,
            "requested_model": model,
            "max_output_tokens": output_reservation,
            "thinking_mode": thinking_mode,
            "temperature": self.settings.get("llm", {}).get("temperature", 0),
            "raw_pdf_session": session_messages is not None,
        }
        identity = {
            "messages": messages,
            "model": model,
            "parameters": versions,
            "rules": VERSION,
            "evaluation_run_id": self.settings.get("evaluation_run_id"),
            "session_prompt_version": request_prompt_version,
        }
        legacy_digest = fingerprint(identity)
        if session_messages is not None:
            identity["execution"] = execution
            identity["request_identity_version"] = "pdf-effective-execution/1"
        digest = fingerprint(identity)
        cached = self._cached_call(issuer, digest)
        if (
            cached is None
            and session_messages is not None
            and self._legacy_session_cache_matches(issuer, legacy_digest, execution)
        ):
            cached = self._cached_call(issuer, legacy_digest)
        if cached is not None:
            return cached
        if self._llm is None and not self.settings.get("llm", {}).get("enabled", False):
            raise ResearchBlocked([issue("research_model_disabled", stage="model")])
        provider_hash = versions["provider_config_hash"]
        with self.store.transaction() as db:
            auth_failure = db.execute(
                "SELECT id FROM events WHERE kind='provider_auth_failed' "
                "AND json_extract(payload,'$.provider_config_hash')=? LIMIT 1",
                (provider_hash,),
            ).fetchone()
        if auth_failure:
            raise ResearchBlocked(
                [
                    issue(
                        "provider_auth_circuit_open",
                        stage="model",
                        message="Provider authentication previously failed; effective credential or endpoint must change",
                    )
                ]
            )
        reservations = {
            "model_calls": 1,
            "input_token_reservations": input_reservation,
            "output_token_reservations": output_reservation,
        }
        if self.acceptance.enabled:
            reserved = self.acceptance.reserve_model(
                self.budget_day.isoformat(), reservations
            )
        else:
            reserved = self.budget_store.reserve_many(
                self.budget_day.isoformat(),
                {
                    "model_calls": (self._limit("daily_model_call_limit", 80), 1),
                    "input_token_reservations": (
                        self._limit("daily_input_token_limit", 10000000),
                        input_reservation,
                    ),
                    "output_token_reservations": (
                        self._limit("daily_output_token_limit", 500000),
                        output_reservation,
                    ),
                },
            )
        if not reserved:
            raise ResearchBlocked(
                [
                    issue(
                        "acceptance_model_budget_exhausted"
                        if self.acceptance.enabled
                        else "daily_model_budget_exhausted",
                        message=canonical(self._model_budget_state()),
                        stage="model",
                    )
                ]
            )
        transport = (
            self._vision_llm
            if vision
            else self._extraction_llm
            if role == "extract"
            else self._llm
        )
        from .llm import ResearchLLM

        if isinstance(transport, ResearchLLM):
            actual_provider = fingerprint(
                {
                    "base_url": transport.base_url,
                    "credential_fingerprint": hashlib.sha256(
                        str(transport.api_key).encode()
                    ).hexdigest(),
                }
            )
            if actual_provider != provider_hash or (
                bool(getattr(transport, "raw_pdf_session", False))
                != (session_messages is not None)
            ) or (
                session_messages is not None and (
                    transport.model != model or transport.max_tokens != output_reservation
                    or transport.thinking_mode != thinking_mode
                    or transport.temperature != execution["temperature"]
                    or transport.timeout != self.settings.get("llm", {}).get(
                        "request_timeout_seconds", 60
                    )
                )
            ):
                transport = None
        if transport is None:
            transport_config = copy.deepcopy(self.config)
            llm_settings = transport_config.setdefault(
                "fundamental_quant", {}
            ).setdefault("llm", {})
            llm_settings.setdefault(
                "max_context_chars", self._limit("max_request_chars", 450000)
            )
            llm_settings.update(
                model=model, max_tokens=output_reservation, thinking_mode=thinking_mode
            )
            llm_settings["raw_pdf_session"] = session_messages is not None
            transport = ResearchLLM(transport_config)
            if vision:
                self._vision_llm = transport
            elif role == "extract":
                self._extraction_llm = transport
            else:
                self._llm = transport
        request = {
            "request_hash": digest,
            "role": role,
            "requested_model": model,
            "max_output_tokens": output_reservation,
            "thinking_mode": thinking_mode,
            "prompt_version": request_prompt_version,
            "input_hash": fingerprint(packet),
            "messages": messages,
            "input_reservation": input_reservation,
            "session_id": packet.get("session_id"),
            "effective_execution": execution,
        }
        self._event(issuer, "model_request", request)
        logger.info("FQ model begin issuer=%s role=%s model=%s", issuer, role, model)
        usage_recorded = False
        usage = None
        try:
            response = transport.complete(messages)
            usage = response.get("usage")
            if session_messages is not None:
                self._last_session_usage = usage
            if isinstance(usage, dict):
                self.budget_store.add_usage(self.budget_day.isoformat(), usage)
                if self.acceptance.enabled:
                    self.acceptance.add_usage(usage)
            self._usage.setdefault(issuer, []).append(
                usage if isinstance(usage, dict) else {}
            )
            usage_recorded = True
            content = response.get("content")
            if not isinstance(content, dict):
                raise TypeError("non-object model result")
            self._event(
                issuer,
                "model_response",
                {
                    "request_hash": digest,
                    "role": role,
                    "content": content,
                    "usage": usage,
                    "model": response.get("model"),
                    "transport_repairs": response.get("transport_repairs", []),
                    "transport_diagnostics": response.get("transport_diagnostics", {}),
                    "raw_assistant_content": response.get("raw_assistant_content"),
                    "usage_known": isinstance(usage, dict)
                    and all(
                        type(usage.get(k)) is int and usage[k] >= 0
                        for k in ("input_tokens", "output_tokens")
                    ),
                },
            )
            logger.info(
                "FQ model finished issuer=%s role=%s usage=%s", issuer, role, usage
            )
            return content
        except Exception as exc:
            if not usage_recorded:
                usage = getattr(exc, "usage", None)
                if isinstance(usage, dict):
                    self.budget_store.add_usage(self.budget_day.isoformat(), usage)
                    if self.acceptance.enabled:
                        self.acceptance.add_usage(usage)
                self._usage.setdefault(issuer, []).append(
                    usage if isinstance(usage, dict) else {}
                )
            error = issue(
                getattr(exc, "code", "model_response_failed"),
                stage="model",
                message=type(exc).__name__,
            )
            if error["code"] in {"http_401", "http_403", "credentials_missing"}:
                self._event(
                    issuer,
                    "provider_auth_failed",
                    {
                        "provider_config_hash": provider_hash,
                        "error_code": error["code"],
                    },
                )
            self._event(
                issuer,
                "model_error",
                {
                    "request_hash": digest,
                    "error": error,
                    "provider_config_hash": provider_hash,
                    "usage": usage,
                    "diagnostics": getattr(exc, "diagnostics", {}),
                    "raw_assistant_content": getattr(exc, "raw_content", ""),
                    "usage_known": isinstance(usage, dict)
                    and all(
                        type(usage.get(k)) is int and usage[k] >= 0
                        for k in ("input_tokens", "output_tokens")
                    ),
                },
            )
            logger.warning(
                "FQ model blocked issuer=%s role=%s code=%s",
                issuer,
                role,
                error["code"],
            )
            raise ResearchBlocked([error]) from None

    def _collect(self, company):
        self._roll_budget_day()
        issuer = company["id"]
        self._progress("collect", issuer)
        if company.get("listing_status") in {"inactive", "out_of_universe"}:
            raise ResearchBlocked(
                [
                    issue(
                        "issuer_excluded_from_universe",
                        company=issuer,
                        stage="discovery",
                    )
                ]
            )
        if issuer in self._packets:
            cached = copy.deepcopy(self._packets[issuer])
            if company.get("universe_revision"):
                cached["company"]["universe_revision"] = company["universe_revision"]
            return cached
        self._discovery_reservations.discard(issuer)
        reserved = self.budget_store.reserve(
            self.budget_day.isoformat(),
            "acquisition",
            self._acquisition_limit(),
            key=issuer,
        )
        if not reserved:
            with self.budget_store.transaction() as db:
                reserved = (
                    db.execute(
                        "SELECT 1 FROM reservations WHERE day=? AND kind='acquisition' AND key=?",
                        (self.budget_day.isoformat(), issuer),
                    ).fetchone()
                    is not None
                )
        attempts = [
            e["payload"]
            for e in self.store.events(kind="collection_attempt", company_id=issuer)
            if e["payload"].get("budget_day") == self.budget_day.isoformat()
        ]
        finished = {
            e["payload"].get("attempt_id")
            for e in self.store.events(kind="collection_finished", company_id=issuer)
        }
        # Discovery already paid for these issuers. Resume unfinished work after
        # restart; one interrupted attempt may be retried, never unboundedly.
        refresh = bool(
            reserved
            and len(attempts) < 2
            and (not attempts or attempts[-1]["attempt_id"] not in finished)
        )
        if self.acceptance.enabled:
            campaign = self.acceptance.state()
            refresh = self.acceptance.reserve_operation(
                self.budget_day.isoformat(),
                "acceptance_collection",
                issuer,
                campaign.get("config", {}).get("target_limit", 3) * 5,
            )
        attempt_id = None
        if refresh:
            attempt_id = fingerprint(
                {
                    "issuer": issuer,
                    "budget_day": self.budget_day.isoformat(),
                    "attempt": len(attempts) + 1,
                }
            )
            self._event(
                issuer,
                "collection_attempt",
                {
                    "attempt_id": attempt_id,
                    "budget_day": self.budget_day.isoformat(),
                    "attempt": len(attempts) + 1,
                },
            )
        try:
            result = self.sources.collect(company, self.today, refresh=refresh)
            if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
                raise TypeError("provider envelope missing data")
            self._event(issuer, "tool_result", result)
            packet = result["data"]
            if company.get("universe_revision"):
                packet["company"]["universe_revision"] = company["universe_revision"]
            packet["acquisition_errors"] = result.get("errors", [])
            if (
                reserved
                and not refresh
                and attempts
                and attempts[-1]["attempt_id"] not in finished
            ):
                packet["acquisition_errors"] = [
                    *packet["acquisition_errors"],
                    issue(
                        "collection_interrupted_retry_exhausted",
                        stage="data",
                        company=issuer,
                    ),
                ]
            if attempt_id:
                self._event(
                    issuer,
                    "collection_finished",
                    {
                        "attempt_id": attempt_id,
                        "budget_day": self.budget_day.isoformat(),
                        "status": result.get("status", "unknown"),
                    },
                )
            # Target and peer dependencies share the same durable extraction
            # cache. A peer-only issuer must not need its own recursive research
            # run to retain facts extracted on a previous day.
            with self.store.transaction() as db:
                previous = db.execute(
                    "SELECT packet FROM companies WHERE id=?", (issuer,)
                ).fetchone()
            if previous and previous[0]:
                prior = json.loads(previous[0])
                current_hashes = {
                    s["id"]: s.get("hash") for s in packet.get("sources", [])
                }
                old_hashes = {s["id"]: s.get("hash") for s in prior.get("sources", [])}
                present = {f["id"] for f in packet.get("facts", [])}
                packet["facts"] = [
                    *packet.get("facts", []),
                    *(
                        f
                        for f in prior.get("facts", [])
                        if f["id"] not in present
                        and f.get("source_id") in current_hashes
                        and current_hashes[f["source_id"]]
                        == old_hashes.get(f["source_id"])
                    ),
                ]
            self._packets[issuer] = packet
            return copy.deepcopy(packet)
        except Exception as exc:
            if attempt_id:
                self._event(
                    issuer,
                    "collection_finished",
                    {
                        "attempt_id": attempt_id,
                        "budget_day": self.budget_day.isoformat(),
                        "status": "failed",
                    },
                )
            error = issue(
                "collection_failed",
                stage="data",
                company=issuer,
                message=type(exc).__name__,
            )
            self._event(issuer, "tool_error", error)
            raise ResearchBlocked([error]) from None

    def _extract(self, packet, valid, errors):
        """Fact extraction is permitted only against a real, hashed primary page."""
        import re

        budget = self._model_budget_state()
        if budget["usage"].get("model_calls", 0) >= max(
            0, budget["limits"]["model_calls"] - 3
        ):
            # Blind evidence review, analysis, and crosscheck need at least
            # three calls. Missing facts cannot consume that final runway.
            problem = issue(
                "daily_model_review_budget_exhausted",
                message=canonical({**budget, "reserved_review_calls": 3}),
                stage="extract",
            )
            self._event(packet["company"]["id"], "extraction_deferred", problem)
            return valid, errors + [problem]
        docs = [
            s
            for s in packet.get("sources", [])
            if (
                s.get("kind") in {"annual", "interim"}
                or (
                    s.get("kind") == "statement"
                    and s.get("document_kind") in {"annual", "interim"}
                    and s.get("document_status") == "incomplete_text"
                    and s.get("page_status") == "complete_text"
                )
            )
            and s.get("page", 0) > 0
            and s.get("page_status") != "incomplete_text"
            and s.get("url")
            and s.get("hash") == hashlib.sha256(s.get("text", "").encode()).hexdigest()
        ]
        original_docs = list(docs)
        prior_extractions = [
            e["payload"]
            for e in self.store.events(
                kind="extraction", company_id=packet["company"]["id"]
            )
        ]
        done_pages = {
            tuple(page)
            for e in prior_extractions
            if not e.get("page_request_signatures")
            for page in e.get("pages", [])
        }
        blocked_batches = [
            e["payload"]
            for e in self.store.events(
                kind="extraction_blocked", company_id=packet["company"]["id"]
            )
        ]
        versions = self._versions()

        def changed_failure_configuration(batch):
            codes = {e.get("code") for e in batch.get("issues", [])}
            previous = batch.get("versions", {})
            if (
                codes
                <= {"daily_model_budget_exhausted", "acceptance_model_budget_exhausted"}
                and codes
            ):
                # No request was sent. A new day or increased cap may unlock it;
                # a provider failure still requires changed evidence/config.
                current = self._model_budget_state()
                old = batch.get("budget_state", {})
                changed = (
                    not old
                    or old.get("campaign_id") != current.get("campaign_id")
                    or old.get("day") != current["day"]
                    or any(
                        limit > old.get("limits", {}).get(key, 0)
                        for key, limit in current["limits"].items()
                    )
                )
                available = all(
                    current["usage"].get(key, 0) + amount <= limit
                    for key, limit in current["limits"].items()
                    for amount in [
                        self.settings.get("llm", {}).get("max_tokens", 8192)
                        if key == "output_token_reservations"
                        else 1
                    ]
                )
                return changed and available
            if codes & {
                "http_401",
                "http_403",
                "credentials_missing",
                "provider_auth_circuit_open",
            }:
                return (
                    previous.get("provider_config_hash")
                    != versions["provider_config_hash"]
                )
            if any(page[2] != PROMPT_VERSION for page in batch.get("pages", [])):
                return True
            if "invalid_response" in codes:
                return previous.get("transport") != versions.get("transport")
            parameter_failure = codes & {
                "http_400",
                "http_404",
                "context_limit",
                "incomplete_response",
                "invalid_output_token_limit",
                "model_request_exceeds_budget",
                "required_evidence_exceeds_context_budget",
            }
            if parameter_failure or any(
                str(code).startswith("invalid_config_") for code in codes
            ):
                return (previous.get("model"), previous.get("parameters")) != (
                    versions.get("model"),
                    versions.get("parameters"),
                )
            return False

        recoverable_pages = {
            tuple(page[:2])
            for e in blocked_batches
            if changed_failure_configuration(e)
            for page in e.get("pages", [])
        }
        blocked_pages = {
            tuple(page)
            for e in blocked_batches
            if not changed_failure_configuration(e)
            for page in e.get("pages", [])
        }
        docs = [
            s
            for s in docs
            if (s["id"], s["hash"], PROMPT_VERSION) not in done_pages | blocked_pages
        ]
        if not docs:
            return valid, errors + (
                [
                    issue(
                        "extraction_batches_blocked_pending_evidence_or_prompt_change",
                        stage="extract",
                    )
                ]
                if blocked_pages
                else []
            )
        required = (
            set(ANNUAL_CORE)
            | set(SPECIAL.get(packet["company"]["template"], ()))
            | {
                "total_assets",
                "total_liabilities",
                "average_parent_equity",
                "net_debt",
                "net_reinvestment",
                "interest_not_in_cfo",
                "interest_paid",
                "lease_principal",
                "lease_payments",
                "sbc_cost",
                "share_based_compensation",
                "maintenance_acquisitions",
                "minority_equity",
                "profit_before_tax",
                "income_tax_expense",
                "interest_expense",
                "depreciation_amortization",
                "operating_working_capital",
                "acquisitions_cash",
                "cash_and_cash_equivalents",
                "restricted_cash",
                "short_term_borrowings",
                "current_portion_noncurrent_debt",
                "long_term_borrowings",
                "bonds_payable",
                "lease_liabilities",
            }
        )
        template = packet["company"]["template"]
        if template in {"bank", "insurance"}:
            required = {
                "revenue",
                "net_income_parent",
                "adjusted_net_income_parent",
                "basic_eps",
                "parent_equity",
                "average_parent_equity",
                "dividends",
                "net_buybacks",
                *SPECIAL[template],
            }
            if template == "insurance":
                required.update({"new_business_value", "combined_ratio"})
        elif template == "reit":
            required = {*SPECIAL[template], "asset_life_years", "dividends", "net_debt"}
        priority_metrics = set()
        checks = {}
        if template in {"ordinary", "cyclical", "bank", "insurance"}:
            checks = screen({**packet, "facts": valid}, self.today, self.settings)[
                "checks"
            ]
            if (
                checks.get("adjusted_pe_ttm") is None
                or checks.get("earnings_proxy") is None
            ):
                priority_metrics.add("adjusted_net_income_parent")
            if checks.get("pe_ttm") is None:
                priority_metrics.add("net_income_parent")
            if (
                template in {"ordinary", "cyclical"}
                and checks.get("debt_assets") is None
            ):
                priority_metrics.update({"total_assets", "total_liabilities"})
            if checks.get("earnings_proxy") is None and (
                packet["company"].get("valuation_path") == "pb"
                or template in {"bank", "insurance"}
            ):
                priority_metrics.update({"average_parent_equity", "parent_equity"})
        screen_first = self.settings.get("screen_first_extraction", False)
        if screen_first and priority_metrics:
            if "average_parent_equity" in priority_metrics:
                priority_metrics.add("adjusted_roe")
            # Resolve the mechanical screen before paying for seven-year
            # cashflow/ROIC interpretation. Later calls retain the full contract.
            required = set(priority_metrics)
        keys = (
            "经营活动",
            "现金流量",
            "利润表",
            "资产负债",
            "净资产收益",
            "股份支付",
            "income",
            "cash flow",
            "balance sheet",
        )
        hints = {
            "revenue": ("营业收入", "营业总收入", "revenue"),
            "basic_eps": ("基本每股收益", "basic earnings per share"),
            "interest_paid": ("支付的利息", "利息支出支付的现金", "interest paid"),
            "minority_dividends_paid": (
                "少数股东的股利",
                "dividends paid to non-controlling",
            ),
            "dividends_and_interest_paid": ("分配股利、利润或偿付利息",),
            "lease_cash_payments": (
                "支付租赁",
                "租赁付款",
                "支付的租赁",
                "lease payments",
            ),
            "interest_not_in_cfo": ("支付的利息", "interest paid"),
            "lease_principal": ("偿还租赁负债本金", "租赁负债本金"),
            "lease_payments": ("支付租赁", "租赁付款", "lease payments"),
            "share_based_compensation": ("股份支付", "share-based compensation"),
            "maintenance_acquisitions": ("取得子公司", "acquisition"),
            "acquisitions_cash": ("取得子公司", "acquisition"),
            "depreciation_amortization": ("折旧", "摊销", "depreciation"),
            "operating_working_capital": ("营运资本", "working capital"),
            "investment_yield": ("投资收益率", "investment yield"),
            "new_business_value": ("新业务价值", "new business value"),
            "combined_ratio": ("综合成本率", "combined ratio"),
            "dpu": ("每份分配", "每份收益", "distribution per unit"),
            "interest_coverage": ("利息保障倍数", "interest coverage"),
            "leverage": ("资产负债率", "杠杆率", "leverage"),
            "financing_cost": ("融资成本", "financing cost"),
            "occupancy_rate": ("出租率", "occupancy"),
            "nav": ("基金净资产", "net asset value"),
            "asset_life_years": ("剩余经营", "剩余期限", "remaining life"),
            "adjusted_roe": (
                "扣除非经常性损益后的加权平均净资产收益率",
                "扣除非经常性损益后的净资产收益率",
            ),
            "total_assets": ("资产总计", "资产总额", "总资产", "total assets"),
            "total_liabilities": (
                "负债合计",
                "负债总额",
                "总负债",
                "total liabilities",
            ),
            "parent_equity": ("归属于母公司所有者权益", "归属于上市公司股东的净资产"),
            "average_parent_equity": ("平均净资产", "average equity"),
            "net_income_parent": (
                "归属于母公司所有者的净利润",
                "归属于上市公司股东的净利润",
            ),
            "adjusted_net_income_parent": (
                "扣除非经常性损益",
                "扣非",
                "adjusted net income",
            ),
            "net_interest_margin": ("净息差", "net interest margin"),
            "credit_cost": ("信用成本", "信贷成本", "credit cost"),
            "capital_adequacy": ("资本充足率", "capital adequacy"),
            "nonperforming_loan_ratio": ("不良贷款率", "non-performing loan"),
            "provision_coverage": ("拨备覆盖率", "provision coverage"),
            "solvency_ratio": ("偿付能力", "solvency"),
            "payout_ratio": ("分红比例", "派息比率", "payout ratio"),
            "affo": ("调整后营运现金", "affo"),
            "distributable_amount": ("可供分配金额", "distributable amount"),
            "operating_profit": ("营业利润", "operating profit"),
            "operating_cash_flow": (
                "经营活动产生的现金流量净额",
                "operating activities",
            ),
            "capital_expenditures": ("购建固定资产", "purchase of property"),
            "dividends": ("分配股利", "利润分配", "dividends"),
            "net_buybacks": ("回购", "股份变动", "股本", "repurchase"),
            "minority_equity": ("少数股东权益", "non-controlling"),
            "interest_expense": ("利息费用", "interest expense"),
            "income_tax_expense": ("所得税费用", "income tax"),
            "profit_before_tax": ("利润总额", "before tax"),
            "short_term_borrowings": ("短期借款",),
            "long_term_borrowings": ("长期借款",),
            "current_portion_noncurrent_debt": ("一年内到期的非流动负债",),
            "lease_liabilities": ("租赁负债",),
            "restricted_cash": ("受限", "使用受到限制"),
            "cash_and_cash_equivalents": ("现金及现金等价物",),
            "bonds_payable": ("应付债券",),
            "sbc_cost": ("股份支付",),
            "net_reinvestment": ("折旧", "摊销", "营运资本"),
        }
        original_ids = {
            s["id"]
            for s in packet["sources"]
            if s.get("kind") in {"annual", "interim"}
            or s.get("page_status") == "complete_text"
        }
        covered = {
            (f["metric"], int(f["period_end"][:4]))
            for f in valid
            if f.get("source_id") in original_ids and f.get("period_type") == "annual"
        }
        end = self.today.year - (1 if self.today.month >= 5 else 2)
        width = 11 if packet["company"]["template"] == "cyclical" else 8
        if packet.get("research_mode") == "model_led":
            derived_only = {
                "nopat",
                "invested_capital",
                "net_debt",
                "average_parent_equity",
                "average_invested_capital",
            }
            required -= derived_only
            requested = packet.get("research_evidence_requests", [])
            if requested:
                verified_cells = {
                    (f["metric"], f["period_end"])
                    for f in valid
                    if f.get("source_id") in original_ids
                }
                requested = [
                    r
                    for r in requested
                    if (r["metric"], r["period_end"]) not in verified_cells
                ]
                from .research_contracts import EVIDENCE_METRICS

                required = (
                    {r["metric"] for r in requested} & EVIDENCE_METRICS
                ) - derived_only
                if not required:
                    return valid, errors
            width = 3
            # Model-directed missing cells replace mechanical-screen priorities.
            # A dense income-summary page must not starve a requested capex row.
            priority_metrics = set()
        if screen_first and priority_metrics:
            width = 5 if "average_parent_equity" in priority_metrics else 3
        missing_cells = {
            metric: [
                year
                for year in range(end - width + 1, end + 1)
                if (metric, year) not in covered
            ]
            for metric in sorted(required)
        }
        if packet.get("research_mode") == "model_led" and packet.get(
            "research_evidence_requests"
        ):
            missing_cells = {
                metric: sorted(
                    {
                        int(r["period_end"][:4])
                        for r in requested
                        if r["metric"] == metric
                    }
                )
                for metric in required
            }
        from .retrieval import RETRIEVAL_VERSION, select_requested_pages

        def request_signature(source):
            cells = {
                metric: [
                    y
                    for y in years
                    if y
                    in {
                        int(source["period_end"][:4]),
                        int(source["period_end"][:4]) - 1,
                    }
                ]
                for metric, years in missing_cells.items()
            }
            if packet.get("research_mode") == "model_led":
                return fingerprint(
                    {
                        "retrieval": RETRIEVAL_VERSION,
                        "missing_cells": cells,
                        "requested_pairs": sorted(
                            (r["metric"], r["period_end"]) for r in requested
                        ),
                    }
                )
            return fingerprint(cells)

        page_request_signatures = {
            source["id"]: request_signature(source) for source in docs
        }
        attempted_requests = {
            (*page, event["page_request_signatures"][page[0]])
            for event in prior_extractions
            for page in event.get("pages", [])
            if page[0] in event.get("page_request_signatures", {})
        }
        docs = [
            source
            for source in docs
            if (
                source["id"],
                source["hash"],
                PROMPT_VERSION,
                page_request_signatures[source["id"]],
            )
            not in attempted_requests
        ]
        if not docs:
            return valid, errors

        def page_score(source):
            text = source.get("text", "").lower()
            compact = re.sub(r"\s+", "", text)

            def mentions(term):
                return term in text or re.sub(r"\s+", "", term) in compact

            year = int(source["period_end"][:4])
            gap_score = sum(
                2
                for metric, terms in hints.items()
                if any(y in missing_cells.get(metric, []) for y in (year, year - 1))
                and any(mentions(term) for term in terms)
            )
            priority_score = sum(
                100
                for metric in priority_metrics
                if any(mentions(term) for term in hints.get(metric, ()))
            )
            if (
                screen_first
                and priority_score
                and checks.get("adjusted_pe_ttm") is None
                and source.get("kind") == "interim"
                and year >= self.today.year - 1
            ):
                priority_score += 1000
            parent_heading = re.search(r"母公司(?:资产负债表|利润表|现金流量表)", text)
            # A page can end a consolidated table, then begin a parent table.
            # Keep the preceding numeric rows eligible; this is retrieval only,
            # not certification of their consolidation/unit basis.
            preceding_rows = text[: parent_heading.start()] if parent_heading else ""
            numeric_continuation = any(
                any(term in preceding_rows for term in terms)
                for metric, terms in hints.items()
                if metric in required
            ) and bool(re.search(r"\d[\d,]*\.\d{2}", preceding_rows))
            return (
                bool(parent_heading)
                and not numeric_continuation
                and not bool(re.search(r"合并(?:资产负债表|利润表|现金流量表)", text)),
                -priority_score - gap_score - sum(k in text for k in keys),
                int(source["page"]),
                source["id"],
            )

        # One original filing per request: cross-report unit/currency context
        # must never be offered as evidence for another filing's table.
        if packet.get("research_mode") == "model_led":
            from .retrieval import matching_metric_rows

            docs = [s for s in docs if matching_metric_rows(s, required, hints)]
            requests = requested
            if requests:
                from .retrieval import requested_row_cells

                docs = [s for s in docs if requested_row_cells(s, requests, hints)]
            if not docs:
                return valid, errors + [
                    issue(
                        "requested_financial_rows_not_retrieved",
                        stage="extract",
                        message="Repair row retrieval before another model extraction",
                    )
                ]
        if screen_first and priority_metrics:
            needed_years = {year for years in missing_cells.values() for year in years}

            def relevant_to_screen(source):
                year = int(source["period_end"][:4])
                interim = (
                    source.get("kind") == "interim"
                    or source.get("document_kind") == "interim"
                )
                if not interim:
                    return bool(needed_years.intersection(range(year - 2, year + 1)))
                if year < self.today.year - 1:
                    return False
                dates = {source["period_end"], str(year - 1) + source["period_end"][4:]}
                return any(
                    not any(
                        fact.get("metric") == metric
                        and fact.get("period_type") == "interim"
                        and fact.get("period_end") == period
                        and fact.get("is_cumulative") is True
                        for fact in valid
                    )
                    for metric in priority_metrics
                    & {"adjusted_net_income_parent", "net_income_parent"}
                    for period in dates
                )

            docs = [source for source in docs if relevant_to_screen(source)]
            if not docs:
                return valid, errors + [
                    issue("screen_requested_evidence_exhausted", stage="extract")
                ]
        filings = {}
        for source in docs:
            identity = tuple(
                source.get(k) for k in ("origin_id", "published_at", "issuer_id")
            )
            filings.setdefault(identity, []).append(source)
        for pages in filings.values():
            pages.sort(key=page_score)
        # An interim bonus chooses the first useful request, not every request.
        # Persisted attempts survive restarts; a rejected interim batch must
        # leave room for the still-unvisited annual evidence required by TTM.
        source_by_id = {s["id"]: s for s in original_docs}
        filing_attempts = {}
        for event in prior_extractions:
            attempted_filings = set()
            for page in event.get("pages", []):
                source = source_by_id.get(page[0])
                if source and page[1] == source["hash"]:
                    attempted_filings.add(
                        tuple(
                            source.get(k)
                            for k in ("origin_id", "published_at", "issuer_id")
                        )
                    )
            for identity in attempted_filings:
                filing_attempts[identity] = filing_attempts.get(identity, 0) + 1
        selected_identity = min(
            filings,
            key=lambda identity: (
                filing_attempts.get(identity, 0),
                page_score(filings[identity][0])[:2],
                -int(filings[identity][0]["period_end"][:4]),
                page_score(filings[identity][0])[2:],
            ),
        )
        selected_filing = filings[selected_identity]
        if packet.get("research_mode") == "model_led":
            requests = requested or [
                {"metric": metric, "period_end": f"{year}-12-31"}
                for metric, years in missing_cells.items()
                for year in years
            ]
            selected_docs = select_requested_pages(
                selected_filing,
                requests,
                hints,
                original_docs,
                self._limit("extraction_pages_per_day", 8),
            )
        else:
            selected_docs = selected_filing[
                : self._limit("extraction_pages_per_day", 8)
            ]
        # Context is original text with its own source identity. Retrieval does
        # not itself establish that a neighboring unit governs the target row.
        context_map, context_docs = {}, {}
        for source in selected_docs:
            related = [
                s
                for s in original_docs
                if s.get("origin_id") == source.get("origin_id")
                and s.get("published_at") == source.get("published_at")
                and s.get("issuer_id") == source.get("issuer_id")
            ]
            unit_pages = [
                s
                for s in related
                if abs(int(s["page"]) - int(source["page"])) <= 1
                and re.search(r"单位\s*[:：]|units?\s*[:：]", s["text"], re.IGNORECASE)
            ]
            currency_pages = [
                s for s in related if find_currency_definitions(s["text"])
            ]
            currency_pages = sorted(currency_pages, key=lambda s: int(s["page"]))[:1]
            from .currency_definitions import find_statement_currency_declarations
            from .retrieval import currency_context_sources

            statement_context = currency_context_sources(source, original_docs)
            currency_pages = list(
                {
                    s["id"]: s
                    for s in [
                        *currency_pages,
                        *(
                            s
                            for s in statement_context
                            if find_statement_currency_declarations(s["text"])
                        ),
                    ]
                }.values()
            )
            context_map[source["id"]] = {
                "unit_source_ids": [s["id"] for s in unit_pages],
                "currency_source_ids": [s["id"] for s in currency_pages],
            }
            context_docs.update({s["id"]: s for s in [*unit_pages, *currency_pages]})
            context_docs.update({s["id"]: s for s in statement_context})
        batch_sources = list(
            {s["id"]: s for s in [*selected_docs, *context_docs.values()]}.values()
        )
        if packet.get("research_mode") == "model_led" and requests:
            from .retrieval import requested_row_cells

            # A supplied statement-header page can also contain a requested
            # numeric row (for example CFO immediately before the Capex page).
            # Explicitly promote only matching pages, within the same page cap.
            selected_ids = {s["id"] for s in selected_docs}
            for source in batch_sources:
                if len(selected_docs) >= self._limit("extraction_pages_per_day", 8):
                    break
                if source["id"] not in selected_ids and requested_row_cells(
                    source, requests, hints
                ):
                    selected_docs.append(source)
                    selected_ids.add(source["id"])
        filing = selected_docs[0]
        filing_year = int(filing["period_end"][:4])
        is_interim = (
            filing.get("kind") == "interim" or filing.get("document_kind") == "interim"
        )
        balance_metrics = {
            "total_assets",
            "total_liabilities",
            "parent_equity",
            "minority_equity",
            "net_debt",
            "cash_and_cash_equivalents",
            "restricted_cash",
            "short_term_borrowings",
            "current_portion_noncurrent_debt",
            "long_term_borrowings",
            "bonds_payable",
            "lease_liabilities",
            "operating_working_capital",
        }
        requested_cells = {
            metric: [
                year
                for year in years
                if (
                    year == filing_year - 1 and metric in balance_metrics
                    if is_interim
                    else filing_year - 2 <= year <= filing_year
                )
            ]
            for metric, years in missing_cells.items()
        }
        exact_requests = []
        if packet.get("research_mode") == "model_led" and requests:
            reachable = set().union(
                *(requested_row_cells(s, requests, hints) for s in selected_docs)
            )
            exact_requests = [
                r for r in requests if (r["metric"], r["period_end"]) in reachable
            ]
            requested_cells = {
                metric: sorted({
                    int(r["period_end"][:4])
                    for r in exact_requests if r["metric"] == metric
                })
                for metric in required
            }
        if self.acceptance.enabled:
            if not self.acceptance.reserve_batch(packet["company"]["id"]):
                return valid, errors + [
                    issue("acceptance_extraction_limit", stage="extract")
                ]
        else:
            day_key = f"extract:{packet['company']['id']}"
            with self.budget_store.transaction() as db:
                daily_batches = db.execute(
                    "SELECT COUNT(*) FROM reservations WHERE day=? AND kind IN "
                    "('extraction_issuers','extraction_recoveries','extraction_continuations') "
                    "AND key IN (?,?,?)",
                    (
                        self.budget_day.isoformat(),
                        day_key,
                        f"{day_key}:1",
                        f"{day_key}:2",
                    ),
                ).fetchone()[0]
            if daily_batches >= 3:
                return valid, errors
            if not self.budget_store.reserve(
                self.budget_day.isoformat(),
                "extraction_issuers",
                self._acquisition_limit(),
                key=day_key,
            ):
                with self.budget_store.transaction() as db:
                    prepaid = db.execute(
                        "SELECT 1 FROM reservations WHERE day=? AND kind='extraction_issuers' AND key=?",
                        (self.budget_day.isoformat(), day_key),
                    ).fetchone()
                if not prepaid:
                    return valid, errors
                selected = selected_docs
                recovered = any(
                    (s["id"], s["hash"]) in recoverable_pages for s in selected
                ) and self.budget_store.reserve(
                    self.budget_day.isoformat(),
                    "extraction_recoveries",
                    self._acquisition_limit(),
                    key=day_key,
                )
                if not recovered:
                    latest = prior_extractions[-1] if prior_extractions else {}
                    changed_prompt = bool(latest) and all(
                        page[2] != PROMPT_VERSION for page in latest.get("pages", [])
                    )
                    if not latest.get("facts") and not changed_prompt:
                        return valid, errors
                    usage = self.budget_store.budgets(self.budget_day.isoformat())
                    if (
                        usage.get("model_calls", 0)
                        >= self._limit("daily_model_call_limit", 80) - 3
                    ):
                        return valid, errors
                    continued = any(
                        self.budget_store.reserve(
                            self.budget_day.isoformat(),
                            "extraction_continuations",
                            self._acquisition_limit() * 2,
                            key=f"{day_key}:{number}",
                        )
                        for number in (1, 2)
                    )
                    if not continued:
                        return valid, errors
        chunk = {
            **packet,
            "facts": [],
            "sources": batch_sources,
            "page_reviews": [
                review
                for review in packet.get("page_reviews", [])
                if review.get("document_sha256")
                in {
                    source.get("document_sha256")
                    for source in selected_docs
                    if source.get("document_sha256")
                }
            ],
            "requested_metrics": sorted(required),
            "requested_missing_cells": requested_cells,
            "priority_metrics": sorted(priority_metrics),
            "extraction_target_source_ids": [s["id"] for s in selected_docs],
            "evidence_context_map": context_map,
            "requested_interim_periods": sorted(
                {
                    s["period_end"]
                    for s in selected_docs
                    if s.get("kind") == "interim" or s.get("document_kind") == "interim"
                }
            ),
        }
        if exact_requests:
            chunk["research_evidence_requests"] = exact_requests
            chunk["requested_interim_periods"] = sorted({
                r["period_end"] for r in exact_requests
                if r["period_end"][5:] != "12-31"
            })
        selected_pages = [[s["id"], s["hash"], PROMPT_VERSION] for s in selected_docs]
        try:
            answer = self._call("extract", chunk)
        except ResearchBlocked as exc:
            self._event(
                packet["company"]["id"],
                "extraction_blocked",
                {
                    "pages": selected_pages,
                    "issues": exc.errors,
                    "versions": versions,
                    "budget_state": self._model_budget_state(),
                },
            )
            self.store.update(
                packet["company"]["id"],
                "evidence",
                errors=errors + exc.errors,
                packet=packet,
            )
            self._packets[packet["company"]["id"]] = copy.deepcopy(packet)
            raise
        extracted = answer.get("facts", [])
        if not isinstance(extracted, list):
            raise ResearchBlocked(
                [issue("extraction_contract_invalid", stage="extract")]
            )
        selected = {s["id"] for s in selected_docs}
        proposed = extracted
        scoped, rejected = [], []
        requested_pairs = (
            {
                (r["metric"], r["period_end"])
                for r in packet.get("research_evidence_requests", [])
            }
            if packet.get("research_mode") == "model_led"
            else set()
        )
        if len(proposed) > 24:
            rejected.append(issue("extraction_batch_limit_exceeded", stage="extract"))
        for fact in proposed[:24]:
            if (
                not isinstance(fact, dict)
                or not isinstance(fact.get("source_id"), str)
                or not isinstance(fact.get("metric"), str)
                or fact.get("source_id") not in selected
                or fact.get("metric") not in required
                or (
                    requested_pairs
                    and (fact.get("metric"), fact.get("period_end"))
                    not in requested_pairs
                )
            ):
                rejected.append(
                    issue(
                        "extraction_outside_requested_evidence",
                        str(fact.get("id", "")) if isinstance(fact, dict) else "",
                        stage="extract",
                    )
                )
            else:
                scoped.append(fact)
        from .extraction_repair import repair_local_currency_scope

        scoped, scope_repairs = repair_local_currency_scope(chunk, scoped)
        extracted, conversion_errors = prepare_extracted_facts(
            chunk, scoped, answer.get("issues", []), self.today
        )
        rejected += conversion_errors
        # Reject malformed proposals before mutating evidence. Individually
        # valid opposing disclosures stay in the packet for conflict blocking.
        existing_ids = {f.get("id") for f in packet.get("facts", [])}
        extracted = list(
            {f["id"]: f for f in extracted if f["id"] not in existing_ids}.values()
        )
        packet["facts"] = [*packet.get("facts", []), *extracted]
        valid, errors = validate_packet(packet, self.today)
        errors += rejected
        if any(
            isinstance(item, dict) and item.get("severity") == "blocking"
            for item in (answer.get("issues") or [])
        ):
            errors.append(issue("extraction_reported_issues", stage="extract"))
        # Retain partial progress before any later readiness, peer or review
        # failure. A subsequent collection must not discard extracted facts.
        self.store.update(
            packet["company"]["id"], "evidence", errors=errors, packet=packet
        )
        self._packets[packet["company"]["id"]] = copy.deepcopy(packet)
        self._event(
            packet["company"]["id"],
            "extraction",
            {
                "facts": extracted,
                "rejected_proposals": rejected,
                "local_currency_scope_repairs": scope_repairs,
                "issues": answer.get("issues", []),
                "page_request_signatures": {
                    s["id"]: page_request_signatures[s["id"]] for s in selected_docs
                },
                "pages": [[s["id"], s["hash"], PROMPT_VERSION] for s in selected_docs],
            },
        )
        definitive_rejection = (
            template == "ordinary"
            and screen({**packet, "facts": valid}, self.today, self.settings)["status"]
            == "fail"
        )
        if extracted and not rejected and not definitive_rejection:
            return self._extract(packet, valid, errors)
        return valid, errors

    def _peer_packets(self, packet):
        peers = packet.get("peers", [])
        if not peers:
            answer = self._call("peers", packet)
            peers = answer.get("peers", [])
            if answer.get("issues"):
                raise ResearchBlocked(
                    [issue("peer_selection_incomplete", stage="peers")]
                )
        if (
            not isinstance(peers, list)
            or len(peers) != 4
            or any(not isinstance(p, dict) for p in peers)
        ):
            raise ResearchBlocked([issue("four_peers_required", stage="peers")])
        ids = {p.get("id") for p in peers}
        if (
            len(ids) != 4
            or packet["company"]["id"] in ids
            or sum(p.get("role") == "direct" for p in peers) != 2
            or sum(p.get("role") == "economic_model" for p in peers) != 2
        ):
            raise ResearchBlocked([issue("peer_composition_invalid", stage="peers")])
        known = {
            row["id"]: row["company"]
            for row in self.store.companies(include_packet=False)
        }
        known.update(
            {c["id"]: c for c in packet.get("peer_candidates", []) if c.get("id")}
        )
        packets, errors = [], []
        for peer in peers:
            if peer["id"] not in known:
                errors.append(
                    issue("peer_identity_not_discovered", peer["id"], stage="peers")
                )
                continue
            company = dict(known[peer["id"]])
            if peer["id"] not in {
                r["id"] for r in self.store.companies(include_packet=False)
            }:
                company["peer_only"] = True
            source_refs = {s["id"] for s in packet["sources"]}
            if (
                not peer.get("reason")
                or not peer.get("source_refs")
                or not set(peer["source_refs"]).issubset(source_refs)
            ):
                errors.append(
                    issue("peer_selection_unevidenced", peer["id"], stage="peers")
                )
                continue
            self.store.register(company)
            if self.acceptance.enabled:
                self._acceptance_track_dependency(peer["id"])
            other = self._collect(company)
            from .extraction_audit import quarantine_legacy_proposals

            quarantine_legacy_proposals(self, other)
            from .cover_workflow import resolve_covers

            resolve_covers(self, other)
            accepted, problems = validate_packet(other, self.today)
            other["facts"] = accepted
            if readiness(other, self.today):
                try:
                    accepted, problems = self._extract(other, accepted, problems)
                    other["facts"] = accepted
                except ResearchBlocked as exc:
                    problems += exc.errors
                self._packets[company["id"]] = copy.deepcopy(other)
                self.store.update(peer["id"], "evidence", errors=problems, packet=other)
            problems += readiness(other, self.today)
            quotes = [f for f in accepted if f["metric"] == "market_cap"]
            if not quotes or (
                self.today - date.fromisoformat(max(f["period_end"] for f in quotes))
            ).days > self.settings.get("max_quote_age_days", 7):
                problems.append(
                    issue("peer_price_stale_or_missing", peer["id"], stage="peers")
                )
            if not problems:
                try:
                    self._prepare_cashflow_bridge(other)
                except ResearchBlocked as exc:
                    problems += exc.errors
            metrics, calculation_errors = calculate(other, settings=self.settings)
            problems += calculation_errors
            if problems:
                errors.append(
                    issue("peer_evidence_incomplete", peer["id"], stage="peers")
                )
                self.store.update(
                    peer["id"], "evidence_blocked", errors=problems, packet=other
                )
                continue
            packets.append({**other, "peers": [], "calculations": metrics})
        if errors:
            raise ResearchBlocked(errors)
        packet["peers"], packet["peer_packets"] = peers, packets

    def _prepare_cashflow_bridge(self, packet):
        # Source-supplied judgments cannot carry their own approval. The cached
        # model boundary is keyed to the frozen evidence and prompt version.
        packet.pop("cashflow_bridge", None)
        if packet["company"]["template"] not in {"ordinary", "cyclical"}:
            return
        # Do not spend two reasoning calls on inconsistent base arithmetic.
        _, preflight = calculate(packet, settings=self.settings)
        preflight = [
            e for e in preflight if not e["code"].startswith("cashflow_bridge_")
        ]
        if preflight:
            raise ResearchBlocked(preflight)
        missing = missing_requirements(packet, self.settings)
        if not missing:
            return
        proposal = self._call("bridge", packet, {"required_cells": missing})
        errors = validate_proposal(packet, proposal, self.settings)
        self._event(
            packet["company"]["id"],
            "cashflow_bridge_proposal",
            {"proposal": proposal, "errors": errors},
        )
        if errors:
            raise ResearchBlocked(errors)
        review = self._call(
            "bridge_review",
            {**packet, "cashflow_bridge": {"proposal": proposal}},
            {"proposal": proposal},
        )
        errors = validate_bridge_review(proposal, review)
        self._event(
            packet["company"]["id"],
            "cashflow_bridge_review",
            {"review": review, "errors": errors},
        )
        if errors:
            raise ResearchBlocked(errors)
        packet["cashflow_bridge"] = {"proposal": proposal, "review": review}

    def _process(self, row):
        company, issuer = row["company"], row["id"]
        acquisition_errors = []
        usage_start = len(self._usage.get(issuer, []))
        trace = {
            "issuer_id": issuer,
            "prompt_version": PROMPT_VERSION,
            "rules_version": VERSION,
            "parser_version": CONTRACT_VERSION,
            "model": self.settings.get("llm", {}).get(
                "model", self.config.get("llm", {}).get("model", "deepseek-chat")
            ),
            "parameters": {
                "temperature": self.settings.get("llm", {}).get("temperature", 0),
                "max_tokens": self.settings.get("llm", {}).get("max_tokens", 8192),
            },
            "analysis_called": False,
            "prerequisite_ready": False,
            "published": False,
            "candidate": None,
            "reviewed": False,
        }
        try:
            packet = self._collect(company)
            acquisition_errors = packet.get("acquisition_errors", [])
            # Accumulated extraction survives restarts; don't trust stale output
            # if its referenced original pages have changed or disappeared.
            if row.get("packet"):
                current_sources = {
                    s["id"]: s.get("hash") for s in packet.get("sources", [])
                }
                old_sources = {
                    s["id"]: s.get("hash") for s in row["packet"].get("sources", [])
                }
                existing = {f.get("id") for f in packet.get("facts", [])}
                packet["facts"] = [
                    *packet.get("facts", []),
                    *(
                        f
                        for f in row["packet"].get("facts", [])
                        if f.get("id") not in existing
                        and current_sources.get(f.get("source_id"))
                        == old_sources.get(f.get("source_id"))
                        and f.get("source_id") in current_sources
                    ),
                ]
            from .extraction_audit import quarantine_legacy_proposals

            quarantine_legacy_proposals(self, packet)
            current = self.store.current(issuer)
            if current and current["status"] == "published":
                from .cover_workflow import resolve_covers

                resolve_covers(self, packet)
            accepted, errors = validate_packet(packet, self.today)
            validated = {**packet, "facts": accepted}
            trace["input_hash"] = evidence_hash(packet)
            if current and current["status"] == "published":
                if (
                    not errors
                    and not self.settings.get("evaluation_run_id")
                    and current.get("versions") == self._versions()
                    and current.get("fundamental_hash")
                    == evidence_hash(packet, exclude_prices=True)
                    and self._refresh_prices(packet, validated, current)
                ):
                    return
                self.store.revoke(
                    issuer,
                    "Input evidence changed or failed verification; prior conclusion withdrawn pending revalidation",
                )
            result = screen(validated, self.today, self.settings)
            self._event(issuer, "screen", result)
            if result["status"] == "fail":
                self.store.update(
                    issuer,
                    "screened",
                    errors=errors + result["errors"] + acquisition_errors,
                    screen="fail",
                    packet=packet,
                )
                trace["candidate"] = False
                return
            from .cover_workflow import resolve_covers

            if not current or current["status"] != "published":
                resolve_covers(self, packet)
            accepted, errors = validate_packet(packet, self.today)
            validated = {**packet, "facts": accepted}
            missing = readiness(validated, self.today)
            if result["status"] == "unknown" or missing:
                accepted, errors = self._extract(packet, accepted, errors)
                validated = {**packet, "facts": accepted}
                result = screen(validated, self.today, self.settings)
                missing = readiness(validated, self.today)
            self.store.update(
                issuer,
                "evidence",
                errors=errors + missing + result["errors"] + acquisition_errors,
                screen=result["status"],
                packet=packet,
            )
            if result["status"] not in {"pass", "cycle_exception"}:
                raise ResearchBlocked(
                    result["errors"] or [issue("screen_not_passed", stage="screen")]
                )
            if missing or errors:
                raise ResearchBlocked(errors + missing)
            research_reserved = (
                self.acceptance.reserve_operation(
                    self.budget_day.isoformat(),
                    "research",
                    issuer,
                    self.acceptance.state()["config"]["target_limit"],
                )
                if self.acceptance.enabled
                else self.budget_store.reserve(
                    self.budget_day.isoformat(),
                    "research",
                    self._limit("daily_research_limit", 20),
                    key=f"{self.settings.get('evaluation_run_id', '')}:{issuer}",
                )
            )
            if not research_reserved:
                raise ResearchBlocked(
                    [issue("daily_research_budget_exhausted", stage="analysis")]
                )
            self._prepare_cashflow_bridge(validated)
            metrics, calc_errors = calculate(validated, settings=self.settings)
            if calc_errors:
                raise ResearchBlocked(calc_errors)
            self._peer_packets(validated)
            validated["calculations"] = metrics
            trace["prerequisite_ready"] = True
            trace["input_hash"] = evidence_hash(packet)
            attempt_hash = fingerprint(
                {
                    "evidence": evidence_hash(validated),
                    "prompt": PROMPT_VERSION,
                    "rules": VERSION,
                    "model": self._versions(),
                    "evaluation_run_id": self.settings.get("evaluation_run_id"),
                }
            )
            if row.get("attempt_hash") == attempt_hash:
                raise ResearchBlocked(
                    [issue("unchanged_research_already_attempted", stage="analysis")]
                )
            self.store.update(
                issuer, "analyzing", packet=packet, attempt_hash=attempt_hash
            )
            blind = self._call("review", validated)
            problems = validate_review(blind, validated)
            if problems:
                trace["reviewed"] = True
                trace["verification_blocked"] = True
                raise ResearchBlocked(problems)
            trace["analysis_called"] = True
            analysis = self._call("analyze", validated)
            problems, metrics, forecasts, breakers = self._validate_draft(
                analysis, validated
            )
            audit = None
            if not problems:
                audit = self._call(
                    "crosscheck",
                    validated,
                    {
                        "analysis": analysis,
                        "blind_review": blind,
                        "calculations": metrics,
                        "scenarios": forecasts,
                    },
                )
                problems += validate_review(audit, validated)
                trace["reviewed"] = True
            if problems:
                # Refresh only when that issuer still has a daily acquisition
                # reservation available; never bypass the fifty-issuer ceiling.
                trace["correction_attempted"] = True
                self._event(
                    issuer,
                    "correction_findings",
                    {"issues": problems, "analysis": analysis, "review": audit},
                )
                # No new raw fact is invented; the frozen evidence can support
                # correction of a misread source, otherwise preserve the blocker.
                analysis = self._call(
                    "repair",
                    validated,
                    {
                        "previous_analysis": analysis,
                        "findings": problems,
                        "blind_review": blind,
                        "crosscheck": audit,
                    },
                )
                problems, metrics, forecasts, breakers = self._validate_draft(
                    analysis, validated
                )
                if not problems:
                    audit = self._call(
                        "crosscheck",
                        validated,
                        {
                            "analysis": analysis,
                            "blind_review": blind,
                            "calculations": metrics,
                            "scenarios": forecasts,
                            "correction": True,
                        },
                    )
                    problems += validate_review(audit, validated)
                    trace["reviewed"] = True
                trace["correction_succeeded"] = not problems
            if problems:
                trace["verification_blocked"] = True
                raise ResearchBlocked(problems)
            trace["adjustments"] = analysis.get("adjustments", [])
            eligibility = qualification(metrics, forecasts, breakers)
            candidate = eligibility["candidate"]
            payload = {
                "as_of": self.today.isoformat(),
                "input_hash": evidence_hash(packet),
                "fundamental_hash": evidence_hash(packet, exclude_prices=True),
                "prompt_version": PROMPT_VERSION,
                "versions": self._versions(),
                "rules_version": VERSION,
                "candidate": candidate,
                "qualification": eligibility,
                "metrics": metrics,
                "conclusions": analysis["conclusions"],
                "adjustments": analysis.get("adjustments", []),
                "cashflow_bridge": validated.get("cashflow_bridge"),
                "page_reviews": validated.get("page_reviews", []),
                "scenarios": forecasts,
                "peers": validated["peers"],
                "peer_comparison": self._compare_peers(validated, metrics),
                "peer_fundamental_hashes": {
                    p["company"]["id"]: evidence_hash(p, exclude_prices=True)
                    for p in validated["peer_packets"]
                },
                "breakers": analysis["breakers"],
                "breaker_states": breakers,
                "review": {
                    "blind": blind,
                    "crosscheck": audit,
                    "independence": "separate_contexts_not_independent_error_probabilities",
                },
                "issues": [],
                "source_refs": [
                    {k: s.get(k) for k in ("id", "url", "page", "hash", "origin_id")}
                    for s in packet["sources"]
                ],
                "source_crosschecks": source_crosschecks(validated),
                "quote_date": max(
                    (f["period_end"] for f in accepted if f["metric"] == "market_cap"),
                    default="",
                ),
            }
            version = self.store.publish(issuer, payload)
            self.store.update(
                issuer,
                "published",
                errors=acquisition_errors,
                screen=result["status"],
                packet=packet,
            )
            self._check_breakers(validated, {**payload, "version": version})
            trace.update(
                candidate=candidate, published=True, verification_blocked=False
            )
        except ResearchBlocked as exc:
            self.store.revoke(
                issuer,
                "Research verification blocked: "
                + ",".join(e["code"] for e in exc.errors),
            )
            self.store.update(
                issuer,
                "blocked",
                errors=exc.errors + acquisition_errors,
                # Requests completed before this budget stop remain cached;
                # only this uncompleted research attempt may resume unchanged.
                attempt_hash=""
                if all(
                    e["code"].startswith("daily_") and "budget_exhausted" in e["code"]
                    for e in exc.errors
                )
                else None,
            )
            trace["blockers"] = exc.errors
            trace["error_code"] = ",".join(sorted({e["code"] for e in exc.errors}))
            trace["data_error_count"] = len(exc.errors)
            trace["surfaced_error_count"] = len(exc.errors)
        except Exception as exc:
            errors = [
                issue(
                    "unexpected_pipeline_error",
                    message=type(exc).__name__,
                    company=issuer,
                )
            ]
            self.store.revoke(
                issuer, "Unexpected pipeline error; publication cannot be trusted"
            )
            self.store.update(issuer, "blocked", errors=errors)
            self._event(
                issuer, "pipeline_error", {"exception_type": type(exc).__name__}
            )
            trace["blockers"] = errors
            trace["error_code"] = "unexpected_pipeline_error"
        finally:
            if isinstance(locals().get("packet"), dict):
                originals = [
                    source
                    for source in packet.get("sources", [])
                    if (source.get("document_kind") or source.get("kind"))
                    in {"annual", "interim"}
                ]
                self._event(
                    issuer,
                    "evidence_progress",
                    {
                        "updated_at": datetime.now(pytz.UTC).isoformat(),
                        "source_count": len(packet.get("sources", [])),
                        "original_page_count": len(originals),
                        "readable_original_page_count": sum(
                            s.get("page_status") == "complete_text"
                            or (
                                s.get("kind") in {"annual", "interim"}
                                and s.get("document_status") != "incomplete_text"
                            )
                            for s in originals
                        ),
                        "original_periods": sorted(
                            {s["period_end"] for s in originals if s.get("period_end")}
                        ),
                        "fact_count": len(packet.get("facts", [])),
                        "mechanically_checked_fact_count": len(
                            locals().get("accepted", [])
                        ),
                        "verification_limit": "Counts describe acquired evidence, not investment conclusions or independent accounting review",
                    },
                )
            usages = self._usage.get(issuer, [])[usage_start:]
            for name in ("input_tokens", "output_tokens"):
                values = [u.get(name) for u in usages]
                trace[name] = (
                    sum(values)
                    if all(type(v) is int and v >= 0 for v in values)
                    else None
                )
            self._event(issuer, "evaluation_trace", trace)

    def _validate_draft(self, analysis, packet):
        problems = validate_analysis(analysis, packet, set(packet["calculations"]))
        if problems:
            return problems, {}, [], []
        metrics, errors = calculate(
            packet, analysis.get("adjustments", []), self.settings
        )
        if not metrics:
            return errors, {}, [], []
        forecasts, growth_errors = scenarios(
            packet, metrics, analysis.get("scenarios", []), self.settings
        )
        breakers, breaker_errors = evaluate_breakers(
            packet, analysis.get("breakers", [])
        )
        return errors + growth_errors + breaker_errors, metrics, forecasts, breakers

    def _refresh_prices(self, raw_packet, packet, previous):
        """Reprice reviewed economics without another narrative/model pass."""
        eligibility = screen(packet, self.today, self.settings)
        if any(e["code"] == "quote_stale_or_missing" for e in eligibility["errors"]):
            self.store.revoke(packet["company"]["id"], "Quote expired")
            return False
        packet["peers"] = previous["peers"]
        try:
            self._peer_packets(packet)
        except ResearchBlocked:
            self.store.revoke(
                packet["company"]["id"], "Peer evidence or prices unavailable"
            )
            raise
        peer_hashes = {
            p["company"]["id"]: evidence_hash(p, exclude_prices=True)
            for p in packet["peer_packets"]
        }
        if peer_hashes != previous.get("peer_fundamental_hashes"):
            return False
        # Only reused when the complete fundamental hash and versions match.
        # Resolve validates every bridge reference against the current originals.
        packet["cashflow_bridge"] = previous.get("cashflow_bridge") or {}
        metrics, errors = calculate(
            packet, previous.get("adjustments", []), self.settings
        )
        forecasts, forecast_errors = (
            scenarios(packet, metrics, previous.get("scenarios", []), self.settings)
            if metrics
            else ([], errors)
        )
        states, breaker_errors = evaluate_breakers(packet, previous.get("breakers", []))
        if errors or forecast_errors or breaker_errors:
            self.store.revoke(packet["company"]["id"], "Repricing validation failed")
            raise ResearchBlocked(errors + forecast_errors + breaker_errors)
        payload = {
            k: copy.deepcopy(v)
            for k, v in previous.items()
            if k not in {"status", "version"}
        }
        payload.update(
            as_of=self.today.isoformat(),
            input_hash=evidence_hash(raw_packet),
            metrics=metrics,
            scenarios=forecasts,
            peer_comparison=self._compare_peers(packet, metrics),
            breaker_states=states,
            candidate=qualification(metrics, forecasts, states)["candidate"]
            and eligibility["status"] in {"pass", "cycle_exception"},
            qualification=qualification(metrics, forecasts, states),
            source_crosschecks=source_crosschecks(packet),
            quote_date=max(
                f["period_end"] for f in packet["facts"] if f["metric"] == "market_cap"
            ),
            review_scope="Previously independently reviewed fundamental interpretation; deterministic price refresh",
            interpretation_version=previous.get(
                "interpretation_version", previous["version"]
            ),
        )
        # Price-dependent prose is never carried forward as a fresh conclusion.
        for conclusion in payload["conclusions"]:
            if conclusion["key"] in {"implied_return", "peer_thesis"}:
                conclusion["reason"] = (
                    "价格及同行比较已由代码重算；业务解释沿用所标记的已核验版本。以本页当前指标和达标状态为准。"
                )
        version = self.store.publish(packet["company"]["id"], payload)
        self.store.update(
            packet["company"]["id"],
            "published",
            screen=eligibility["status"],
            packet=raw_packet,
            errors=raw_packet.get("acquisition_errors", []),
        )
        self._check_breakers(packet, {**payload, "version": version})
        self._event(
            packet["company"]["id"],
            "deterministic_repricing",
            {"version": version, "model_calls": 0},
        )
        return True

    def _compare_peers(self, packet, target_metrics=None):
        from statistics import median

        target_metrics = target_metrics or packet.get("calculations", {})
        target_id = packet["company"]["id"]

        # Fiscal endpoints and CAGR interval counts must match. Ratios can be
        # compared across currencies; absolute amounts cannot be ranked here.
        def endpoint(item):
            return max(
                (
                    f["period_end"]
                    for f in item["facts"]
                    if f["period_type"] == "annual"
                ),
                default=None,
            )

        rows = []
        for name in (
            "normalized_roe",
            "normalized_roic",
            "fcf_yield",
            "fcf_conversion",
            "reinvestment_rate",
            "shareholder_yield",
            "implied_ke_pb",
            "normalized_pe",
            "current_pb",
            "net_debt_to_market_cap",
            "revenue_cagr",
            "adjusted_net_income_parent_cagr",
            "raw_fcf_cagr",
            "equity_fcf_cagr",
        ):
            target = target_metrics.get(name, {})
            observations, gaps = [], []
            for peer in packet["peer_packets"]:
                metric = peer["calculations"].get(name, {})
                comparable = (
                    metric.get("value") is not None
                    and endpoint(peer) == endpoint(packet)
                    and (
                        not name.endswith("_cagr")
                        or metric.get("interval_years") == target.get("interval_years")
                    )
                    and (
                        not name.startswith("normalized_")
                        or (peer["company"]["template"] == "cyclical")
                        == (packet["company"]["template"] == "cyclical")
                    )
                )
                observations.append(
                    {
                        "issuer_id": peer["company"]["id"],
                        "value": metric.get("value"),
                        "fiscal_period_end": endpoint(peer),
                        "comparable": comparable,
                        "basis": "unadjusted_code_normalization_reviewed_with_target",
                    }
                )
                if not comparable:
                    gaps.append(peer["company"]["id"])
            values = [p["value"] for p in observations if p["comparable"]]
            ranked = len(values) == 4 and target.get("value") is not None
            lower_is_better = name in {
                "normalized_pe",
                "current_pb",
                "net_debt_to_market_cap",
            }
            rank = None
            if ranked:
                rank = 1 + sum(
                    v < target["value"] if lower_is_better else v > target["value"]
                    for v in values
                )
            rows.append(
                {
                    "metric": name,
                    "target_issuer": target_id,
                    "target_value": target.get("value"),
                    "peer_median": median(values) if len(values) == 4 else None,
                    "coverage": f"{len(values)}/4",
                    "ranked": ranked,
                    "target_rank_of_five": rank,
                    "rank_direction": "ascending" if lower_is_better else "descending",
                    "ties": "competition_rank",
                    "observations": observations,
                    "comparability_gaps": gaps,
                }
            )
        return rows

    def _check_breakers(self, packet, publication):
        states, errors = evaluate_breakers(packet, publication.get("breakers", []))
        for state in states:
            if self.store.breaker(
                packet["company"]["id"],
                state["id"],
                state["state"],
                publication["version"],
            ):
                self.store.enqueue(
                    "breaker",
                    {
                        "company_id": packet["company"]["id"],
                        "version": publication["version"],
                        **state,
                    },
                )
        if errors or any(s["state"] != "clear" for s in states):
            self.store.revoke(
                packet["company"]["id"],
                "Thesis breaker triggered or its observations are unavailable",
            )

    def _acceptance_track_dependency(self, issuer):
        if issuer in self._acceptance_dependency_before:
            return
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT packet FROM companies WHERE id=?", (issuer,)
            ).fetchone()
        self._acceptance_dependency_before[issuer] = (
            self._acceptance_evidence_hash(json.loads(row[0]))
            if row and row[0]
            else None
        )

    def _acceptance_dependency_progress(self):
        changed = []
        for issuer, before in self._acceptance_dependency_before.items():
            with self.store.transaction() as db:
                row = db.execute(
                    "SELECT packet FROM companies WHERE id=?", (issuer,)
                ).fetchone()
            after = (
                self._acceptance_evidence_hash(json.loads(row[0]))
                if row and row[0]
                else None
            )
            if before != after:
                changed.append(issuer)
        return changed

    @staticmethod
    def _acceptance_evidence_hash(packet):
        """Count facts and usable source changes, not refreshed cover receipts."""
        quote_ids = {
            s["id"] for s in packet.get("sources", []) if s.get("kind") == "quote"
        }
        return fingerprint(
            {
                "facts": [
                    f
                    for f in packet.get("facts", [])
                    if f.get("source_id") not in quote_ids
                ],
                "sources": [
                    {
                        k: s.get(k)
                        for k in (
                            "id",
                            "hash",
                            "origin_id",
                            "published_at",
                            "period_end",
                            "issuer_id",
                            "kind",
                            "page_status",
                            "document_status",
                        )
                    }
                    for s in packet.get("sources", [])
                    if s["id"] not in quote_ids
                ],
            }
        )

    def _acceptance_extraction_work(self, issuer):
        # Work is explicitly distinct from verified financial progress. A new
        # empty attempt permits visiting another filing, but identical attempts
        # cannot keep a company alive. The lifetime batch cap still applies.
        return {
            fingerprint(
                [page, event["payload"].get("page_request_signatures", {}).get(page[0])]
            )
            for event in self.store.events(kind="extraction", company_id=issuer)
            for page in event["payload"].get("pages", [])
        }

    def _acceptance_blocker_alert(self, issuer, outcome):
        if not self.settings.get("notifications", {}).get("enabled", False):
            return
        errors = outcome.get("errors", [])
        payload = {
            "campaign_id": self.acceptance.id,
            "company_id": issuer,
            "reason_codes": sorted({e["code"] for e in errors}),
            "reason_fields": sorted({e["field"] for e in errors if e.get("field")}),
            "reason": outcome.get("reason", "no_unattempted_evidence_progress"),
            "next_action": "修复列出的证据或解析问题后续跑；保留已用额度，继续处理其他公司。",
            "versions": self._versions(),
            "evidence_hash": outcome["evidence_hash"],
        }
        notification_id = self.store.enqueue("acceptance_blocker", payload)
        with self.store.transaction() as db:
            db.execute(
                "UPDATE outbox SET target_channels=? WHERE id=?",
                (canonical(["email"]), notification_id),
            )
        self._deliver()

    def _run_acceptance(self):
        """Exercise complete gates on a frozen small cohort without rediscovery."""
        self.settings["screen_first_extraction"] = True
        state = self.acceptance.state()
        if state["status"] == "not_started":
            self._reserve_continuations(reserve=False)
            state = self.acceptance.start(
                sorted(
                    self._continuation_ids,
                    key=lambda issuer: self._continuation_ranks[issuer],
                )
            )
            self._event("", "acceptance_started", state)
        self.settings.setdefault("sources", {})["acceptance_document_budget"] = {
            "id": self.acceptance.id,
            "limit": 12,
            "expires_at": state["config"]["expires_at"],
        }
        for issuer in state.get("targets", []):
            if self.acceptance.state()["status"] != "active":
                break
            if state.get("outcomes", {}).get(issuer, {}).get("status") in {
                "published",
                "screened",
                "no_progress",
                "blocked",
            }:
                continue
            while self.acceptance.state()["status"] == "active":
                self._roll_budget_day()
                control = self.root / "worker" / "runtime" / "drain.json"
                if control.exists():
                    request = json.loads(control.read_text(encoding="utf-8"))
                    if request.get("run_id") and request["run_id"] == os.environ.get(
                        "FQ_RUN_ID"
                    ):
                        self._event("", "run_drained", {"run_id": request["run_id"]})
                        return self.report()
                with self.store.transaction() as db:
                    saved = db.execute(
                        "SELECT * FROM companies WHERE id=?", (issuer,)
                    ).fetchone()
                if not saved or not saved["packet"]:
                    self.acceptance.checkpoint(
                        issuer,
                        {"status": "blocked", "reason": "saved_evidence_missing"},
                    )
                    break
                row = dict(saved)
                for key in ("company", "packet", "errors"):
                    row[key] = json.loads(row[key])
                before = self._acceptance_evidence_hash(row["packet"])
                attempted_before = self._acceptance_extraction_work(issuer)
                before_facts = len(row["packet"].get("facts", []))
                self._acceptance_dependency_before = {}
                self._progress("acceptance_company", issuer)
                self._process(row)
                self._packets.clear()
                with self.store.transaction() as db:
                    current = dict(
                        db.execute(
                            "SELECT * FROM companies WHERE id=?", (issuer,)
                        ).fetchone()
                    )
                packet = json.loads(current["packet"] or "{}")
                after = evidence_hash(packet, exclude_prices=True)
                financial_progress = before != self._acceptance_evidence_hash(packet)
                attempted_new = (
                    self._acceptance_extraction_work(issuer) - attempted_before
                )
                dependency_progress = self._acceptance_dependency_progress()
                errors = json.loads(current["errors"])
                publication = self.store.current(issuer)
                verified_publication = bool(
                    publication and publication.get("status") == "published"
                )
                status = (
                    "published"
                    if verified_publication
                    else "screened"
                    if current["stage"] == "screened"
                    else (
                        "no_progress"
                        if not financial_progress
                        and not dependency_progress
                        and not attempted_new
                        else "progress"
                    )
                )
                outcome = {
                    "status": status,
                    "stage": current["stage"],
                    "screen": current["screen"],
                    "errors": errors,
                    "before_facts": before_facts,
                    "after_facts": len(packet.get("facts", [])),
                    "evidence_hash": after,
                    "dependencies_advanced": dependency_progress,
                    "financial_evidence_advanced": financial_progress,
                    "newly_attempted_page_requests": len(attempted_new),
                    "reason": (
                        "new_unvisited_extraction_work"
                        if attempted_new
                        and not financial_progress
                        and not dependency_progress
                        else "evidence_advanced"
                        if financial_progress or dependency_progress
                        else "no_unattempted_evidence_progress"
                    ),
                }
                self.acceptance.checkpoint(issuer, outcome)
                self.report()
                if status in {"no_progress", "blocked"}:
                    self._acceptance_blocker_alert(issuer, outcome)
                if status != "progress":
                    break
        state = self.acceptance.state()
        if state["status"] == "active":
            success = any(
                x.get("status") == "published" for x in state["outcomes"].values()
            )
            self.acceptance.finish(
                "completed" if success else "no_progress",
                "verified_report_produced" if success else "cohort_gates_blocked",
            )
        state = self.acceptance.state()
        self._event("", "acceptance_finished", state)
        result = self.report()
        if self.settings.get("notifications", {}).get("enabled", False):
            notification_id = self.store.enqueue("acceptance", state)
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE outbox SET target_channels=? WHERE id=?",
                    (canonical(["email"]), notification_id),
                )
            self._deliver()
        self._progress("acceptance_finished")
        return result

    def run(self):
        with exclusive_process_lock(self.root / ".run.lock") as acquired:
            if not acquired:
                return envelope(
                    "blocked",
                    None,
                    self.today.isoformat(),
                    [issue("research_already_running")],
                )
            start = time.monotonic()
            self._roll_budget_day()
            from .session_prompts import VERSION as pdf_policy_version

            pdf_active = any(e["payload"].get("version") == pdf_policy_version
                             for e in self.store.events(kind="research_policy_activated"))
            if self.settings.get("pdf_sessions", {}).get("enabled") or pdf_active:
                from .session_workflow import PDFResearchWorkflow

                return PDFResearchWorkflow(self).run()
            model_led = self.settings.get("model_led", {})
            from .research_prompts import VERSION as research_policy_version

            activated = any(
                e["payload"].get("version") == research_policy_version
                for e in self.store.events(kind="research_policy_activated")
            )
            if model_led.get("mode") != "disabled" and (
                model_led.get("enabled", False) or activated
            ):
                from .research_workflow import ResearchWorkflow

                return ResearchWorkflow(self).run()
            if self.acceptance.enabled:
                return self._run_acceptance()
            self._event("", "effective_model_budget", self._model_budget_state())
            self.bootstrap.refresh(initialize=True, complete=True)
            self._progress("discovery")
            self._reserve_continuations()
            self._reserve_bootstrap_pending()
            try:
                discovered = self.sources.discover(self.today)
                self.store.register_many(
                    discovered.get("data", {}).get("companies", [])
                )
                for tombstone in discovered.get("data", {}).get(
                    "excluded_companies", []
                ):
                    try:
                        if date.fromisoformat(tombstone["as_of"]) > self.today:
                            raise ValueError("future universe exclusion")
                        self.store.exclude(tombstone)
                        self._packets.pop(tombstone["id"], None)
                    except (KeyError, ValueError, TypeError) as exc:
                        discovered.setdefault("errors", []).append(
                            issue(
                                "invalid_universe_exclusion",
                                message=str(exc),
                                stage="discovery",
                            )
                        )
                self._event("", "discovery", discovered)
            except Exception as exc:
                self._event(
                    "",
                    "discovery",
                    envelope(
                        "unavailable",
                        {},
                        self.today.isoformat(),
                        [issue("discovery_failed", message=type(exc).__name__)],
                    ),
                )
            # Discovery changes the denominator immediately, even if the first
            # issuer takes hours or the process is interrupted during collection.
            self.bootstrap.refresh(initialize=True, complete=True)
            self.report()
            last_report_at = time.monotonic()
            rows = self.store.companies(include_packet=False)
            # Aged rows retain a fifth of the batch slots; A500/candidate
            # dependencies receive the remainder without starving expansion.
            aged = rows[:10]
            rest = sorted(
                rows[10:],
                key=lambda r: (
                    not r["company"].get("a500", False),
                    r["updated_at"],
                    r["id"],
                ),
            )
            advanced = 0
            batch_limit = max(
                self._acquisition_limit(),
                self._limit("daily_research_limit", 20),
            )
            # The complete manifest is always reported. Discovery must not
            # force an unbounded per-company filesystem scan on the first run.
            with self.budget_store.transaction() as db:
                reserved_ids = {
                    r[0]
                    for r in db.execute(
                        "SELECT key FROM reservations WHERE day=? AND kind='acquisition'",
                        (self.budget_day.isoformat(),),
                    )
                }
            completed_ids = {
                e["company_id"]
                for e in self.store.events(kind="collection_finished")
                if e["payload"].get("budget_day") == self.budget_day.isoformat()
            }
            pending_reserved = reserved_ids - completed_ids
            ordered = sorted(
                [*aged, *rest],
                key=lambda r: (
                    r["id"] not in self._continuation_ids,
                    self._continuation_ranks.get(r["id"], 0),
                    self.bootstrap.priority(r["id"]),
                    r["id"] not in pending_reserved,
                ),
            )
            for row in ordered:
                self._roll_budget_day()
                if time.monotonic() - start > self._limit("max_run_seconds", 10800):
                    self._event(
                        "",
                        "run_budget",
                        {"status": "exhausted", "remaining_companies": True},
                    )
                    break
                if row["company"].get("peer_only") or row["stage"] == "excluded":
                    continue
                if advanced >= batch_limit and row["stage"] != "published":
                    continue
                # A graceful restart stops at a company boundary, preserving the
                # active provider/model request and all of its budget receipts.
                control = self.root / "worker" / "runtime" / "drain.json"
                if control.exists():
                    request = json.loads(control.read_text(encoding="utf-8"))
                    if request.get("run_id") == os.environ.get(
                        "FQ_RUN_ID"
                    ) and request.get("run_id"):
                        self._event("", "run_drained", {"run_id": request["run_id"]})
                        break
                with self.store.transaction() as db:
                    saved = db.execute(
                        "SELECT packet FROM companies WHERE id=?", (row["id"],)
                    ).fetchone()
                if saved and saved[0]:
                    row["packet"] = json.loads(saved[0])
                self._process(row)
                self._progress("company_finished", row["id"])
                row.pop("packet", None)
                # Durable packets live in SQLite; bound this batch's raw-text RAM.
                self._packets.clear()
                advanced += 1
                # Database state is visible immediately. Publish file snapshots
                # for actual research progress and at the end of the daily pass.
                if self.store.current(
                    row["id"]
                ) or time.monotonic() - last_report_at >= self._limit(
                    "report_interval_seconds", 300
                ):
                    self.report()
                    last_report_at = time.monotonic()
            self.bootstrap.refresh(complete=True)
            result = self.report()
            self._deliver()
            self._progress("batch_finished")
            return result

    def status(self):
        if not (self.root / "research.sqlite3").exists():
            return {
                "as_of": self.today.isoformat(),
                "status": "not_started",
                "companies": [],
                "budgets": {},
                "metrics": {},
                "errors": [issue("research_not_started")],
            }
        from .auditing import build_audit_queue
        from .research_release import revalidate_published_research

        revalidate_published_research(
            self.store,
            datetime.now(pytz.timezone("Asia/Shanghai")).date()
            if self._live_calendar
            else self.today,
        )
        from .evaluation import evaluate_traces
        from .worker import read_worker_status

        runtime = read_worker_status(self.config)

        with self.store.transaction() as db:
            progress = {
                row["company_id"]: json.loads(row["payload"])
                for row in db.execute(
                    "SELECT company_id,payload FROM events WHERE id IN "
                    "(SELECT MAX(id) FROM events WHERE kind='evidence_progress' "
                    "GROUP BY company_id)"
                )
            }
        publications = self.store.current_many()
        from .research_release import revalidate_staging
        from .session_store import SessionStore

        session_progress = SessionStore(self.store).summaries()
        staged = {r["company_id"]: {**r["payload"], "version": r["version"],
                                   "status": "reviewed_before_activation"}
                  for r in revalidate_staging(self.store)["reports"]
                  if r["policy_version"] == "raw-pdf-sessions/1"}
        companies = []
        for row in self.store.companies(include_packet=False):
            publication = publications.get(row["id"])
            if publication and publication["status"] == "published":
                quote_date = publication.get("quote_date")
                if not quote_date or (
                    self.today - date.fromisoformat(quote_date)
                ).days > self.settings.get("max_quote_age_days", 7):
                    # All read surfaces suppress stale publications immediately,
                    # even if the daily worker hasn't run yet.
                    publication = {
                        "status": "stale",
                        "version": publication["version"],
                        "issues": [issue("publication_quote_stale")],
                    }
            companies.append(
                {
                    **row["company"],
                    "stage": row["stage"],
                    "screen": row["screen"],
                    "status": row["stage"],
                    "errors": row["errors"],
                    "updated_at": row["updated_at"],
                    "evidence_progress": progress.get(row["id"], {}),
                    "publication": publication,
                    "research_session": session_progress.get(row["id"]),
                    "reviewed_report": staged.get(row["id"]),
                }
            )
        events = self.store.events(kind="evaluation_trace")
        discoveries = [
            e["payload"] for e in self.store.events(kind="discovery", limit=1)
        ]
        discovery = discoveries[-1] if discoveries else {}
        return {
            "as_of": self.today.isoformat(),
            "status": "partial"
            if discovery.get("errors")
            or any(
                (c["errors"] or c["stage"] not in {"screened", "published"})
                and c["stage"] != "excluded"
                for c in companies
            )
            else "ready",
            "companies": companies,
            "universe_counts": {
                "registered": len(companies),
                "active_or_pending": sum(c["stage"] != "excluded" for c in companies),
                "excluded": sum(c["stage"] == "excluded" for c in companies),
            },
            "budgets": self.budget_store.budgets(self.budget_day.isoformat()),
            "acceptance_campaign": self.acceptance.state(),
            "acquisition_policy": self.bootstrap.refresh(),
            "baseline": self.bootstrap.state.get("baseline", {}),
            "runtime": runtime.get("progress_summary", {}),
            "limits": {
                "acquisition": self._acquisition_limit(),
                "research": self._limit("daily_research_limit", 20),
                "model_calls": self._limit("daily_model_call_limit", 80),
                "input_token_reservations": self._limit(
                    "daily_input_token_limit", 10000000
                ),
                "output_token_reservations": self._limit(
                    "daily_output_token_limit", 500000
                ),
            },
            "budget_day": self.budget_day.isoformat(),
            "metrics": evaluate_traces(
                [e["payload"] for e in events if e["kind"] == "evaluation_trace"]
            ),
            "audit_queue": build_audit_queue(
                companies,
                self.store.events(kind="correction_findings") + events,
                self.today.isoformat(),
                sample_count=self._limit("daily_audit_sample_count", 5),
            ),
            "coverage": discovery.get("coverage", {}),
            "errors": discovery.get("errors", []),
            "pending_notifications": len(self.store.outbox()),
        }

    def query(self, code=None):
        from .longitudinal_release import group_bot_view, list_group_reports

        snapshot = self.status()
        snapshot["longitudinal_reports"] = [
            group_bot_view(report) for report in list_group_reports(self.store, code)
        ]
        if code:
            snapshot["companies"] = [
                c
                for c in snapshot["companies"]
                if c.get("code") == code or c.get("id") == code
            ]
            if not snapshot["companies"] and not snapshot["longitudinal_reports"]:
                snapshot["errors"] = [
                    *snapshot["errors"],
                    issue("company_not_in_research_registry", str(code)),
                ]
            for company in snapshot["companies"]:
                with self.store.transaction() as db:
                    row = db.execute(
                        "SELECT packet FROM companies WHERE id=?", (company["id"],)
                    ).fetchone()
                if row and row[0]:
                    valid, errors = validate_packet(json.loads(row[0]), self.today)
                    company["verified_facts"] = valid
                    company["fact_verification_errors"] = errors
                    company["verification_limit"] = (
                        "Numerical provenance checks only; not independent accounting interpretation"
                    )
        # Never leak revoked claims to bots even if callers forget the renderer.
        from .reporting import visible_company

        snapshot["companies"] = [visible_company(c) for c in snapshot["companies"]]
        return snapshot

    def report(self):
        from .reporting import render_reports
        from .research_release import revalidate_staging

        revalidate_staging(self.store)

        started = time.monotonic()
        logger.info("FQ full-registry report begin")
        snapshot = self.query()
        if (self.root / "research.sqlite3").exists():
            audit = {
                "as_of": self.today.isoformat(),
                "queue": snapshot.get("audit_queue", []),
            }
            prior = self.store.events(kind="audit_queue", limit=1)
            if not prior or fingerprint(prior[0]["payload"]) != fingerprint(audit):
                self._event("", "audit_queue", audit)
        artifacts = render_reports(self.root, snapshot)
        logger.info(
            "FQ full-registry report finished issuers=%s seconds=%.1f",
            len(snapshot["companies"]),
            time.monotonic() - started,
        )
        return {**snapshot, "artifacts": artifacts}

    def _deliver(self):
        if not self.settings.get("notifications", {}).get("enabled", False):
            return
        with exclusive_process_lock(self.root / ".notify.lock") as acquired:
            if not acquired:
                self._event("", "notification_busy", {"status": "pending"})
                return
            self._drain_notifications()

    def _drain_notifications(self):
        from .longitudinal_release import deliver_group_notifications
        from .research_release import revalidate_staging

        deliver_group_notifications(self.store, self.config)

        revalidate_staging(self.store)
        from ..notification.settings import channel_enabled
        from .notifications import deliver_notification

        required = [
            c
            for c in ("email", "feishu", "telegram")
            if channel_enabled(self.config, c)
        ]
        for item in self.store.outbox():
            targets = item.get("target_channels")
            item_required = [c for c in required if targets is None or c in targets]
            channels = [
                c
                for c in item_required
                if item["delivered"].get(c, {}).get("status") != "sent"
            ]
            if not channels:
                continue
            response = deliver_notification(
                self.config,
                "Fundamental Quant｜"
                + {
                    "weekly": "每周研究进展",
                    "correction": "研究结论更正",
                    "breaker": "失效条件变化",
                }.get(item["kind"], "研究状态更新"),
                canonical(item["payload"]),
                report={
                    "kind": item["kind"],
                    "payload": item["payload"],
                    "snapshot": self.query(),
                    "limits": {
                        "acquisition": self._acquisition_limit(),
                        "research": self._limit("daily_research_limit", 20),
                        "model_calls": self._limit("daily_model_call_limit", 80),
                    },
                },
                channels=channels,
            )
            self.store.delivery(item["id"], response.get("channels", {}), item_required)
            self._event("", "notification_delivery", {"id": item["id"], **response})
            revalidate_staging(self.store)

    def notify_weekly(self):
        snapshot = self.query()
        self.store.enqueue(
            "weekly",
            {
                "week": self.today.strftime("%G-W%V"),
                "as_of": self.today.isoformat(),
                "candidates": [
                    c["code"]
                    for c in snapshot["companies"]
                    if c.get("publication", {})
                    and c["publication"].get("status") == "published"
                    and c["publication"].get("candidate")
                ],
                "publication_refs": [
                    {"company_id": c["id"], "version": c["publication"]["version"]}
                    for c in snapshot["companies"]
                    if c.get("publication")
                    and c["publication"].get("status") == "published"
                    and c["publication"].get("candidate")
                ],
                "coverage": snapshot.get("coverage", {}),
                "budgets": snapshot["budgets"],
                "errors": snapshot["errors"],
            },
        )
        self._deliver()
        return self.status()
