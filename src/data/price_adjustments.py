"""Derive forward-adjusted prices from raw bars and explicit distributions."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import numpy as np
import pandas as pd

from .corporate_action_currency import validate_cash_currency
from .market_history import CorporateAction, PriceHistoryBundle, corporate_action_issues


def _failure_diagnostics(actions: list[CorporateAction]) -> str:
    """Keep bounded HKEX failure context when strict adjustment building rejects.

    The rejected actions will not be persisted, so their URL/reason must reach
    the outer readiness report through the exception. Do not emit unrelated
    metadata or diagnostics for successfully verified actions.
    """
    details: list[str] = []
    omitted = 0
    prefixes = (
        "hkex_cash_evidence_unavailable=",
        "hkex_cash_evidence_conflict=",
        "hkex_cash_evidence_document_budget_exhausted",
    )
    for action in actions:
        if not corporate_action_issues([action]):
            continue
        for diagnostic in action.diagnostics:
            if not diagnostic.startswith(prefixes):
                continue
            detail = f"{action.code} {action.ex_date}: {diagnostic}"
            if len(detail) > 1024:
                detail = detail[:1024] + "...[truncated]"
            if detail in details:
                continue
            if len(details) < 8:
                details.append(detail)
            else:
                omitted += 1
    if omitted:
        details.append(f"additional_hkex_failure_diagnostics_omitted={omitted}")
    return "; ".join(details)


def apply_disclosed_adjustments(
    bundle: PriceHistoryBundle, actions: list[CorporateAction], *, evidence: str
) -> PriceHistoryBundle:
    """Bind a complete disclosure feed and calculate multiplicative qfq.

    On an ex-date, the historical adjustment is (previous close - cash) /
    (previous close * share multiplier). No cash or share change is inferred
    from prices. The caller is responsible for proving the disclosure feed's
    instrument and requested-window coverage; its identity is mandatory.
    """
    if not evidence.strip():
        raise ValueError("independent corporate-action coverage evidence is required")
    issues = corporate_action_issues(actions)
    if issues:
        diagnostics = _failure_diagnostics(actions)
        reason = "; ".join(issues)
        if diagnostics:
            reason += "; hkex_evidence_details=[" + diagnostics + "]"
        raise ValueError(reason)
    if (
        any(
            action.action_type == "in_kind_distribution"
            and action.in_kind_terms is not None
            and action.in_kind_terms.source_adjustment_factor is None
            for action in actions
        )
        and bundle.source == "tencent_raw_qfq"
    ):
        # This provider fetched independent raw/qfq requests with matched
        # sessions and volumes. Preserve that observed signal series; never
        # reverse-engineer cash, share entitlements or taxes from a price gap.
        if not any(
            value.startswith("tencent_window_request:") for value in bundle.diagnostics
        ):
            raise ValueError("in-kind provider adjustment lacks raw/qfq provenance")
        for action in actions:
            if str(action.code) != str(bundle.code):
                raise ValueError("corporate action belongs to a different instrument")
            if action.action_type != "in_kind_distribution":
                validate_cash_currency(action, bundle.currency)
        frame = bundle.prices.copy().sort_values("date").reset_index(drop=True)
        dates = pd.to_datetime(frame["date"], errors="coerce")
        if frame.empty or dates.isna().any() or dates.duplicated().any():
            raise ValueError("provider raw/qfq history needs unique valid dates")
        columns = [
            f"{basis}_{field}"
            for basis in ("raw", "qfq")
            for field in ("open", "high", "low", "close")
        ] + ["qfq_factor"]
        values = frame[columns].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError("provider raw/qfq values must be finite and positive")
        if not np.allclose(
            frame["raw_close"] * frame["qfq_factor"],
            frame["qfq_close"],
            rtol=1e-6,
            atol=1e-12,
        ):
            raise ValueError("provider qfq_close != raw_close * qfq_factor")
        kept = [
            action
            for action in actions
            if dates.iloc[0].date() <= action.ex_date <= dates.iloc[-1].date()
        ]
        identity = [(action.code, action.ex_date) for action in kept]
        if len(identity) != len(set(identity)):
            raise ValueError("ambiguous same-day corporate actions")
        digest = hashlib.sha256(
            frame[["date", *columns]]
            .to_json(
                orient="split",
                date_format="iso",
                double_precision=15,
            )
            .encode("utf-8")
        ).hexdigest()
        return replace(
            bundle,
            prices=frame,
            actions=kept,
            source=bundle.source + "+disclosed_actions",
            diagnostics=[
                value
                for value in bundle.diagnostics
                if value != "corporate_actions_require_independent_disclosures"
            ]
            + [
                "qfq_preserved_from_independent_provider_requests:sha256=" + digest,
                "action_coverage:" + evidence,
            ],
        ).validate()
    for action in actions:
        if action.action_type != "in_kind_distribution":
            validate_cash_currency(action, bundle.currency)
    frame = bundle.prices.copy().sort_values("date").reset_index(drop=True)
    dates = pd.to_datetime(frame["date"]).dt.date
    if frame.empty or dates.duplicated().any():
        raise ValueError("raw history must contain unique trading dates")
    raw = frame[[f"raw_{name}" for name in ("open", "high", "low", "close")]]
    if not np.isfinite(raw.to_numpy(dtype=float)).all() or (raw <= 0).any().any():
        raise ValueError("raw OHLC must be finite and positive")
    multipliers = np.ones(len(frame), dtype=float)
    seen = set()
    kept = []
    for action in sorted(actions, key=lambda a: a.ex_date):
        if str(action.code) != str(bundle.code):
            raise ValueError("corporate action belongs to a different instrument")
        if action.ex_date in seen:
            raise ValueError(f"ambiguous same-day corporate actions: {action.ex_date}")
        seen.add(action.ex_date)
        if action.ex_date > dates.iloc[-1]:
            continue
        if action.ex_date < dates.iloc[0]:
            continue
        kept.append(action)
        before = np.flatnonzero((dates < action.ex_date).to_numpy())
        if not len(before):
            continue
        previous_close = float(frame.loc[before[-1], "raw_close"])
        if action.action_type == "in_kind_distribution":
            terms = action.in_kind_terms
            if (
                terms is None
                or terms.source_adjustment_factor is None
                or terms.adjustment_published_at is None
                or terms.adjustment_published_at > action.ex_date
                or not terms.adjustment_source_url
            ):
                raise ValueError(
                    f"in-kind distribution lacks causal adjustment evidence: "
                    f"{action.code} {action.ex_date}"
                )
            ratio = float(terms.source_adjustment_factor)
        else:
            cash = float(action.cash_per_share or 0.0)
            shares = float(action.share_multiplier or 1.0)
            ratio = (previous_close - cash) / (previous_close * shares)
        if not np.isfinite(ratio) or ratio <= 0:
            raise ValueError(f"distribution exceeds prior close: {action.ex_date}")
        multipliers[before] *= ratio
    for name in ("open", "high", "low", "close"):
        frame[f"qfq_{name}"] = frame[f"raw_{name}"] * multipliers
    frame["qfq_factor"] = multipliers
    return replace(
        bundle,
        prices=frame,
        actions=kept,
        source=bundle.source + "+disclosed_actions",
        diagnostics=[
            item
            for item in bundle.diagnostics
            if item != "corporate_actions_require_independent_disclosures"
        ]
        + ["qfq_derived_from_explicit_cash_and_shares", "action_coverage:" + evidence],
    ).validate()
