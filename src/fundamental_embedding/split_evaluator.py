"""Causal cross-sectional evaluation for split company/pricing models."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from typing import Any

import numpy as np
from scipy.stats import rankdata, spearmanr

from .api import FundamentalPricingDataset, FundamentalPricingSnapshot, MoEConfig
from .exposure import CompanyExposureEncoder, RobustFeatureTransformer
from .moe import WalkForwardMoEEvaluator
from .pricing import create_pricing_models, estimate_realized_factor_prices
from .split_api import (
    APPROVED_PRICING_FEATURES,
    FACTOR_NAMES,
    CompanyExposureBatch,
    SplitPricingConfig,
    SplitPricingEvaluation,
)


def _fit_ridge(x: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    design = np.column_stack((np.ones(len(x)), x))
    penalty = np.eye(design.shape[1], dtype=np.float64) * float(alpha)
    penalty[0, 0] = 0.0
    return np.linalg.pinv(design.T @ design + penalty) @ design.T @ y


def _ridge_predict(x: np.ndarray, coefficient: np.ndarray) -> np.ndarray:
    return np.column_stack((np.ones(len(x)), x)) @ coefficient


def _centered_rank(values: np.ndarray) -> np.ndarray:
    if len(values) <= 1:
        return np.zeros(len(values), dtype=np.float64)
    ranks = rankdata(values, method="average")
    return (ranks - (len(values) + 1.0) / 2.0) / (
        (len(values) - 1.0) / 2.0
    )


def _quarter_rank_targets(
    dates: tuple[date, ...],
    values: np.ndarray,
) -> np.ndarray:
    result = np.zeros(len(values), dtype=np.float64)
    date_array = np.asarray(dates, dtype=object)
    for current_date in sorted(set(dates)):
        selected = date_array == current_date
        result[selected] = _centered_rank(values[selected])
    return result


def _rank_ic(prediction: np.ndarray, target: np.ndarray) -> float | None:
    if len(prediction) < 3 or np.std(prediction) <= 1e-12:
        return None
    value = spearmanr(prediction, target).statistic
    return float(value) if np.isfinite(value) else None


def _bootstrap_mean_ci(
    values: list[float],
    *,
    seed: int = 20260819,
    samples: int = 4000,
) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    array = np.asarray(values, dtype=np.float64)
    generator = np.random.default_rng(seed)
    indices = generator.integers(0, len(array), size=(samples, len(array)))
    means = array[indices].mean(axis=1)
    lower, upper = np.quantile(means, [0.025, 0.975])
    return float(lower), float(upper)


def _cosine(left: np.ndarray, right: np.ndarray) -> float | None:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator <= 1e-12:
        return None
    return float(np.dot(left, right) / denominator)


class SplitPricingEvaluator:
    """Walk forward while keeping company identity separate from factor prices."""

    def __init__(self, config: SplitPricingConfig | None = None):
        self.config = config or SplitPricingConfig()
        self.pricing_models = create_pricing_models(self.config)
        if self.config.candidate_model_id not in self.pricing_models:
            raise ValueError(
                f"unknown candidate pricing model: {self.config.candidate_model_id}"
            )

    def _comparison_contract_hash(self, dataset: FundamentalPricingDataset) -> str:
        payload = {
            "contract": "split-fundamental-pricing-1",
            "feature_names": list(dataset.feature_names),
            "factor_names": list(FACTOR_NAMES),
            "mandatory_baselines": list(self.config.mandatory_baselines),
            "minimum_train_rows": self.config.minimum_train_rows,
            "minimum_train_dates": self.config.minimum_train_dates,
            "objective": "equal_quarter_cross_sectional_rank_ic",
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _legacy_scores(
        self,
        dataset: FundamentalPricingDataset,
    ) -> dict[tuple[date, str], float]:
        legacy = WalkForwardMoEEvaluator(
            MoEConfig(
                ridge_alpha=self.config.raw_ridge_alpha,
                embedding_smoothing_alpha=self.config.exposure_smoothing_alpha,
                minimum_train_rows=self.config.minimum_train_rows,
                minimum_train_dates=self.config.minimum_train_dates,
                winsor_limit=self.config.winsor_limit,
            )
        ).run(dataset)
        return {
            (date.fromisoformat(row["feature_date"]), row["symbol"]): float(
                row["prediction"]
            )
            for row in legacy.predictions
        }

    @staticmethod
    def _rank_matrix(values: np.ndarray) -> np.ndarray:
        result = np.zeros_like(values, dtype=np.float64)
        for index in range(values.shape[1]):
            result[:, index] = _centered_rank(values[:, index])
        return result

    def _stabilize_batch(
        self,
        batch: CompanyExposureBatch,
        initial: dict[str, np.ndarray] | None = None,
    ) -> tuple[CompanyExposureBatch, dict[str, np.ndarray]]:
        previous = {
            symbol: value.copy() for symbol, value in (initial or {}).items()
        }
        stable = np.zeros_like(batch.raw_exposures, dtype=np.float64)
        date_array = np.asarray(batch.feature_dates, dtype=object)
        for current_date in sorted(set(batch.feature_dates)):
            row_indices = np.flatnonzero(date_array == current_date)
            for row_index in row_indices:
                symbol = batch.symbols[row_index]
                raw = batch.raw_exposures[row_index]
                prior = previous.get(symbol)
                value = (
                    raw
                    if prior is None
                    else self.config.exposure_smoothing_alpha * raw
                    + (1.0 - self.config.exposure_smoothing_alpha) * prior
                )
                stable[row_index] = value
                previous[symbol] = value
        ranking = np.zeros_like(stable)
        for current_date in sorted(set(batch.feature_dates)):
            selected = date_array == current_date
            ranking[selected] = self._rank_matrix(stable[selected])
            ranking[selected] *= batch.availability_confidence[selected]
        return CompanyExposureBatch(
            feature_dates=batch.feature_dates,
            symbols=batch.symbols,
            factor_names=batch.factor_names,
            raw_exposures=stable,
            ranking_exposures=ranking,
            availability_confidence=batch.availability_confidence,
            metadata={
                **batch.metadata,
                "smoothed": True,
                "smoothing_alpha": self.config.exposure_smoothing_alpha,
            },
        ).validate(), previous

    def _raw_feature_baselines(
        self,
        dataset: FundamentalPricingDataset,
        train: np.ndarray,
        test: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        feature_index = {
            name: index for index, name in enumerate(dataset.feature_names)
        }
        indices = np.asarray(
            [feature_index[name] for name in APPROVED_PRICING_FEATURES],
            dtype=int,
        )
        transformer = RobustFeatureTransformer(
            APPROVED_PRICING_FEATURES,
            self.config.winsor_limit,
        ).fit(
            dataset.values[train][:, indices],
            dataset.availability_mask[train][:, indices],
        )
        train_x = transformer.transform(
            dataset.values[train][:, indices],
            dataset.availability_mask[train][:, indices],
        )
        test_x = transformer.transform(
            dataset.values[test][:, indices],
            dataset.availability_mask[test][:, indices],
        )
        rank_target = _quarter_rank_targets(
            tuple(np.asarray(dataset.feature_dates, dtype=object)[train]),
            dataset.excess_returns[train],
        )
        rank_coefficient = _fit_ridge(
            train_x, rank_target, self.config.raw_ridge_alpha
        )
        return_coefficient = _fit_ridge(
            train_x,
            dataset.excess_returns[train],
            self.config.raw_ridge_alpha,
        )
        return (
            _ridge_predict(test_x, rank_coefficient),
            _ridge_predict(test_x, return_coefficient),
        )

    @staticmethod
    def _state_row(state) -> dict[str, Any]:
        return {
            "as_of": state.as_of.isoformat(),
            "realized_through": state.realized_through.isoformat(),
            "model_id": state.model_id,
            "factor_names": list(state.factor_names),
            "factor_prices": state.factor_prices.tolist(),
            "uncertainty": state.uncertainty.tolist(),
            "metadata": state.metadata,
        }

    def _outer_predictions(
        self,
        dataset: FundamentalPricingDataset,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        predictions: list[dict[str, Any]] = []
        factor_states: list[dict[str, Any]] = []
        legacy_scores = self._legacy_scores(dataset)
        date_array = np.asarray(dataset.feature_dates, dtype=object)
        all_model_ids = set(self.config.mandatory_baselines)
        all_model_ids.add(self.config.candidate_model_id)

        for test_date in sorted(set(dataset.feature_dates)):
            train = dataset.rows_before(test_date)
            test = dataset.rows_on(test_date)
            train_dates = sorted(set(date_array[train]))
            if (
                int(train.sum()) < self.config.minimum_train_rows
                or len(train_dates) < self.config.minimum_train_dates
                or int(test.sum()) < 2
            ):
                continue
            if any(
                label_end >= test_date
                for label_end, selected in zip(dataset.label_end_dates, train)
                if selected
            ):
                raise RuntimeError("unrealized label entered split pricing training")

            encoder = CompanyExposureEncoder(
                dataset.feature_names, self.config
            ).fit(
                dataset.values[train],
                dataset.availability_mask[train],
            )
            train_batch = encoder.transform(
                tuple(date_array[train]),
                tuple(np.asarray(dataset.symbols, dtype=object)[train]),
                dataset.values[train],
                dataset.availability_mask[train],
            )
            stable_train, final_training_exposure = self._stabilize_batch(
                train_batch
            )
            history = estimate_realized_factor_prices(
                stable_train,
                dataset.excess_returns[train],
                self.config.factor_ridge_alpha,
            )
            test_batch = encoder.transform(
                tuple(date_array[test]),
                tuple(np.asarray(dataset.symbols, dtype=object)[test]),
                dataset.values[test],
                dataset.availability_mask[test],
            )
            stable_test, _ = self._stabilize_batch(
                test_batch, final_training_exposure
            )
            scores, states = self._scores_for_date(
                dataset=dataset,
                train=train,
                test=test,
                test_date=test_date,
                stable_test=stable_test,
                history=history,
                legacy_scores=legacy_scores,
                required_model_ids=all_model_ids,
            )
            factor_states.extend(
                self._state_row(state) for state in states.values()
            )
            predictions.extend(
                self._prediction_rows_for_date(
                    dataset,
                    test,
                    test_batch,
                    stable_test,
                    scores,
                )
            )
        return predictions, factor_states

    def _scores_for_date(
        self,
        *,
        dataset: FundamentalPricingDataset,
        train: np.ndarray,
        test: np.ndarray,
        test_date: date,
        stable_test: CompanyExposureBatch,
        history,
        legacy_scores: dict[tuple[date, str], float],
        required_model_ids: set[str],
    ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
        pricing_model_ids = {
            model_id
            for model_id in required_model_ids
            if model_id in self.pricing_models
        }
        states = {
            model_id: self.pricing_models[model_id].forecast(
                history, test_date
            )
            for model_id in sorted(pricing_model_ids)
        }
        scores = {
            model_id: stable_test.ranking_exposures @ state.factor_prices
            for model_id, state in states.items()
        }
        row_count = int(test.sum())
        scores["zero_score"] = np.zeros(row_count, dtype=np.float64)
        scores["uniform_factor_price"] = np.mean(
            stable_test.ranking_exposures, axis=1
        )
        factor_index = {
            name: index for index, name in enumerate(FACTOR_NAMES)
        }
        scores["quality_growth_static"] = np.mean(
            stable_test.ranking_exposures[
                :,
                [
                    factor_index["quality"],
                    factor_index["growth"],
                ],
            ],
            axis=1,
        )
        rank_ridge, return_ridge = self._raw_feature_baselines(
            dataset, train, test
        )
        scores["single_rank_ridge"] = rank_ridge
        scores["single_return_ridge"] = return_ridge

        row_indices = np.flatnonzero(test)
        legacy = []
        for row_index in row_indices:
            key = (test_date, dataset.symbols[row_index])
            if key not in legacy_scores:
                raise RuntimeError(
                    "legacy baseline does not cover the comparison row "
                    f"{test_date.isoformat()} {dataset.symbols[row_index]}"
                )
            legacy.append(legacy_scores[key])
        scores["legacy_recent_mse_gate"] = np.asarray(
            legacy, dtype=np.float64
        )
        missing = required_model_ids - scores.keys()
        if missing:
            raise RuntimeError(
                "mandatory comparison scores are missing: "
                + ", ".join(sorted(missing))
            )
        return scores, states

    @staticmethod
    def _prediction_rows_for_date(
        dataset: FundamentalPricingDataset,
        test: np.ndarray,
        raw_batch: CompanyExposureBatch,
        stable_batch: CompanyExposureBatch,
        scores: dict[str, np.ndarray],
    ) -> list[dict[str, Any]]:
        rows = []
        for local_index, row_index in enumerate(np.flatnonzero(test)):
            rows.append(
                {
                    "feature_date": dataset.feature_dates[
                        row_index
                    ].isoformat(),
                    "label_end_date": dataset.label_end_dates[
                        row_index
                    ].isoformat(),
                    "symbol": dataset.symbols[row_index],
                    "actual_return": float(dataset.forward_returns[row_index]),
                    "actual_excess_return": float(
                        dataset.excess_returns[row_index]
                    ),
                    "company_exposure_raw": {
                        name: float(raw_batch.raw_exposures[
                            local_index, factor_position
                        ])
                        for factor_position, name in enumerate(FACTOR_NAMES)
                    },
                    "company_exposure_stable": {
                        name: float(stable_batch.raw_exposures[
                            local_index, factor_position
                        ])
                        for factor_position, name in enumerate(FACTOR_NAMES)
                    },
                    "company_exposure_rank": {
                        name: float(stable_batch.ranking_exposures[
                            local_index, factor_position
                        ])
                        for factor_position, name in enumerate(FACTOR_NAMES)
                    },
                    "exposure_confidence": {
                        name: float(stable_batch.availability_confidence[
                            local_index, factor_position
                        ])
                        for factor_position, name in enumerate(FACTOR_NAMES)
                    },
                    "scores": {
                        model_id: float(model_scores[local_index])
                        for model_id, model_scores in scores.items()
                    },
                }
            )
        return rows

    @staticmethod
    def _score_turnover(
        predictions: list[dict[str, Any]],
        model_id: str,
    ) -> float:
        by_date: dict[str, dict[str, float]] = defaultdict(dict)
        for row in predictions:
            by_date[row["feature_date"]][row["symbol"]] = row["scores"][
                model_id
            ]
        changes = []
        prior: dict[str, float] | None = None
        for current in (by_date[item] for item in sorted(by_date)):
            if prior is not None:
                common = sorted(set(prior) & set(current))
                if len(common) >= 2:
                    previous_rank = _centered_rank(
                        np.asarray([prior[symbol] for symbol in common])
                    )
                    current_rank = _centered_rank(
                        np.asarray([current[symbol] for symbol in common])
                    )
                    changes.append(
                        float(np.mean(np.abs(current_rank - previous_rank)) / 2.0)
                    )
            prior = current
        return float(np.mean(changes)) if changes else 0.0

    def _metrics(
        self,
        predictions: list[dict[str, Any]],
        model_id: str,
    ) -> dict[str, Any]:
        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in predictions:
            by_date[row["feature_date"]].append(row)
        quarter_ic: dict[str, float | None] = {}
        spreads = []
        for feature_date, rows in sorted(by_date.items()):
            score = np.asarray(
                [row["scores"][model_id] for row in rows], dtype=np.float64
            )
            target = np.asarray(
                [row["actual_excess_return"] for row in rows],
                dtype=np.float64,
            )
            quarter_ic[feature_date] = _rank_ic(score, target)
            if len(rows) >= 4 and np.std(score) > 1e-12:
                order = np.argsort(score)
                bucket = max(1, len(rows) // 4)
                spreads.append(
                    float(
                        np.mean(target[order[-bucket:]])
                        - np.mean(target[order[:bucket]])
                    )
                )
        valid_ic = [
            float(value) for value in quarter_ic.values() if value is not None
        ]
        mean_ic = float(np.mean(valid_ic)) if valid_ic else None
        ic_std = (
            float(np.std(valid_ic, ddof=1)) if len(valid_ic) > 1 else 0.0
        )
        turnover = self._score_turnover(predictions, model_id)
        selection_score = (
            None
            if mean_ic is None
            else mean_ic
            - self.config.stability_penalty * ic_std
            - self.config.turnover_penalty * turnover
        )
        return {
            "model_id": model_id,
            "row_count": len(predictions),
            "evaluated_quarters": len(by_date),
            "valid_ic_quarters": len(valid_ic),
            "mean_quarterly_rank_ic": mean_ic,
            "quarterly_rank_ic_std": ic_std,
            "positive_ic_quarter_rate": (
                float(np.mean(np.asarray(valid_ic) > 0.0))
                if valid_ic
                else None
            ),
            "mean_top_bottom_quarterly_excess_spread": (
                float(np.mean(spreads)) if spreads else None
            ),
            "score_rank_turnover": turnover,
            "selection_score": selection_score,
            "quarterly_rank_ic": quarter_ic,
        }

    def _paired_comparison(
        self,
        candidate_metrics: dict[str, Any],
        baseline_metrics: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        candidate_by_date = candidate_metrics["quarterly_rank_ic"]
        comparisons = {}
        for model_id, metrics in baseline_metrics.items():
            if model_id == candidate_metrics["model_id"]:
                continue
            baseline_by_date = metrics["quarterly_rank_ic"]
            common_dates = [
                feature_date
                for feature_date in sorted(candidate_by_date)
                if candidate_by_date[feature_date] is not None
                and baseline_by_date.get(feature_date) is not None
            ]
            deltas = [
                float(
                    candidate_by_date[feature_date]
                    - baseline_by_date[feature_date]
                )
                for feature_date in common_dates
            ]
            lower, upper = _bootstrap_mean_ci(deltas)
            comparisons[model_id] = {
                "paired_quarters": len(deltas),
                "mean_delta_rank_ic": (
                    float(np.mean(deltas)) if deltas else None
                ),
                "candidate_win_rate": (
                    float(np.mean(np.asarray(deltas) > 0.0))
                    if deltas
                    else None
                ),
                "bootstrap_95pct_ci": [lower, upper],
                "quarterly_deltas": dict(zip(common_dates, deltas)),
            }
        eligible = [
            (model_id, metrics["selection_score"])
            for model_id, metrics in baseline_metrics.items()
            if model_id != candidate_metrics["model_id"]
            and metrics["selection_score"] is not None
        ]
        strongest = max(eligible, key=lambda item: item[1])[0] if eligible else None
        strongest_selection_score = (
            baseline_metrics[strongest]["selection_score"]
            if strongest is not None
            else None
        )
        return {
            "strongest_baseline": strongest,
            "strongest_baseline_selection_score": strongest_selection_score,
            "candidate_selection_score": candidate_metrics["selection_score"],
            "candidate_selection_delta": (
                candidate_metrics["selection_score"] - strongest_selection_score
                if strongest_selection_score is not None
                and candidate_metrics["selection_score"] is not None
                else None
            ),
            "against_strongest": (
                comparisons.get(strongest) if strongest is not None else None
            ),
            "by_baseline": comparisons,
        }

    @staticmethod
    def _exposure_stability(
        predictions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in predictions:
            by_symbol[row["symbol"]].append(row)
        cosines = []
        raw_changes = []
        stable_changes = []
        for rows in by_symbol.values():
            ordered = sorted(rows, key=lambda item: item["feature_date"])
            for previous, current in zip(ordered, ordered[1:]):
                prior_raw = np.asarray(
                    list(previous["company_exposure_raw"].values())
                )
                current_raw = np.asarray(
                    list(current["company_exposure_raw"].values())
                )
                prior_stable = np.asarray(
                    list(previous["company_exposure_stable"].values())
                )
                current_stable = np.asarray(
                    list(current["company_exposure_stable"].values())
                )
                similarity = _cosine(prior_stable, current_stable)
                if similarity is not None:
                    cosines.append(similarity)
                raw_changes.append(float(np.linalg.norm(current_raw - prior_raw)))
                stable_changes.append(
                    float(np.linalg.norm(current_stable - prior_stable))
                )
        return {
            "adjacent_observations": len(stable_changes),
            "median_stable_exposure_cosine": (
                float(np.median(cosines)) if cosines else None
            ),
            "mean_raw_exposure_change": (
                float(np.mean(raw_changes)) if raw_changes else None
            ),
            "mean_stable_exposure_change": (
                float(np.mean(stable_changes)) if stable_changes else None
            ),
        }

    @staticmethod
    def _exposure_coverage(
        predictions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = {}
        for factor_name in FACTOR_NAMES:
            confidence = np.asarray(
                [
                    row["exposure_confidence"][factor_name]
                    for row in predictions
                ],
                dtype=np.float64,
            )
            result[factor_name] = {
                "mean_confidence": float(np.mean(confidence)),
                "usable_rate": float(np.mean(confidence > 0.0)),
            }
        return result

    def _latest_exposures(
        self,
        dataset: FundamentalPricingDataset,
        snapshot: FundamentalPricingSnapshot | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        if snapshot is None or not snapshot.symbols:
            return [], None
        train = dataset.rows_before(snapshot.feature_date)
        train_dates = sorted({
            item
            for item, selected in zip(dataset.feature_dates, train)
            if selected
        })
        if (
            int(train.sum()) < self.config.minimum_train_rows
            or len(train_dates) < self.config.minimum_train_dates
        ):
            return [], None
        encoder = CompanyExposureEncoder(
            dataset.feature_names, self.config
        ).fit(
            dataset.values[train],
            dataset.availability_mask[train],
        )
        train_batch = encoder.transform(
            tuple(np.asarray(dataset.feature_dates, dtype=object)[train]),
            tuple(np.asarray(dataset.symbols, dtype=object)[train]),
            dataset.values[train],
            dataset.availability_mask[train],
        )
        stable_train, previous = self._stabilize_batch(train_batch)
        history = estimate_realized_factor_prices(
            stable_train,
            dataset.excess_returns[train],
            self.config.factor_ridge_alpha,
        )
        current_raw = encoder.transform(
            (snapshot.feature_date,) * len(snapshot.symbols),
            snapshot.symbols,
            snapshot.values,
            snapshot.availability_mask,
        )
        current_stable, _ = self._stabilize_batch(current_raw, previous)
        state = self.pricing_models[
            self.config.candidate_model_id
        ].forecast(history, snapshot.feature_date)
        rows = []
        scores = current_stable.ranking_exposures @ state.factor_prices
        for row_index, symbol in enumerate(snapshot.symbols):
            rows.append(
                {
                    "feature_date": snapshot.feature_date.isoformat(),
                    "symbol": symbol,
                    "factor_names": list(FACTOR_NAMES),
                    "raw_company_exposure": (
                        current_raw.raw_exposures[row_index].tolist()
                    ),
                    "stable_company_exposure": (
                        current_stable.raw_exposures[row_index].tolist()
                    ),
                    "ranking_company_exposure": (
                        current_stable.ranking_exposures[row_index].tolist()
                    ),
                    "availability_confidence": (
                        current_stable.availability_confidence[
                            row_index
                        ].tolist()
                    ),
                    "market_pricing_model": state.model_id,
                    "market_pricing_state_as_of": state.as_of.isoformat(),
                    "ranking_score": float(scores[row_index]),
                    "company_embedding_contains_market_state": False,
                }
            )
        return rows, self._state_row(state)

    def run(
        self,
        dataset: FundamentalPricingDataset,
        inference_snapshot: FundamentalPricingSnapshot | None = None,
    ) -> SplitPricingEvaluation:
        dataset.validate()
        if inference_snapshot is not None:
            inference_snapshot.validate()
            if inference_snapshot.feature_names != dataset.feature_names:
                raise ValueError(
                    "inference snapshot and dataset features differ"
                )
        predictions, factor_states = self._outer_predictions(dataset)
        if not predictions:
            raise ValueError("no eligible walk-forward comparison rows")

        candidate_metrics = self._metrics(
            predictions, self.config.candidate_model_id
        )
        baseline_metrics = {
            model_id: self._metrics(predictions, model_id)
            for model_id in self.config.mandatory_baselines
        }
        paired = self._paired_comparison(
            candidate_metrics, baseline_metrics
        )
        latest, current_state = self._latest_exposures(
            dataset, inference_snapshot
        )
        if current_state is not None:
            factor_states.append(current_state)

        test_dates = sorted({
            row["feature_date"] for row in predictions
        })
        test_symbols = sorted({row["symbol"] for row in predictions})
        required_scores = set(self.config.mandatory_baselines)
        required_scores.add(self.config.candidate_model_id)
        mandatory_complete = all(
            required_scores <= row["scores"].keys() for row in predictions
        )
        leakage_violations = sum(
            state["realized_through"] >= state["as_of"]
            for state in factor_states
        )
        prices = [
            float(value)
            for state in factor_states
            for value in state["factor_prices"]
        ]
        signed_prices_observed = (
            any(value < 0.0 for value in prices)
            and any(value > 0.0 for value in prices)
        )
        strongest = paired["against_strongest"] or {}
        selection_delta = paired.get("candidate_selection_delta")
        delta = strongest.get("mean_delta_rank_ic")
        win_rate = strongest.get("candidate_win_rate")
        interval = strongest.get("bootstrap_95pct_ci") or [None, None]
        spread = candidate_metrics.get(
            "mean_top_bottom_quarterly_excess_spread"
        )
        production_ready = (
            mandatory_complete
            and leakage_violations == 0
            and len(test_symbols) >= self.config.production_minimum_symbols
            and candidate_metrics["mean_quarterly_rank_ic"] is not None
            and candidate_metrics["mean_quarterly_rank_ic"]
            >= self.config.production_minimum_rank_ic
            and selection_delta is not None
            and selection_delta > 0.0
            and delta is not None
            and delta >= self.config.production_minimum_delta_rank_ic
            and win_rate is not None
            and win_rate >= self.config.production_minimum_win_rate
            and interval[0] is not None
            and interval[0] > 0.0
            and spread is not None
            and spread > 0.0
        )
        return SplitPricingEvaluation(
            contract="split-fundamental-pricing-1",
            dataset={
                **dataset.metadata,
                "row_count": len(dataset.symbols),
                "symbol_count": len(set(dataset.symbols)),
                "feature_date_count": len(set(dataset.feature_dates)),
                "test_row_count": len(predictions),
                "test_symbol_count": len(test_symbols),
                "test_quarter_count": len(test_dates),
                "leakage_violations": leakage_violations,
                "comparison_contract_hash": (
                    self._comparison_contract_hash(dataset)
                ),
            },
            config=asdict(self.config),
            candidate_model_id=self.config.candidate_model_id,
            metrics=candidate_metrics,
            baselines=baseline_metrics,
            paired_comparison=paired,
            stability=self._exposure_stability(predictions),
            exposure_coverage=self._exposure_coverage(predictions),
            factor_price_states=factor_states,
            predictions=predictions,
            latest_company_exposures=latest,
            acceptance={
                "framework_ready": (
                    mandatory_complete and leakage_violations == 0
                ),
                "production_ready": production_ready,
                "mandatory_baselines_complete": mandatory_complete,
                "mandatory_baselines": list(
                    self.config.mandatory_baselines
                ),
                "company_embedding_excludes_market_pricing": True,
                "market_factor_prices_are_signed": True,
                "signed_prices_observed_in_sample": signed_prices_observed,
                "objective": "equal_quarter_cross_sectional_rank_ic",
                "strongest_baseline_must_be_beaten": True,
                "production_minimum_symbols": (
                    self.config.production_minimum_symbols
                ),
                "candidate_beats_strongest_baseline": (
                    selection_delta is not None
                    and selection_delta > 0.0
                    and delta is not None
                    and delta >= self.config.production_minimum_delta_rank_ic
                    and win_rate is not None
                    and win_rate >= self.config.production_minimum_win_rate
                    and interval[0] is not None
                    and interval[0] > 0.0
                ),
            },
        )
