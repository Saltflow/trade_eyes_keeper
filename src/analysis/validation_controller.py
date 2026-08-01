"""Post-selection robustness and isolated-window validation."""

from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np

from .optimizer import (
    StrategyEncoding,
    _evaluate_encoding_wf,
    _partition_window_indexes,
)
from .search_contracts import Candidate, CandidateBatch, ParameterSchema
from .search_interface import Params


@dataclass
class ValidatedSearchResult:
    candidate_id: str
    parameters: dict[str, object]
    ranking_stats: list[object]
    all_stats: list[object]
    validation_stats: list[object]
    objective_score: float
    selection_score: float
    ranking_metrics: dict[str, object]
    gate_results: tuple[dict[str, object], ...] = ()
    purged_window_count: int = 0
    sensitivity: dict[str, object] = field(default_factory=dict)
    universe_robustness: dict[str, object] = field(default_factory=dict)


class ValidationController:
    """The only component allowed to open isolation and holdout windows."""

    def __init__(
        self,
        strategy,
        constraints,
        wf_manager,
        evaluator,
        schema: ParameterSchema,
        ranking_service,
        gate_pipeline,
        all_windows: list,
    ):
        self.strategy = strategy
        self.constraints = constraints
        self.wf_manager = wf_manager
        self.evaluator = evaluator
        self.schema = schema
        self.ranking_service = ranking_service
        self.gate_pipeline = gate_pipeline
        self.all_windows = list(all_windows)
        ranking, purged, validation = _partition_window_indexes(
            self.all_windows, constraints
        )
        self.ranking_indexes = ranking
        self.purged_indexes = purged
        self.validation_indexes = validation
        self.ranking_windows = [self.all_windows[index] for index in ranking]

    def run(self, search_results: list) -> list[ValidatedSearchResult]:
        if not search_results:
            return []
        limit = min(
            len(search_results),
            self.constraints.genetic_search.sensitivity_top_candidates,
        )
        validated = [self._base_result(result) for result in search_results]
        for result in validated[:limit]:
            result.sensitivity = self._sensitivity(result)
            drop = float(result.sensitivity.get("drop", 0.0))
            result.selection_score -= (
                self.constraints.genetic_search.sensitivity_penalty_weight
                * max(drop, 0.0)
            )
            result.sensitivity["selection_score"] = result.selection_score
        validated[:limit] = sorted(
            validated[:limit],
            key=lambda item: (item.selection_score, item.candidate_id),
            reverse=True,
        )
        selected = validated[0]
        selected.universe_robustness = self._universe_robustness(selected)
        worst_drop = float(selected.universe_robustness.get("worst_drop", 0.0))
        if math.isfinite(worst_drop):
            selected.selection_score -= (
                self.constraints.genetic_search.sensitivity_penalty_weight
                * max(worst_drop, 0.0)
            )
        self._evaluate_full_windows(selected)
        return validated

    def _base_result(self, result) -> ValidatedSearchResult:
        return ValidatedSearchResult(
            candidate_id=result.candidate_id,
            parameters=dict(result.parameters),
            ranking_stats=list(result.ranking_stats),
            all_stats=list(result.ranking_stats),
            validation_stats=[],
            objective_score=float(result.objective_score),
            selection_score=float(result.selection_score),
            ranking_metrics=dict(result.ranking_metrics),
            gate_results=tuple(result.gate_results),
            purged_window_count=len(self.purged_indexes),
        )

    def _sensitivity(self, selected: ValidatedSearchResult) -> dict[str, object]:
        perturbations = self.schema.local_perturbations(selected.parameters)
        candidates = []
        for index, perturbation in enumerate(perturbations):
            candidates.append(
                Candidate.create(
                    perturbation,
                    self.schema,
                    "validation/local-perturbation",
                    nonce=f"{selected.candidate_id}:{index}",
                )
            )
        if not candidates:
            return {}
        evaluated = self.ranking_service.evaluate_batch(
            CandidateBatch.from_candidates(candidates, self.schema)
        )
        scores = []
        for score, metrics, raw_feasible in zip(
            evaluated.objective_scores,
            evaluated.raw_metrics,
            evaluated.feasible,
        ):
            decision = self.gate_pipeline.evaluate(metrics)
            if bool(raw_feasible) and decision.feasible:
                scores.append(float(score) - decision.penalty)
            else:
                scores.append(-float("inf"))
        finite = [score for score in scores if math.isfinite(score)]
        worst = min(scores) if scores else -float("inf")
        return {
            "sample_count": len(scores),
            "feasible_sample_count": len(finite),
            "base_score": round(float(selected.selection_score), 6),
            "worst_score": round(float(worst), 6),
            "drop": round(float(selected.selection_score - worst), 6),
            "min_score": round(float(worst), 6),
            "max_score": round(float(max(scores)), 6),
            "local_robustness_passed": bool(finite and worst > 0.0),
        }

    def _encoding(self, selected: ValidatedSearchResult) -> StrategyEncoding:
        params = Params(values=dict(selected.parameters), _engine=self.strategy.name)
        return StrategyEncoding(
            genome=[
                int(params.values[dim.name]) for dim in self.strategy.param_space.dims
            ],
            engine_name=self.strategy.name,
            params=params,
        )

    def _subset_manager(self, indexes: list[int]):
        codes = list(self.wf_manager.stock_codes)
        manager = type("SubsetWalkForwardData", (), {})()
        manager.indicator_matrix = self.wf_manager.indicator_matrix[:, indexes]
        manager.price_matrix = self.wf_manager.price_matrix[:, indexes]
        manager.price_high_matrix = self.wf_manager.price_high_matrix[:, indexes]
        manager.price_low_matrix = self.wf_manager.price_low_matrix[:, indexes]
        manager.stock_codes = [codes[index] for index in indexes]
        manager.dates = self.wf_manager.dates
        manager.benchmark_series = self.wf_manager.benchmark_series
        manager.benchmark_high_series = self.wf_manager.benchmark_high_series
        return manager

    def _universe_robustness(
        self, selected: ValidatedSearchResult
    ) -> dict[str, object]:
        codes = list(getattr(self.wf_manager, "stock_codes", []) or [])
        if not self.ranking_windows or not codes:
            return {}
        encoding = self._encoding(selected)
        reverse = _evaluate_encoding_wf(
            encoding,
            self.strategy,
            self.ranking_windows,
            self.constraints.discrete_search,
            self.constraints,
            self.evaluator,
            self._subset_manager(list(reversed(range(len(codes))))),
            validation_window_count=0,
        )
        order_invariant = reverse is not None and len(reverse[1]) == len(
            selected.ranking_stats
        )
        if order_invariant:
            for left, right in zip(selected.ranking_stats, reverse[1]):
                left_values = (
                    left.strategy_return,
                    left.test_excess_return,
                    left.max_drawdown_pct,
                    left.sharpe_ratio,
                    left.total_trades,
                    left.final_asset,
                )
                right_values = (
                    right.strategy_return,
                    right.test_excess_return,
                    right.max_drawdown_pct,
                    right.sharpe_ratio,
                    right.total_trades,
                    right.final_asset,
                )
                if left_values != right_values:
                    order_invariant = False
                    break

        base_mean = float(
            np.mean([stat.test_excess_return for stat in selected.ranking_stats])
        )
        variants = []
        positive = 0
        drops = []
        if len(codes) == 1:
            variants.append(
                {"removed": codes[0], "mean_excess": base_mean, "not_applicable": True}
            )
            positive = 1
        else:
            for removed_index, removed_code in enumerate(codes):
                indexes = [
                    index for index in range(len(codes)) if index != removed_index
                ]
                result = _evaluate_encoding_wf(
                    encoding,
                    self.strategy,
                    self.ranking_windows,
                    self.constraints.discrete_search,
                    self.constraints,
                    self.evaluator,
                    self._subset_manager(indexes),
                    validation_window_count=0,
                )
                mean_excess = (
                    float(np.mean([stat.test_excess_return for stat in result[1]]))
                    if result is not None and result[1]
                    else -float("inf")
                )
                if mean_excess > 0:
                    positive += 1
                drop = (
                    base_mean - mean_excess
                    if math.isfinite(mean_excess)
                    else float("inf")
                )
                drops.append(drop)
                variants.append(
                    {"removed": removed_code, "mean_excess": mean_excess, "drop": drop}
                )
        required = max(1, int(np.ceil(len(codes) * 0.80)))
        return {
            "symbol_order_invariant": order_invariant,
            "variant_count": len(codes),
            "positive_variant_count": positive,
            "required_positive_variant_count": required,
            "leave_one_out_passed": positive >= required,
            "base_mean_excess": base_mean,
            "worst_drop": max(drops) if drops else 0.0,
            "variants": variants,
        }

    def _evaluate_full_windows(self, selected: ValidatedSearchResult) -> None:
        result = _evaluate_encoding_wf(
            self._encoding(selected),
            self.strategy,
            self.all_windows,
            self.constraints.discrete_search,
            self.constraints,
            self.evaluator,
            self.wf_manager,
        )
        if result is None:
            raise RuntimeError("final full-window evaluation failed")
        all_stats, ranking_stats, validation_stats, score = result
        selected.all_stats = list(all_stats)
        selected.ranking_stats = list(ranking_stats)
        selected.validation_stats = list(validation_stats)
        selected.objective_score = float(score)
