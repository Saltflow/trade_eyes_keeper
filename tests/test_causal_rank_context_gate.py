from datetime import date, timedelta

import numpy as np

from src.fundamental_embedding.causal_rank_context_gate import CausalRankContextGate


def test_rank_gate_prefers_the_expert_with_better_cross_sectional_ordering():
    dates = np.asarray(
        [date(2022, 1, 1) + timedelta(days=90 * i) for i in range(8) for _ in range(12)],
        dtype=object,
    )
    rng = np.random.default_rng(17)
    base = rng.normal(size=(len(dates), 2))
    context = rng.normal(size=(len(dates), 2))
    target = context[:, 0] + rng.normal(scale=0.05, size=len(dates))
    model = CausalRankContextGate().fit(
        base, np.ones_like(base, dtype=bool), context,
        np.ones_like(context, dtype=bool), target, dates,
    )
    assert model.gate_weights[1] > model.gate_weights[0]
    assert model.diagnostics()["gate_objective"] == "mean_validation_cross_sectional_rank_ic"
