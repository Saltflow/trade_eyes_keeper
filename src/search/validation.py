"""Post-selection robustness and isolated-window validation."""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from .gates import aggregate_ranking_metrics
from .workflow import _evaluate_params_wf, _partition_window_indexes
from .contracts import Candidate, CandidateBatch, ParameterSchema
from ..strategy import Params


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
    search_metadata: dict[str, object] = field(default_factory=dict)


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
            if self.constraints.local_sensitivity.enabled:
                result.sensitivity = self._sensitivity(result)
            else:
                result.sensitivity = {
                    "enabled": False,
                    "local_robustness_passed": True,
                }
            drop = float(result.sensitivity.get("drop", 0.0))
            result.selection_score -= (
                self.constraints.genetic_search.sensitivity_penalty_weight
                * max(drop, 0.0)
            )
            result.sensitivity["selection_score"] = result.selection_score
        validated[:limit] = sorted(
            validated[:limit],
            key=lambda item: (
                bool(item.sensitivity.get("local_robustness_passed")),
                item.selection_score,
            ),
            reverse=True,
        )
        universe_config = self.constraints.universe_robustness
        if universe_config.enabled:
            universe_limit = min(len(validated), universe_config.finalist_count)
            for result in validated[:universe_limit]:
                result.universe_robustness = self._universe_robustness(result)
                worst_drop = float(
                    result.universe_robustness.get("worst_drop", float("inf"))
                )
                if math.isfinite(worst_drop):
                    result.selection_score -= universe_config.penalty_weight * max(
                        worst_drop, 0.0
                    )
                else:
                    result.selection_score = -float("inf")
                result.universe_robustness["selection_score"] = result.selection_score
            validated[:universe_limit] = sorted(
                validated[:universe_limit],
                key=lambda item: (
                    bool(item.universe_robustness.get("passed")),
                    math.isfinite(item.selection_score),
                    item.selection_score,
                ),
                reverse=True,
            )
        else:
            validated[0].universe_robustness = {
                "enabled": False,
                "passed": True,
                "activation_required": universe_config.activation_required,
            }
        selected = validated[0]
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
        scores: list[float] = []
        feasible_scores: list[float] = []
        for score, metrics, raw_feasible in zip(
            evaluated.objective_scores,
            evaluated.raw_metrics,
            evaluated.feasible,
        ):
            decision = self.gate_pipeline.evaluate(metrics)
            if bool(raw_feasible) and decision.feasible:
                adjusted = float(score) - decision.penalty
                scores.append(adjusted)
                if math.isfinite(adjusted):
                    feasible_scores.append(adjusted)
            else:
                scores.append(-float("inf"))
        sample_count = len(scores)
        feasible_count = len(feasible_scores)
        feasible_ratio = feasible_count / sample_count if sample_count else 0.0
        worst = min(feasible_scores) if feasible_scores else -float("inf")
        best = max(feasible_scores) if feasible_scores else -float("inf")
        base_score = float(selected.selection_score)
        drop = max(base_score - worst, 0.0) if math.isfinite(worst) else float("inf")
        passed = bool(
            feasible_scores
            and feasible_ratio
            >= self.constraints.local_sensitivity.minimum_feasible_ratio
        )
        return {
            "enabled": True,
            "config": self.constraints.local_sensitivity.to_contract(),
            "sample_count": sample_count,
            "feasible_sample_count": feasible_count,
            "infeasible_sample_count": sample_count - feasible_count,
            "feasible_ratio": round(float(feasible_ratio), 6),
            "base_score": round(base_score, 6),
            "worst_score": round(float(worst), 6),
            "worst_feasible_score": round(float(worst), 6),
            "drop": round(float(drop), 6),
            "min_score": round(float(worst), 6),
            "max_score": round(float(best), 6),
            "local_robustness_passed": passed,
        }

    def _params(self, selected: ValidatedSearchResult) -> Params:
        return Params(values=dict(selected.parameters), _engine=self.strategy.name)

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
        manager.market_group = getattr(self.wf_manager, "market_group", "a_share")
        manager.market_data_enricher = getattr(
            self.wf_manager, "market_data_enricher", None
        )
        return manager

    def _universe_robustness(
        self, selected: ValidatedSearchResult
    ) -> dict[str, object]:
        codes = list(getattr(self.wf_manager, "stock_codes", []) or [])
        config = self.constraints.universe_robustness
        if not self.ranking_windows or not codes:
            return {}
        params = self._params(selected)
        reverse = _evaluate_params_wf(
            params,
            self.strategy,
            self.ranking_windows,
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

        base_metrics = aggregate_ranking_metrics(
            selected.ranking_stats,
            selected.objective_score,
            self.constraints.walk_forward.ranking_weights(len(selected.ranking_stats)),
            self.constraints.benchmark_codes,
        )
        base_mean = float(base_metrics["mean_majority_benchmark_excess"])
        variants = []
        positive = 0
        drops = []
        if len(codes) == 1:
            variants.append(
                {
                    "removed": codes[0],
                    "mean_majority_excess": base_mean,
                    "not_applicable": True,
                    "passed": True,
                }
            )
            positive = 1
        else:
            for removed_index, removed_code in enumerate(codes):
                indexes = [
                    index for index in range(len(codes)) if index != removed_index
                ]
                result = _evaluate_params_wf(
                    params,
                    self.strategy,
                    self.ranking_windows,
                    self.constraints,
                    self.evaluator,
                    self._subset_manager(indexes),
                    validation_window_count=0,
                )
                metrics = (
                    aggregate_ranking_metrics(
                        result[1],
                        result[3],
                        self.constraints.walk_forward.ranking_weights(len(result[1])),
                        self.constraints.benchmark_codes,
                    )
                    if result is not None and result[1]
                    else {}
                )
                mean_excess = float(
                    metrics.get("mean_majority_benchmark_excess", -float("inf"))
                )
                passed = bool(
                    math.isfinite(mean_excess)
                    and mean_excess > config.minimum_mean_majority_excess
                )
                if passed:
                    positive += 1
                drop = (
                    base_mean - mean_excess
                    if math.isfinite(base_mean) and math.isfinite(mean_excess)
                    else float("inf")
                )
                drops.append(drop)
                gate = self.gate_pipeline.evaluate(metrics) if metrics else None
                variants.append(
                    {
                        "removed": removed_code,
                        "mean_majority_excess": mean_excess,
                        "drop": drop,
                        "passed": passed,
                        "gate_feasible": bool(gate and gate.feasible),
                    }
                )
        required = config.required_positive_variants(len(codes))
        leave_one_out_passed = positive >= required
        passed = bool(
            leave_one_out_passed
            and (order_invariant or not config.require_order_invariance)
        )
        return {
            "enabled": True,
            "config": config.to_contract(),
            "passed": passed,
            "symbol_order_invariant": order_invariant,
            "variant_count": len(codes),
            "positive_variant_count": positive,
            "required_positive_variant_count": required,
            "leave_one_out_passed": leave_one_out_passed,
            "base_mean_majority_excess": base_mean,
            "worst_drop": max(drops) if drops else 0.0,
            "variants": variants,
        }

    def _evaluate_full_windows(self, selected: ValidatedSearchResult) -> None:
        result = _evaluate_params_wf(
            self._params(selected),
            self.strategy,
            self.all_windows,
            self.constraints,
            self.evaluator,
            self.wf_manager,
            include_candidate_diagnostics=True,
        )
        if result is None:
            raise RuntimeError("final full-window evaluation failed")
        all_stats, ranking_stats, validation_stats, score = result
        selected.all_stats = list(all_stats)
        selected.ranking_stats = list(ranking_stats)
        selected.validation_stats = list(validation_stats)
        selected.objective_score = float(score)
