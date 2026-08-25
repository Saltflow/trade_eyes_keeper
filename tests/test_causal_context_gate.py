from datetime import date, timedelta

import numpy as np

from src.fundamental_embedding.causal_context_gate import CausalContextGate


def test_context_gate_freezes_training_only_weights_and_predicts_two_experts():
    dates = np.asarray(
        [date(2022, 1, 1) + timedelta(days=90 * i) for i in range(6) for _ in range(8)],
        dtype=object,
    )
    rng = np.random.default_rng(11)
    base = rng.normal(size=(len(dates), 3))
    context = rng.normal(size=(len(dates), 2))
    target = base[:, 0] + rng.normal(scale=0.1, size=len(dates))
    model = CausalContextGate().fit(
        base,
        np.ones_like(base, dtype=bool),
        context,
        np.ones_like(context, dtype=bool),
        target,
        dates,
    )
    result = model.predict(base[:3], np.ones((3, 3), dtype=bool), context[:3], np.ones((3, 2), dtype=bool))
    assert result["gated"].shape == (3,)
    assert np.isclose(model.gate_weights.sum(), 1.0)
    assert len(model.gate_training_dates) >= 2
    assert model.diagnostics()["gate_uses_only_training_labels"] is True
