"""Derive forward-adjusted prices from raw bars and explicit distributions."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from .market_history import CorporateAction, PriceHistoryBundle, corporate_action_issues


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
        raise ValueError("; ".join(issues))
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
