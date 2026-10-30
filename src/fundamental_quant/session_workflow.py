"""Continuous raw-PDF research and isolated round review, on existing ledgers."""

from __future__ import annotations

# Provider boundaries persist visible failures; never turn exceptions into success.
# ruff: noqa: BLE001
import copy
import json
import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .contracts import canonical, fingerprint, issue
from .pdf_sessions import DeepSeekFiles, PageDocument, PDFBatchReader, PDFSessionError
from .research_release import release_status, stage_report
from .session_baseline import financial_baseline
from .session_context import ContextLimits, ContextManager, load_token_counter
from .session_prompts import (
    COMPACT,
    PROTOCOL_VERSION,
    RESEARCH,
    REVIEW,
    TOOLS,
    VERSION,
    initial_messages,
)
from .session_review import (
    calculate,
    deterministic_calculation_preview,
    evaluate_round,
    render_round_html,
    report_payload,
)
from .session_store import SessionStore, session_id
from .store import utcnow


class PDFResearchWorkflow:
    """Each checkpoint advances exactly one durable research/review boundary.

    Construction, inventory and estimates never make provider requests. Public
    run/step require an already active campaign, or a reviewed activated policy.
    """

    def __init__(self, service, *, reader=None, files=None, token_counter=None):
        self.service, self.store = service, service.store
        self.settings = service.settings.get("pdf_sessions", {})
        self.sessions = SessionStore(self.store)
        self.reader = reader or PDFBatchReader(self.store, service.root / "pdf_pages")
        self.files = files
        self.token_counter = token_counter
        self.execution_revision = fingerprint(
            {
                "code": {
                    name: Path(__file__)
                    .with_name(name + ".py")
                    .read_text(encoding="utf-8")
                    for name in (
                        "session_workflow",
                        "session_context",
                        "session_review",
                        "session_prompts",
                        "pdf_sessions",
                        "session_sources",
                        "service",
                        "llm",
                    )
                },
                "pdf_parameters": {
                    key: self.settings.get(key)
                    for key in (
                        "context_tokens",
                        "retain_record_tokens",
                        "max_output_tokens",
                        "compact_max_output_tokens",
                        "tokenizer_path",
                    )
                },
                "model_parameters": service._versions()
                if hasattr(service, "_versions")
                else service.settings.get("llm", {}),
            }
        )

    def _packet(self, issuer):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT company,packet,input_hash,stage FROM companies WHERE id=?",
                (issuer,),
            ).fetchone()
        if not row or row["stage"] == "excluded":
            raise ValueError("unknown_or_excluded_company")
        return (
            json.loads(row["packet"])
            if row["packet"]
            else {"company": json.loads(row["company"]), "facts": [], "sources": []}
        )

    def inventory(self, issuer):
        packet = self._packet(issuer)
        documents = self.reader.discover(packet)
        if hasattr(self.reader, "discover_catalog"):
            documents += self.reader.discover_catalog(
                packet["company"], self.service.today
            )
        documents = list({(d.issuer_id, d.sha256): d for d in documents}.values())
        cutoff = self.service.today.year - 7
        return [
            asdict(doc)
            for doc in documents
            if cutoff <= int(doc.period_end[:4]) <= self.service.today.year
        ]

    def estimate(self, issuers):
        """Offline source inventory; known pages are not promised completeness."""
        entries = []
        for issuer in issuers:
            documents, errors = [], []
            try:
                for item in self.inventory(issuer):
                    try:
                        pages = self.reader.page_count(PageDocument(**item))
                        documents.append(
                            {
                                **item,
                                "page_count": pages,
                                "batches": math.ceil(pages / 50),
                            }
                        )
                    except Exception as exc:
                        errors.append(
                            {
                                "document_id": item["document_id"],
                                "code": getattr(exc, "code", type(exc).__name__),
                            }
                        )
            except Exception as exc:
                errors.append({"code": getattr(exc, "code", type(exc).__name__)})
            batches = sum(d["batches"] for d in documents)
            entries.append(
                {
                    "company_id": issuer,
                    "documents": documents,
                    "errors": errors,
                    "known_batches": batches,
                    "minimum_read_and_review_calls": batches * 2,
                    "new_page_image_tokens_upper": sum(
                        d["page_count"] for d in documents
                    )
                    * 1024,
                }
            )
        return {
            "policy_version": VERSION,
            "companies": entries,
            "paid_calls": 0,
            "scope": "cached_documents_not_full_pool",
            "limitations": [
                "Includes no uncollected peer documents",
                "Repeated context, tools and compaction cost extra",
                "Estimates are not budget authorization",
            ],
        }

    def preflight(self, issuers=()):
        campaign = self.service.acceptance.state()
        activated = any(
            e["payload"].get("version") == VERSION
            for e in self.store.events(kind="research_policy_activated")
        )
        errors = []
        if campaign.get("enabled"):
            if campaign.get("status") != "active":
                errors.append(issue("active_explicit_research_budget_required"))
            if set(issuers) - set(campaign.get("targets", [])):
                errors.append(issue("targets_outside_authorized_campaign"))
            for k, limit in campaign.get("limits", {}).items():
                if campaign.get("usage", {}).get(k, 0) >= limit:
                    errors.append(issue("acceptance_budget_exhausted", k))
        elif not activated:
            errors.append(issue("active_explicit_research_budget_required"))
        if (
            not self.service.settings.get("llm", {}).get("enabled")
            and self.service._llm is None
        ):
            errors.append(issue("research_model_disabled"))
        if self.token_counter is None:
            try:
                self.token_counter = load_token_counter(
                    Path(
                        self.settings.get(
                            "tokenizer_path",
                            str(self.service.root / "tokenizer" / "tokenizer.json"),
                        )
                    )
                )
            except (OSError, ValueError, ImportError):
                errors.append(issue("session_tokenizer_unavailable"))
        if self.files is None:
            try:
                client = DeepSeekFiles(self.store, self.service.config)
                if not client.api_key.strip():
                    errors.append(issue("files_credentials_missing"))
            except (ValueError, PDFSessionError) as exc:
                errors.append(
                    issue(getattr(exc, "code", "files_configuration_invalid"))
                )
        return {
            "status": "blocked" if errors else "ready",
            "errors": errors,
            "campaign_id": campaign.get("id"),
            "limits": campaign.get("limits", {}),
            "usage": campaign.get("usage", {}),
            "expires_at": campaign.get("expires_at"),
        }

    def _guard(self, issuer):
        from .legacy_service import ResearchBlocked

        state = self.preflight([issuer])
        if state["errors"]:
            raise ResearchBlocked(state["errors"])

    def _manager(self, state=None, role="research"):
        return ContextManager(
            ContextLimits(
                context_tokens=int(self.settings.get("context_tokens", 1_000_000)),
                output_tokens=self.service._output_token_limit("pdf_" + role),
                compact_output_tokens=self.service._output_token_limit("pdf_compact"),
                retain_record_tokens=max(
                    8192, int(self.settings.get("retain_record_tokens", 8192))
                ),
                input_margin_tokens=(state or {}).get("input_margin_tokens", 0),
            ),
            token_counter=self.token_counter,
        )

    def _save(self, state, kind, detail=None, round_result=None):
        return self.sessions.save(
            state, kind=kind, detail=detail, round_result=round_result
        )

    def open(self, issuer):
        self._guard(issuer)
        campaign_id = (
            self.service.acceptance.id
            if self.service.acceptance.enabled
            else "continuous"
        )
        sid = session_id(issuer, campaign_id, VERSION)
        saved = self.sessions.load(sid)
        if saved:
            if saved["phase"] in {"blocked", "deferred"}:
                docs = self.inventory(issuer)
                evidence = fingerprint(
                    {"packet": self._packet(issuer), "documents": docs}
                )
                previous = saved.get("blocked_evidence_hash")
                method_changed = bool(
                    saved.get("blocked_execution_revision")
                    and saved["blocked_execution_revision"] != self.execution_revision
                )
                review_invalidated = any(
                    e.get("code") == "reviewed_evidence_changed_or_not_frozen"
                    for e in saved.get("errors", [])
                ) and saved.get("reviewed_evidence_hash") != fingerprint(
                    self._packet(issuer)
                )
                root = fingerprint(
                    {
                        "codes": sorted(
                            e.get("code", "unknown") for e in saved.get("errors", [])
                        ),
                        "phase": saved.get("failed_phase"),
                    }
                )
                attempts = saved.setdefault("resume_counts", {}).get(root, 0)
                administrative_pause = bool(saved.get("errors")) and all(
                    e.get("code") == "active_explicit_research_budget_required"
                    for e in saved["errors"]
                )
                if (
                    (previous and previous != evidence)
                    or method_changed
                    or administrative_pause
                    or review_invalidated
                ) and (attempts < 2 or administrative_pause):
                    format_error = any(
                        e.get("code") == "invalid_response"
                        for e in saved.get("errors", [])
                    )
                    saved["documents"] = list(
                        {
                            d["document_id"]: d for d in saved["documents"] + docs
                        }.values()
                    )
                    saved["phase"] = (
                        (saved.get("failed_phase") or "research")
                        if (previous == evidence and not review_invalidated)
                        or (administrative_pause and not previous)
                        else "research"
                    )
                    if not administrative_pause:
                        saved["resume_counts"][root] = attempts + 1
                    saved["historical_errors"] = saved.get(
                        "historical_errors", []
                    ) + saved.get("errors", [])
                    saved["errors"] = []
                    saved = self._save(
                        saved,
                        "administrative_resume"
                        if administrative_pause
                        else "evidence_changed_resume",
                        {"previous": previous, "current": evidence},
                    )
                    if format_error:
                        corrected = self._queue_format_correction(saved, saved["phase"])
                        if corrected is None:
                            raise ValueError(
                                "format_correction_unavailable_or_exhausted"
                            )
                        saved = corrected
                    if previous != evidence or review_invalidated:
                        packet = self._packet(issuer)
                        quotes = [
                            s
                            for s in packet.get("sources", [])
                            if s.get("kind") == "quote"
                        ]
                        for source in quotes:
                            saved["pages"][source["id"]] = {
                                **source,
                                "source_ref": source["id"],
                                "company_id": source.get("issuer_id", issuer),
                            }
                        refresh = {
                            "evidence_changed": True,
                            "documents": docs,
                            "observation_sources": quotes,
                            "available_observations": [
                                f
                                for f in packet.get("facts", [])
                                if f.get("source_id") in {s["id"] for s in quotes}
                            ],
                            "instruction": "依据新增资料重新判断受影响结论，不能沿用旧审查。",
                        }
                        saved["review"] = {}
                        for role in ("research", "review"):
                            saved["seen"][role] = sorted(
                                set(saved["seen"][role]) | {s["id"] for s in quotes}
                            )
                            saved = self._append(
                                saved,
                                role,
                                [{"role": "user", "content": canonical(refresh)}],
                            )
            return saved
        packet = self._packet(issuer)
        docs = self.inventory(issuer)
        own = [d for d in docs if d["issuer_id"] == issuer]
        own.sort(key=lambda d: (d["kind"] == "annual", d["period_end"]), reverse=True)
        if not own:
            raise ValueError("no_original_pdf_for_company")
        state = {
            "id": sid,
            "company": packet["company"],
            "campaign_id": campaign_id,
            "policy_version": VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "revision": 0,
            "round": 0,
            "started_at": utcnow(),
            "phase": "read",
            "documents": docs,
            "document_id": own[0]["document_id"],
            "next_page": 1,
            "peers": [],
            "pages": {},
            "images": {},
            "coverage": {},
            "contexts": {
                role: initial_messages(role) for role in ("research", "review")
            },
            "seen": {"research": [], "review": []},
            "errors": [],
            "tool_attempts": {},
            "last_reviewed": None,
        }
        quote_sources = {
            s["id"]: s for s in packet.get("sources", []) if s.get("kind") == "quote"
        }
        for ref, source in quote_sources.items():
            state["pages"][ref] = {
                **source,
                "id": ref,
                "source_ref": ref,
                "company_id": source.get("issuer_id", issuer),
            }
        bootstrap = {
            "company": packet["company"],
            "financial_baseline": financial_baseline(packet, self.service.today),
            "as_of": self.service.today.isoformat(),
            "documents": docs,
            "available_observations": [
                f
                for f in packet.get("facts", [])
                if f.get("source_id") in quote_sources
            ],
            "observation_sources": [
                s for s in packet.get("sources", []) if s.get("kind") == "quote"
            ],
            "notice": "Observations are reference data, verify against raw pages.",
        }
        message = {"role": "user", "content": canonical(bootstrap)}
        for role in state["contexts"]:
            state["contexts"][role].append(copy.deepcopy(message))
            state["seen"][role] = list(quote_sources)
        return self._save(state, "opened", {"bootstrap": bootstrap})

    def _doc(self, state, document_id):
        for item in state["documents"]:
            if item["document_id"] == document_id:
                return PageDocument(**item)
        raise ValueError("document_not_in_session_manifest")

    def _page_message(self, state, pages, purpose):
        # Upload only after the campaign check, including on tool rereads.
        self._guard(state["company"]["id"])
        if self.files is None:
            self.files = DeepSeekFiles(self.store, self.service.config)
        content = [
            {
                "type": "text",
                "text": canonical(
                    {
                        "purpose": purpose,
                        "pages": [
                            {
                                k: p.get(k)
                                for k in (
                                    "source_ref",
                                    "issuer_id",
                                    "period_end",
                                    "published_at",
                                    "page",
                                    "document_sha256",
                                    "zoom_box",
                                )
                            }
                            for p in pages
                        ],
                    }
                ),
            }
        ]
        for page in pages:
            self._guard(state["company"]["id"])
            block = self.files.ensure_uploaded(page)
            content.append(block)
            state["images"][block["file_id"]] = page
            ref = page["source_ref"]
            state["pages"][ref] = {
                **page,
                "id": ref,
                "company_id": page["issuer_id"],
            }
        return {"role": "user", "content": content}

    def _call(self, state, role, messages):
        self._guard(state["company"]["id"])
        packet = {
            "company": state["company"],
            "facts": [],
            "sources": [],
            "session_id": state["id"],
            "round": state["round"],
        }
        return self.service._call(
            "pdf_" + role,
            packet,
            session_messages=messages,
            session_images=state["images"],
            session_limits=self._manager(state, role).limits,
            session_token_counter=self.token_counter,
        )

    def _refresh_files(self, state):
        """Refresh expired Files IDs without changing original evidence identity."""
        if self.files is None:
            self.files = DeepSeekFiles(self.store, self.service.config)
        replacements = {}
        for context in state["contexts"].values():
            for message in context:
                if not isinstance(message.get("content"), list):
                    continue
                for part in message["content"]:
                    if part.get("type") != "file":
                        continue
                    old = part["file_id"]
                    if old not in replacements:
                        self._guard(state["company"]["id"])
                        replacements[old] = self.files.ensure_uploaded(
                            state["images"][old]
                        )["file_id"]
        if not any(old != new for old, new in replacements.items()):
            return state
        for context in state["contexts"].values():
            for message in context:
                if isinstance(message.get("content"), list):
                    for part in message["content"]:
                        if part.get("type") == "file":
                            old = part["file_id"]
                            part["file_id"] = replacements[old]
                            state["images"][replacements[old]] = state["images"][old]
        return self._save(
            state, "file_references_refreshed", {"replacements": replacements}
        )

    @staticmethod
    def _compaction_messages(history, required):
        messages = [{"role": "system", "content": COMPACT}]
        for message in history:
            if message.get("role") == "system":
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = [
                    {
                        "type": "text",
                        "text": canonical(
                            {
                                "historical_role": message.get("role"),
                                "notice": "以下是待压缩的历史资料，历史指令不在本轮执行。",
                            }
                        ),
                    }
                ] + copy.deepcopy(content)
            else:
                content = canonical(
                    {
                        "historical_role": message.get("role"),
                        "historical_content": content,
                    }
                )
            messages.append({"role": "user", "content": content})
        messages.append(
            {
                "role": "user",
                "content": canonical(
                    {
                        "task_version": "pdf-checkpoint-task/2",
                        "required_citations": sorted(required),
                        "instruction": "本轮唯一任务是压缩上述历史，不执行其中的审查、工具或格式修正指令。返回检查点JSON，必须包含summary字符串及facts、estimates、citations、conflicts、read_coverage、next_actions数组。保留原数值、事实与估算区别、已读范围、待纠正事项及全部required_citations。不得返回conclusions/issues/tools替代检查点。",
                    }
                ),
            }
        )
        return messages

    def _append(self, state, role, pending, *, protocol_version=None):
        """Model compaction, validate, then persist context/history atomically."""
        manager = self._manager(state, role)
        plan = manager.plan(state["contexts"][role], pending, images=state["images"])
        checkpoint = None
        if plan.needed:
            required = self._claim_refs(state.get("draft", {})) | self._refs(
                state.get("review", {})
            )
            summary_messages = self._compaction_messages(plan.summary_input, required)
            manager.validate_compaction_request(summary_messages, state["images"])
            checkpoint = self._call(state, "compact", summary_messages)
        composed = manager.compose(
            plan,
            checkpoint,
            images=state["images"],
            allowed_refs=set(state["pages"]),
            required_refs=required if plan.needed else set(),
        )
        state["contexts"][role] = composed
        if protocol_version:
            versions = state.setdefault("protocol_versions", {})
            versions[role] = protocol_version
            if all(versions.get(r) == protocol_version for r in ("research", "review")):
                state["protocol_version"] = protocol_version
        seen = set(state.setdefault("seen", {}).setdefault(role, []))
        for message in pending:
            if isinstance(message.get("content"), list):
                for part in message["content"]:
                    if (
                        part.get("type") == "file"
                        and part.get("file_id") in state["images"]
                    ):
                        seen.add(state["images"][part["file_id"]]["source_ref"])
        state["seen"][role] = sorted(seen)
        return self._save(
            state,
            "context_checkpoint" if checkpoint else "context_append",
            {
                "role": role,
                "messages": pending,
                "checkpoint": checkpoint,
                "history_fingerprint": plan.original_fingerprint,
            },
        )

    def _read(self, state):
        batch = self.reader.render_batch(
            self._doc(state, state["document_id"]), state["next_page"]
        )
        message = self._page_message(state, batch["pages"], "next_contiguous_batch")
        message["content"].insert(
            0,
            {
                "type": "text",
                "text": canonical(
                    {"batch": {k: v for k, v in batch.items() if k != "pages"}}
                ),
            },
        )
        state["round"] += 1
        state["batch"] = {k: v for k, v in batch.items() if k != "pages"}
        state["batch_refs"] = [p["source_ref"] for p in batch["pages"]]
        state["batch_message"] = message
        state["phase"] = "research"
        state["coverage"].setdefault(state["document_id"], []).append(
            [batch["start_page"], batch["end_page"]]
        )
        return self._append(state, "research", [message])

    def _tool(self, state, request, role):
        if not isinstance(request, dict):
            raise TypeError("tool_request_not_object")
        name = request.get("name") or request.get("tool")
        if (
            request.get("name")
            and request.get("tool")
            and request["name"] != request["tool"]
        ):
            raise ValueError("conflicting_tool_names")
        args = request.get(
            "arguments", {k: v for k, v in request.items() if k not in {"name", "tool"}}
        )
        if not isinstance(args, dict):
            raise TypeError("tool_arguments_not_object")
        digest = fingerprint(
            {
                "role": role,
                "request": request,
                "documents": state["documents"],
                "protocol": PROTOCOL_VERSION,
            }
        )
        previous = state["tool_attempts"].get(digest)
        retry_at = (previous or {}).get("retry_at")
        retry_due = bool(
            retry_at and datetime.now(timezone.utc).timestamp() >= float(retry_at)
        )
        if (
            previous
            and name not in {"read_pages", "zoom", "catalog", "calculate"}
            and not (name == "collect_report" and retry_due)
        ):
            if previous.get("status") == "error":
                return state, {**previous, "cached_failure": True}, []
            return (
                state,
                {
                    "status": "blocked",
                    "error": "same_tool_request_already_completed",
                    "previous": state["tool_attempts"][digest],
                },
                [],
            )
        pages = []
        if name == "calculate":
            records = copy.deepcopy(state.get("draft", {}))
            current = (
                state.get("response", {}) if state.get("response_role") == role else {}
            )
            for field in ("facts", "estimates", "calculations"):
                rows = {r["id"]: r for r in records.get(field, [])}
                for row in current.get(field, []):
                    if row["id"] in rows and row != rows[row["id"]]:
                        raise ValueError("immutable_calculator_record_changed")
                    rows[row["id"]] = row
                records[field] = list(rows.values())
            visible = set(state.get("seen", {}).get(role, []))
            preview = deterministic_calculation_preview(
                records,
                [p for ref, p in state.get("pages", {}).items() if ref in visible],
                company_id=state["company"]["id"],
                template=state["company"].get("template", "ordinary"),
            )
            values = dict(preview.get("values", {}))
            for key, supplied in args.get("values", {}).items():
                if key in values and values[key] != supplied:
                    raise ValueError("calculator_binding_conflicts_with_record")
                values[key] = supplied
            value = calculate(args.get("expression"), values)
            result = {
                "status": "success",
                "scope": "deterministic_only_not_reviewed",
                "data": {**args, "values": values, "value": value},
            }
        elif name == "catalog":
            issuer = args.get("company_id", state["company"]["id"])
            docs = self.inventory(issuer)
            if issuer != state["company"]["id"] and issuer not in state["peers"]:
                if len(state["peers"]) >= 3:
                    raise ValueError("peer_scope_limit")
                state["peers"].append(issuer)
            state["documents"] = list(
                {d["document_id"]: d for d in state["documents"] + docs}.values()
            )
            result = {
                "status": "success",
                "documents": docs,
                "peer_candidates": [
                    r["company"]
                    for r in self.store.companies(include_packet=False)
                    if r["company"].get("industry") == state["company"].get("industry")
                ][:40],
            }
        elif name == "collect":
            issuer = args.get("company_id")
            company = self._packet(issuer)["company"]
            allowed = {state["company"]["id"], *state["peers"]}
            if issuer not in allowed:
                if len(state["peers"]) >= 3 or not args.get("reason"):
                    raise ValueError("peer_limit_or_reason_missing")
                state["peers"].append(issuer)
            self._guard(state["company"]["id"])
            packet = self.service._collect(company)
            self.store.update(
                issuer,
                "evidence",
                packet=packet,
                errors=packet.get("acquisition_errors", []),
            )
            docs = self.inventory(issuer)
            by_id = {d["document_id"]: d for d in state["documents"]}
            by_id.update({d["document_id"]: d for d in docs})
            state["documents"] = list(by_id.values())
            result = {
                "status": "partial" if packet.get("acquisition_errors") else "success",
                "documents": docs,
                "errors": packet.get("acquisition_errors", []),
            }
        elif name == "collect_report":
            from .session_sources import SessionReportSource

            issuer = args.get("company_id", state["company"]["id"])
            packet = self._packet(issuer)
            if issuer != state["company"]["id"] and issuer not in state["peers"]:
                if len(state["peers"]) >= 3 or not args.get("reason"):
                    raise ValueError("peer_limit_or_reason_missing")
                state["peers"].append(issuer)
            self._guard(state["company"]["id"])
            result = SessionReportSource(self.service).collect(
                packet["company"], args.get("report_type"), args.get("year")
            )
            docs = result.get("documents", [])
            by_id = {d["document_id"]: d for d in state["documents"]}
            by_id.update({d["document_id"]: d for d in docs})
            state["documents"] = list(by_id.values())
            if docs:
                current = {
                    d["document_id"]: d for d in packet.get("pdf_session_documents", [])
                }
                current.update({d["document_id"]: d for d in docs})
                packet["pdf_session_documents"] = list(current.values())
                self.store.update(
                    issuer, "evidence", packet=packet, errors=result.get("errors", [])
                )
        elif name in {"read_pages", "zoom"}:
            doc = self._doc(state, args.get("document_id"))
            if name == "zoom":
                pages = [
                    self.reader.render_zoom(doc, args.get("page"), args.get("box"))
                ]
            elif "pages" in args:
                requested = args["pages"]
                if (
                    not isinstance(requested, list)
                    or not 1 <= len(requested) <= 50
                    or any(type(p) is not int or p < 1 for p in requested)
                ):
                    raise ValueError("pages_must_be_one_to_fifty_positive_page_numbers")
                pages = [
                    self.reader.render_zoom(doc, page, [0, 0, 1, 1])
                    for page in dict.fromkeys(requested)
                ]
                ranges = state["coverage"].setdefault(doc.document_id, [])
                for page in dict.fromkeys(requested):
                    if [page, page] not in ranges:
                        ranges.append([page, page])
            else:
                batch = self.reader.render_batch(doc, args.get("start_page", 1))
                pages = batch["pages"]
                ranges = state["coverage"].setdefault(doc.document_id, [])
                if [batch["start_page"], batch["end_page"]] not in ranges:
                    ranges.append([batch["start_page"], batch["end_page"]])
            result = {
                "status": "success",
                "page_refs": [p["source_ref"] for p in pages],
            }
        else:
            raise ValueError("unknown_session_tool")
        state["tool_attempts"][digest] = result
        extra = (
            [self._page_message(state, pages, "requested_original_page")]
            if pages
            else []
        )
        return state, result, extra

    def _queue_format_correction(self, state, role):
        """Ask the same role once to fix a complete, retained JSON-syntax failure."""
        if role not in {"research", "review"}:
            return None
        if state.get("format_corrections", {}).get(role, 0) >= 1:
            return None
        issuer = state["company"]["id"]
        errors = self.store.events(limit=1, kind="model_error", company_id=issuer)
        requests = self.store.events(limit=1, kind="model_request", company_id=issuer)
        if not errors or not requests:
            return None
        error, request = errors[0]["payload"], requests[0]["payload"]
        diagnostics = error.get("diagnostics", {})
        raw = error.get("raw_assistant_content")
        if (
            request.get("session_id") != state["id"]
            or request.get("role") != "pdf_" + role
            or error.get("request_hash") != request.get("request_hash")
            or diagnostics.get("error_category") != "json_syntax"
            or diagnostics.get("finish_reason") != "stop"
            or diagnostics.get("raw_content_truncated") is not False
            or not isinstance(raw, str)
            or not raw.strip()
        ):
            return None
        state.setdefault("format_corrections", {})[role] = 1
        state["phase"] = role
        self.store.event(
            issuer,
            "pdf_format_correction_requested",
            {
                "session_id": state["id"],
                "role": role,
                "failed_request_hash": error["request_hash"],
                "diagnostics": diagnostics,
                "attempt": 1,
            },
        )
        return self._append(
            state,
            role,
            [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": canonical(
                        {
                            "format_error": diagnostics,
                            "instruction": "上一条完整响应因JSON语法错误未被接受。仅重新输出合法JSON：字符串内部双引号须转义；保持原结论、数值、引用、问题、工具请求及其影响范围，不重做研究，不省略问题。不要输出代码围栏。此次仅允许一次格式补正，之后仍执行全部适用核验。",
                        }
                    ),
                },
            ],
        )

    def _ensure_protocol(self, state, role):
        """Upgrade only the context that is about to call the model.

        A saved response is consumed before any upgrade/compaction. Otherwise a
        reviewer correction already paid for can be stranded behind unrelated
        context growth. Each role commits its own marker with the append.
        """
        versions = state.setdefault("protocol_versions", {})
        if versions.get(role, state.get("protocol_version")) == PROTOCOL_VERSION:
            return state
        contract = RESEARCH if role == "research" else REVIEW
        state = self._append(
            state,
            role,
            [
                {
                    "role": "user",
                    "content": canonical(
                        {
                            "protocol_revision": PROTOCOL_VERSION,
                            "contract": contract,
                            "notice": "接口合同已修正；原图、原记录和旧审查仍保留。修正旧记录使用新id及supersedes，不能把失败回执当作不存在原文。",
                        }
                    ),
                }
            ],
            protocol_version=PROTOCOL_VERSION,
        )
        return state

    def _respond(self, state, role):
        self._guard(state["company"]["id"])
        state = self._ensure_protocol(state, role)
        state = self._refresh_files(state)
        estimate = self._manager(state).measure(
            state["contexts"][role], images=state["images"]
        )
        self.service._last_session_usage = None
        try:
            output = self._call(state, role, state["contexts"][role])
        except Exception as exc:
            if any(
                e.get("code") == "invalid_response" for e in getattr(exc, "errors", [])
            ):
                corrected = self._queue_format_correction(state, role)
                if corrected is not None:
                    return corrected
            raise
        usage = getattr(self.service, "_last_session_usage", None) or {}
        if type(usage.get("input_tokens")) is int:
            discrepancy = max(
                0, usage["input_tokens"] - estimate.estimated_input_tokens
            )
            state["input_margin_tokens"] = (
                state.get("input_margin_tokens", 0) + discrepancy
            )
            state["last_token_measurement"] = {
                "estimated_input_tokens": estimate.estimated_input_tokens,
                "actual_input_tokens": usage["input_tokens"],
                "input_margin_tokens": state["input_margin_tokens"],
            }
        response = {"role": "assistant", "content": canonical(output)}
        # Persist the paid response before executing anything or calling again.
        state["contexts"][role].append(response)
        state["response"] = output
        state["response_role"] = role
        state["phase"] = role + "_returned"
        state = self._save(
            state, "model_turn_completed", {"role": role, "message": response}
        )
        # Expose the paid, structured response immediately, including tool turns.
        # This audit view does not approve or publish any investment conclusion.
        from .session_notebook import render_session_notebooks

        render_session_notebooks(self.service.root)
        return state

    def _tools(self, state, role, output):
        if role == "review":
            seen = set(state.get("seen", {}).get("review", []))
            claims = {
                c.get("key") for c in state.get("draft", {}).get("conclusions", [])
            }
            decisive = any(
                i.get("severity") == "blocking"
                and i.get("reason")
                and set(i.get("affected_conclusions", [])) & claims
                and i.get("page_refs")
                and set(i["page_refs"]) <= seen
                for i in output.get("issues", [])
            )
            if decisive:
                # The reviewer has already identified a sourced correction.
                # Return it now; later reading remains explicit, not discarded.
                state["deferred_review_tools"] = copy.deepcopy(output.get("tools", []))
                self.store.event(
                    state["company"]["id"],
                    "pdf_review_correction_priority",
                    {
                        "session_id": state["id"],
                        "round": state["round"],
                        "deferred_tools": state["deferred_review_tools"],
                    },
                )
                return self._reviewed(state)
        requests = output.get("tools") or []
        if not isinstance(requests, list) or len(requests) > 8:
            raise ValueError("invalid_tool_requests")
        pending = []
        failed_this_turn = set()
        for request in requests:
            try:
                state, result, extra = self._tool(state, request, role)
            except Exception as exc:
                from .legacy_service import ResearchBlocked

                if isinstance(exc, ResearchBlocked):
                    raise
                result = {
                    "status": "error",
                    "error": getattr(exc, "code", type(exc).__name__),
                    "detail": str(exc)
                    if type(exc) in {ValueError, TypeError, KeyError}
                    else getattr(exc, "code", type(exc).__name__),
                    "tool_contract": TOOLS,
                    "next_action": "correct_request_or_limit_conclusion",
                }
                extra = []
                state["tool_attempts"][
                    fingerprint(
                        {
                            "role": role,
                            "request": request,
                            "documents": state["documents"],
                            "protocol": PROTOCOL_VERSION,
                        }
                    )
                ] = result
            if result.get("status") == "error":
                signature = fingerprint(
                    {
                        "tool": request.get("name") or request.get("tool"),
                        "error": result.get("detail", result.get("error")),
                        "protocol": PROTOCOL_VERSION,
                    }
                )
                count = state.setdefault("tool_failure_counts", {}).get(signature, 0)
                if signature not in failed_this_turn:
                    count += 1
                    failed_this_turn.add(signature)
                state["tool_failure_counts"][signature] = count
                if count >= 2:
                    from .legacy_service import ResearchBlocked

                    raise ResearchBlocked(
                        [
                            issue(
                                "repeated_session_tool_failure",
                                message=canonical(result),
                            )
                        ]
                    )
            pending.append(
                {
                    "role": "user",
                    "content": canonical({"tool": request, "result": result}),
                }
            )
            pending.extend(extra)
        state["phase"] = "tool_continuation"
        state["tool_continuation"] = {"role": role, "messages": pending}
        state = self._save(state, "tools_completed", {"role": role})
        return self.step(state)

    def _reviewed(self, state):
        output, review = state["draft"], state["response"]
        review = copy.deepcopy(review)
        explicit = {c.get("key") for c in review.get("conclusions", [])}
        inherited = set()
        for verdict in state.get("review", {}).get("conclusions", []):
            key = verdict.get("key")
            if key not in explicit and self._claim_signature(
                output, key
            ) == self._claim_signature(state.get("review_base_draft", {}), key):
                review.setdefault("conclusions", []).append(
                    {**verdict, "inherited_from_round": state.get("review_round")}
                )
                inherited.add(key)
        for finding in state.get("review", {}).get("issues", []):
            affected = set(finding.get("affected_conclusions", [])) & inherited
            if affected:
                review.setdefault("issues", []).append(
                    {**finding, "affected_conclusions": sorted(affected)}
                )
        shared = set(state.get("seen", {}).get("research", [])) & set(
            state.get("seen", {}).get("review", [])
        )
        trusted_pages = [p for ref, p in state["pages"].items() if ref in shared]
        result = evaluate_round(
            output,
            review,
            trusted_pages,
            company_id=state["company"]["id"],
            template=state["company"].get("template", "ordinary"),
            independently_reviewed=True,
        )
        state["last_reviewed"] = result
        # Bind the result to evidence frozen before review, never to a later
        # packet that happened to arrive while the model was running.
        state["reviewed_evidence_hash"] = state.get("review_input_evidence_hash")
        state["phase"] = "advance"
        state["review"] = review
        state["review_round"] = state["round"]
        state = self._save(
            state,
            "round_reviewed",
            {"result": result},
            round_result={
                "output": output,
                "review": review,
                "result": result,
                "pages": trusted_pages,
            },
        )
        return self._render(state)

    @staticmethod
    def _refs(value):
        refs = set()
        if isinstance(value, dict):
            for k, v in value.items():
                if k == "page_refs" and isinstance(v, list):
                    refs.update(x for x in v if isinstance(x, str))
                else:
                    refs.update(PDFResearchWorkflow._refs(v))
        elif isinstance(value, list):
            for v in value:
                refs.update(PDFResearchWorkflow._refs(v))
        return refs

    def _review_input(self, state, output):
        """Originals behind every proposed claim, including tool-read pages."""
        active = {
            state["images"][part["file_id"]]["source_ref"]
            for message in state["contexts"]["review"]
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if isinstance(part, dict)
            and part.get("type") == "file"
            and part.get("file_id") in state["images"]
        }
        prior_keys = {
            c.get("key") for c in state.get("review", {}).get("conclusions", [])
        }
        changed = [
            c
            for c in output.get("conclusions", [])
            if c.get("key") not in prior_keys
            or self._claim_signature(output, c.get("key"))
            != self._claim_signature(state.get("review_base_draft", {}), c.get("key"))
        ]
        required = self._claim_refs({**output, "conclusions": changed}) | set(
            state.get("batch_refs", [])
        )
        # Unused historical working rows remain in SQLite. They must not force
        # all retired page images back into the active context after compaction.
        current = state.get("response", {})
        required |= self._refs(
            {
                key: current.get(key, [])
                for key in ("facts", "estimates", "calculations")
            }
        )
        pages = [
            state["pages"][ref]
            for ref in sorted(required - active)
            if ref in state["pages"] and state["pages"][ref].get("image_path")
        ]
        pending = []
        if pages:
            pending.append(
                self._page_message(state, pages, "independent_review_originals")
            )
        pending.append(
            {
                "role": "user",
                "content": canonical(
                    {
                        "round": state["round"],
                        "research_output": output,
                        "deterministic_calculations": deterministic_calculation_preview(
                            output,
                            list(state["pages"].values()),
                            company_id=state["company"]["id"],
                            template=state["company"].get("template", "ordinary"),
                        ),
                        "changed_conclusions": [c.get("key") for c in changed],
                        "instruction": "审查本轮新内容及改变的结论。未改变内容可沿用已审查结果；发现新反证仍须指出受影响结论并回查原页。",
                        "coverage": state["coverage"],
                        "page_registry": [
                            {
                                k: p.get(k)
                                for k in (
                                    "id",
                                    "company_id",
                                    "period_end",
                                    "published_at",
                                    "page",
                                    "url",
                                    "kind",
                                )
                            }
                            for p in state["pages"].values()
                        ],
                    }
                ),
            }
        )
        return pending

    @classmethod
    def _claim_refs(cls, output):
        records = {
            r["id"]: r
            for field in ("facts", "estimates", "calculations")
            for r in output.get(field, [])
        }
        pages, visited = set(), set()

        def visit(value):
            if isinstance(value, dict):
                pages.update(cls._refs({"page_refs": value.get("page_refs", [])}))
                for key, child in value.items():
                    if key in {
                        "fact_refs",
                        "estimate_refs",
                        "calculation_refs",
                        "peer_assumption_refs",
                        "ref",
                    }:
                        for ref in child if isinstance(child, list) else [child]:
                            if isinstance(ref, str) and ref not in records:
                                root, _, scenario = ref.rpartition(".")
                                if (
                                    scenario in {"low", "base", "high"}
                                    and root in records
                                ):
                                    ref = root
                            if (
                                isinstance(ref, str)
                                and ref in records
                                and ref not in visited
                            ):
                                visited.add(ref)
                                visit(records[ref])
                    elif key != "page_refs":
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(output.get("conclusions", []))
        return pages

    @staticmethod
    def _claim_signature(output, key):
        """An unchanged conclusion may reuse its previous independent verdict."""
        conclusion = next(
            (c for c in output.get("conclusions", []) if c.get("key") == key), None
        )
        if conclusion is None:
            return None
        records = {
            r["id"]: r
            for field in ("facts", "estimates", "calculations")
            for r in output.get(field, [])
        }
        used = {}

        def visit(value):
            if isinstance(value, dict):
                for name, child in value.items():
                    if name in {
                        "fact_refs",
                        "estimate_refs",
                        "calculation_refs",
                        "peer_assumption_refs",
                        "ref",
                    }:
                        for ref in child if isinstance(child, list) else [child]:
                            if isinstance(ref, str) and ref not in records:
                                root, _, scenario = ref.rpartition(".")
                                if (
                                    scenario in {"low", "base", "high"}
                                    and root in records
                                ):
                                    ref = root
                            if (
                                isinstance(ref, str)
                                and ref in records
                                and ref not in used
                            ):
                                used[ref] = records[ref]
                                visit(records[ref])
                    else:
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(conclusion)
        return fingerprint({"conclusion": conclusion, "dependencies": used})

    def _render(self, state):
        from .legacy_reporting import _atomic

        path = self.service.root / "sessions" / (state["id"] + ".html")
        _atomic(
            path,
            render_round_html(
                state["last_reviewed"],
                state["draft"],
                title=state["company"].get("name", state["company"]["id"]),
                usage=self._usage(state),
            ),
        )
        state["report_path"] = str(path.resolve())
        state = self._save(state, "stage_rendered", {"path": str(path.resolve())})
        self.service.report()
        return state

    def _usage(self, state):
        # Per-session receipts, separate from lifetime issuer history.
        events = self.store.events(company_id=state["company"]["id"])
        hashes = {
            e["payload"].get("request_hash")
            for e in events
            if e["kind"] == "model_request"
            and e["payload"].get("session_id") == state["id"]
        }
        receipts = [
            e["payload"].get("usage") or {}
            for e in events
            if e["kind"] in {"model_response", "model_error"}
            and e["payload"].get("request_hash") in hashes
        ]
        return {
            "model_calls": len(hashes),
            "input_tokens": sum(r.get("input_tokens") or 0 for r in receipts),
            "output_tokens": sum(r.get("output_tokens") or 0 for r in receipts),
            "unknown_usage_calls": len(hashes)
            - sum(
                type(r.get("input_tokens")) is int
                and type(r.get("output_tokens")) is int
                for r in receipts
            ),
            "cache_receipts": [
                r["provider_cache_usage"]
                for r in receipts
                if "provider_cache_usage" in r
            ],
            "scope": "session_including_compaction_tools_review_failures",
        }

    def _publish_pending(self, state):
        """Idempotent delivery checkpoint, including restart after staging."""
        valid = release_status(self.store, VERSION)["reports"]
        report = next(
            (r for r in valid if r["version"] == state["publication_version"]), None
        )
        if report is None:
            raise ValueError("staged_report_no_longer_valid")
        self.store.enqueue(
            "research_report_ready",
            {
                "company_id": state["company"]["id"],
                "staging_version": report["version"],
                "report": report["payload"],
            },
        )
        state["phase"] = "complete"
        state = self._save(
            state, "report_notification_queued", {"version": report["version"]}
        )
        self.service.report()
        self.service._deliver()
        return state

    def _advance(self, state):
        result = state["last_reviewed"]
        feedback = []
        if result.get("report_ready"):
            packet = self._packet(state["company"]["id"])
            reviewed_hash = state.get("reviewed_evidence_hash")
            if not reviewed_hash or reviewed_hash != fingerprint(packet):
                from .legacy_service import ResearchBlocked

                raise ResearchBlocked(
                    [
                        issue(
                            "reviewed_evidence_changed_or_not_frozen",
                            message="资料在审查期间或发布前更新，须刷新研究资料并重新独立审查",
                        )
                    ]
                )
            shared = set(state["seen"]["research"]) & set(state["seen"]["review"])
            payload = report_payload(
                state["company"],
                result,
                state["draft"],
                state["review"],
                [p for ref, p in state["pages"].items() if ref in shared],
                self._usage(state),
            )
            payload.update(
                as_of=self.service.today.isoformat(),
                input_hash=fingerprint(packet),
                session_id=state["id"],
            )
            # Never write the old packet back over a concurrently refreshed one.
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE companies SET stage='reviewed' WHERE id=? "
                    "AND input_hash=? AND stage!='excluded'",
                    (state["company"]["id"], reviewed_hash),
                )
            staged = stage_report(self.store, payload)
            state["publication_version"] = staged["version"]
            state["phase"] = "publish_pending"
            state = self._save(state, "report_staged", {"version": staged["version"]})
            return self._publish_pending(state)
        else:
            next_action = state["draft"].get("next", {})
            choice = next_action.get("action", "continue")
            findings = result.get("issues", [])
            deferred_review_tools = state.pop("deferred_review_tools", [])
            if deferred_review_tools and findings:
                choice = "finish"  # Enter bounded correction, not report acceptance.
            if findings:
                feedback = [
                    {
                        "role": "user",
                        "content": canonical(
                            {
                                "independent_review": findings,
                                "review_follow_up_tools": deferred_review_tools,
                                "instruction": "补证、修正受影响部分或限制判断；无需重写其余结论。",
                            }
                        ),
                    }
                ]
            cursor = self._next_unread(state, state["document_id"])
            total = self.reader.page_count(self._doc(state, state["document_id"]))
            if choice == "continue" and cursor <= total:
                state["next_page"] = cursor
                state["phase"] = "read"
            elif choice == "select" and next_action.get("reason"):
                document_id = next_action.get("document_id")
                self._doc(state, document_id)
                state["document_id"] = document_id
                state["next_page"] = self._next_unread(state, document_id)
                if state["next_page"] > self.reader.page_count(
                    self._doc(state, document_id)
                ):
                    raise ValueError("selected_document_already_fully_read")
                state["phase"] = "read"
            elif choice == "finish" and findings:
                # Correction stays in the same session. One targeted response,
                # then another review; same failed output cannot loop forever.
                signature = fingerprint(
                    {
                        "roots": sorted(
                            {
                                i.get("code", "review_blocking")
                                + ":"
                                + ",".join(sorted(i.get("affected_conclusions", [])))
                                for i in findings
                                if i.get("severity") == "blocking"
                            }
                        ),
                        "pages": sorted(state["pages"]),
                        "version": VERSION,
                    }
                )
                attempts = state.setdefault("repair_counts", {}).get(signature, 0)
                if attempts >= 2:
                    state["phase"] = "blocked"
                    state["errors"] = [issue("unchanged_review_failure")]
                else:
                    state["repair_signature"] = signature
                    state["repair_counts"][signature] = attempts + 1
                    state["round"] += 1
                    state["phase"] = "research"
            else:
                state["phase"] = "deferred"
                state["errors"] = [
                    issue(
                        "research_deferred",
                        message=next_action.get("reason", "未形成完整报告"),
                    )
                ]
        if state["phase"] in {"blocked", "deferred"}:
            state["blocked_execution_revision"] = self.execution_revision
            state["blocked_evidence_hash"] = fingerprint(
                {
                    "packet": self._packet(state["company"]["id"]),
                    "documents": self.inventory(state["company"]["id"]),
                }
            )
        if feedback and state["phase"] not in {"blocked", "deferred"}:
            # Commit feedback, its next phase and repair counter together. A
            # restart must not append it again or lose correction priority.
            return self._append(state, "research", feedback)
        return self._save(state, "round_advanced", {"phase": state["phase"]})

    @staticmethod
    def _next_unread(state, document_id):
        """Return the first hole, not the largest page ever reached by a tool."""
        end = 0
        for first, last in sorted(state["coverage"].get(document_id, [])):
            if first > end + 1:
                break
            end = max(end, last)
        return end + 1

    def _record_research(self, state, output):
        """Persist every research delta, including tool turns, before executing tools."""
        pending = state.get("pending_record_output")
        if pending:
            combined = {**pending, **output}
            for field in ("facts", "estimates", "calculations"):
                rows = {r["id"]: r for r in pending.get(field, [])}
                committed = {r["id"] for r in state.get("draft", {}).get(field, [])}
                for record in output.get(field, []):
                    target = record.get("supersedes")
                    chain, seen = [], {record["id"]}
                    while target in rows and target not in committed:
                        if target in seen:
                            raise ValueError("cyclic_pending_correction")
                        seen.add(target)
                        ancestor = rows[target]
                        chain.append(ancestor)
                        target = ancestor.get("supersedes")
                    if chain and record.get("correction_reason"):
                        if target not in committed:
                            raise ValueError("pending_correction_root_missing")
                        state.setdefault("pending_record_ancestors", {})[
                            record["id"]
                        ] = chain
                        for ancestor in chain:
                            rows.pop(ancestor["id"])
                rows.update({r["id"]: r for r in output.get(field, [])})
                combined[field] = list(rows.values())
            conclusions = {c["key"]: c for c in pending.get("conclusions", [])}
            conclusions.update({c["key"]: c for c in output.get("conclusions", [])})
            combined["conclusions"] = list(conclusions.values())
            output = combined

        def replacement_target(record):
            chain = state.get("pending_record_ancestors", {}).get(record["id"])
            return chain[-1].get("supersedes") if chain else record.get("supersedes")

        problems = []
        previous = state.get("draft", {})
        for field in ("facts", "estimates", "calculations"):
            rows = {r["id"]: r for r in previous.get(field, [])}
            for record in output.get(field, []):
                if rows.get(record["id"]) == record:
                    continue
                replaced = replacement_target(record)
                if replaced and (
                    replaced not in rows
                    or not str(record.get("correction_reason") or "").strip()
                ):
                    problems.append(
                        {
                            "field": field,
                            "record_id": record["id"],
                            "supersedes": replaced,
                            "code": "correction_requires_existing_record_and_reason",
                            "existing_record": replaced in rows,
                        }
                    )
                if record["id"] in rows and rows[record["id"]] != record:
                    problems.append(
                        {
                            "field": field,
                            "record_id": record["id"],
                            "code": "immutable_research_record_changed",
                        }
                    )
        if problems:
            root = fingerprint(sorted({p["code"] for p in problems}))
            attempts = state.setdefault("record_contract_repairs", {})
            if attempts.get(root, 0) >= 1:
                raise ValueError("record_contract_repair_exhausted")
            attempts[root] = attempts.get(root, 0) + 1
            state["pending_record_output"] = output
            state["phase"] = "research"
            self.store.event(
                state["company"]["id"],
                "pdf_record_contract_correction",
                {
                    "session_id": state["id"],
                    "round": state["round"],
                    "problems": problems,
                },
            )
            return self._append(
                state,
                "research",
                [
                    {
                        "role": "user",
                        "content": canonical(
                            {
                                "record_contract_errors": problems,
                                "instruction": "仅返回受影响记录的完整修正版，放在原facts/estimates/calculations数组中。其余本轮输出已暂存，无需重写报告。supersedes必须引用已存在旧记录，并独立填写非空correction_reason；不能只把原因写在method中。事实内容不得由程序补造。修正后仍需独立审查。",
                            }
                        ),
                    }
                ],
            )
        # Working facts are durable independently of compressed model text.
        # Updates use new IDs; a changed value cannot overwrite an old fact.
        previous = state.get("draft", {})
        state.setdefault("research_base_draft", copy.deepcopy(previous))
        merged = {**previous, **output}
        merged["tools"] = output.get("tools") or []
        for field in ("facts", "estimates", "calculations"):
            rows = {r["id"]: r for r in previous.get(field, [])}
            for record in output.get(field, []):
                if rows.get(record["id"]) == record:
                    continue
                replaced = replacement_target(record)
                if replaced:
                    if replaced not in rows or not record.get("correction_reason"):
                        raise ValueError(
                            "correction_requires_existing_record_and_reason"
                        )
                    state.setdefault("superseded_records", []).append(
                        {
                            "record": rows.pop(replaced),
                            "replacement": record["id"],
                            "reason": record["correction_reason"],
                            "round": state["round"],
                            "uncommitted_correction_chain": state.get(
                                "pending_record_ancestors", {}
                            ).get(record["id"], []),
                        }
                    )
                if record["id"] in rows and rows[record["id"]] != record:
                    raise ValueError("immutable_research_record_changed")
                rows[record["id"]] = record
            merged[field] = list(rows.values())
        conclusions = {c["key"]: c for c in previous.get("conclusions", [])}
        conclusions.update({c["key"]: c for c in output.get("conclusions", [])})
        merged["conclusions"] = list(conclusions.values())
        merged["coverage"] = {
            "read_pages": state["coverage"],
            "limitations": output.get("coverage", previous.get("coverage", {})).get(
                "limitations", []
            ),
        }
        merged["superseded_records"] = state.get("superseded_records", [])
        state["draft"] = merged
        state["research_pending_tools"] = copy.deepcopy(output.get("tools") or [])
        state.pop("pending_record_output", None)
        state.pop("pending_record_ancestors", None)
        state["phase"] = "research_recorded"
        return self._save(state, "research_records_saved", {"reviewed": False})

    def _continue_research(self, state):
        requests = state.get("research_pending_tools", [])
        if requests:
            return self._tools(state, "research", {"tools": requests})
        state["review_base_draft"] = state.pop("research_base_draft", {})
        state["review_input_evidence_hash"] = fingerprint(
            self._packet(state["company"]["id"])
        )
        state["phase"] = "review"
        return self._append(state, "review", self._review_input(state, state["draft"]))

    def step(self, state):
        if state["phase"] == "publish_pending":
            # Delivery of an already accepted version makes no paid/model calls.
            return self._publish_pending(state)
        self._guard(state["company"]["id"])
        phase = state["phase"]
        if phase == "read":
            return self._read(state)
        if phase in {"research", "review"}:
            return self._respond(state, phase)
        if phase in {"research_returned", "review_returned"}:
            role = state["response_role"]
            output = state["response"]
            if role == "review" and output.get("tools"):
                return self._tools(state, role, output)
            if role == "review":
                return self._reviewed(state)
            state = self._record_research(state, output)
            if state["phase"] != "research_recorded":
                return state
            return self._continue_research(state)
        if phase == "research_recorded":
            return self._continue_research(state)
        if phase == "tool_continuation":
            continuation = state.pop("tool_continuation")
            state["phase"] = continuation["role"]
            return self._append(state, continuation["role"], continuation["messages"])
        if phase == "advance":
            return self._advance(state)
        return state

    def run(self, issuers=None):
        campaign = self.service.acceptance.state()
        issuers = (
            issuers
            or campaign.get("targets")
            or self.settings.get("priority_issuers", [])
        )
        delivery_errors = []
        # A deadline stops inference, not delivery of an already staged report.
        # Recover the durable outbox handoff without opening a research session.
        for issuer in issuers:
            sid = session_id(issuer, campaign.get("id") or "continuous", VERSION)
            pending = self.sessions.load(sid)
            if pending and pending["phase"] == "publish_pending":
                try:
                    self._publish_pending(pending)
                except Exception as exc:
                    self.store.event(
                        issuer,
                        "pdf_delivery_recovery_failed",
                        {
                            "session_id": sid,
                            "error": type(exc).__name__,
                        },
                    )
                    delivery_errors.append(
                        {
                            "company_id": issuer,
                            "code": "pdf_delivery_recovery_failed",
                            "message": "已保存报告的通知恢复失败，保留待发送状态",
                            "error": type(exc).__name__,
                        }
                    )
                    # Keep publish_pending for a later delivery retry.
        check = self.preflight(issuers)
        check["delivery_errors"] = delivery_errors
        if check["errors"]:
            return check
        outcomes = []
        for issuer in issuers:
            state = None
            try:
                state = self.open(issuer)
                while state["phase"] not in {"complete", "deferred", "blocked"}:
                    elapsed = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(
                            campaign.get("started_at") or state["started_at"]
                        )
                    ).total_seconds()
                    if (
                        elapsed >= 1800
                        and not release_status(self.store, VERSION)["valid_count"]
                    ):
                        notices = self.store.events(kind="pdf_first_report_checkpoint")
                        if not any(
                            e["payload"].get("campaign_id") == state["campaign_id"]
                            for e in notices
                        ):
                            checkpoint = {
                                "campaign_id": state["campaign_id"],
                                "company_id": issuer,
                                "phase": state["phase"],
                                "round": state["round"],
                                "errors": state.get("errors", []),
                                "review_issues": (state.get("last_reviewed") or {}).get(
                                    "issues", []
                                ),
                                "usage": self.service.acceptance.state().get(
                                    "usage", {}
                                ),
                                "next_action": "继续当前补证和审查；预算或截止时间不变",
                            }
                            self.store.event(
                                issuer, "pdf_first_report_checkpoint", checkpoint
                            )
                            self.store.enqueue("research_blocked", checkpoint)
                            self.service._deliver()
                    state = self.step(state)
                outcomes.append(
                    {
                        "company_id": issuer,
                        "status": state["phase"],
                        "round": state["round"],
                        "errors": state.get("errors", []),
                    }
                )
            except Exception as exc:
                errors = getattr(
                    exc,
                    "errors",
                    [issue(getattr(exc, "code", type(exc).__name__), message=str(exc))],
                )
                sid = session_id(
                    issuer,
                    self.service.acceptance.id
                    if self.service.acceptance.enabled
                    else "continuous",
                    VERSION,
                )
                # open() may fail after saving a checkpoint, before assigning state.
                # Always recover the durable state, never hide that failure.
                state = self.sessions.load(state["id"] if state else sid)
                if state:
                    if state["phase"] != "blocked":
                        state["failed_phase"] = state["phase"]
                    state["phase"], state["errors"] = "blocked", errors
                    state["blocked_execution_revision"] = self.execution_revision
                    try:
                        state["blocked_evidence_hash"] = fingerprint(
                            {
                                "packet": self._packet(issuer),
                                "documents": self.inventory(issuer),
                            }
                        )
                    except Exception as evidence_error:
                        # A failed source must not suppress the original failure.
                        state["failure_snapshot_error"] = type(evidence_error).__name__
                    self._save(state, "failure", {"errors": errors})
                self.store.event(issuer, "pdf_research_failed", {"errors": errors})
                self.store.enqueue(
                    "research_blocked", {"company_id": issuer, "errors": errors}
                )
                outcomes.append(
                    {"company_id": issuer, "status": "blocked", "errors": errors}
                )
        self.service.report()
        self.service._deliver()
        release = release_status(self.store, VERSION)
        activation = None
        if release["ready_to_promote"]:
            from .research_workflow import ResearchWorkflow

            activation = ResearchWorkflow(self.service).promote(policy_version=VERSION)
            if self.service.acceptance.enabled:
                self.service.acceptance.finish("complete", "three_verified_pdf_reports")
        return {
            "status": "complete"
            if all(x["status"] == "complete" for x in outcomes) and outcomes
            else "incomplete",
            "policy_version": VERSION,
            "delivery_errors": delivery_errors,
            "outcomes": outcomes,
            "release": release,
            "activation": activation,
        }
