#!/usr/bin/env python3
"""Run the common context-gate evaluation with a Rank-IC objective."""

from __future__ import annotations

import scripts.evaluate_causal_context_gate as evaluation
from src.fundamental_embedding.causal_rank_context_gate import CausalRankContextGate

if __name__ == "__main__":
    evaluation.CausalContextGate = CausalRankContextGate
    raise SystemExit(evaluation.main())
