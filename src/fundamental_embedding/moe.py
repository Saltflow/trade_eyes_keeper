"""Causal style-expert mixture and walk-forward embedding evaluation."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np
from scipy.stats import spearmanr

from .api import (
    EXPERT_FEATURES,
    EmbeddingEvaluation,
    FundamentalPricingDataset,
    FundamentalPricingSnapshot,
    MoEConfig,
)


def _fit_ridge(x: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    design = np.column_stack([np.ones(len(x), dtype=np.float64), x])
    penalty = np.eye(design.shape[1], dtype=np.float64) * float(alpha)
    penalty[0, 0] = 0.0
    matrix = design.T @ design + penalty
    target = design.T @ y
    try:
        return np.linalg.solve(matrix, target)
    except np.linalg.LinAlgError:
        return np.linalg.pinv(matrix) @ target


def _ridge_predict(coefficient: np.ndarray, x: np.ndarray) -> np.ndarray:
    return coefficient[0] + x @ coefficient[1:]


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - np.max(values)
    exponent = np.exp(np.clip(shifted, -50.0, 50.0))
    total = float(exponent.sum())
    if total <= 0 or not np.isfinite(total):
        return np.full(len(values), 1.0 / len(values))
    return exponent / total


def _cosine(left: np.ndarray, right: np.ndarray) -> float | None:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator <= 1e-12:
        return None
    return float(np.dot(left, right) / denominator)


def _js_divergence(left: np.ndarray, right: np.ndarray) -> float:
    midpoint = (left + right) / 2.0

    def kl(first, second):
        valid = first > 0
        return float(np.sum(first[valid] * np.log(first[valid] / second[valid])))

    return 0.5 * kl(left, midpoint) + 0.5 * kl(right, midpoint)


class CausalPricingMoE:
    """Four linear experts with a recency-weighted causal performance gate."""

    def __init__(self, feature_names: tuple[str, ...], config: MoEConfig):
        self.feature_names = feature_names
        self.config = config
        self.expert_names = tuple(EXPERT_FEATURES)
        self.feature_median: np.ndarray | None = None
        self.feature_scale: np.ndarray | None = None
        self.group_indices: dict[str, np.ndarray] = {}
        self.expert_coefficients: dict[str, np.ndarray] = {}
        self.gate_weights = np.full(
            len(self.expert_names), 1.0 / len(self.expert_names)
        )
        self.gate_losses = np.full(len(self.expert_names), np.nan)
        self.signal_mean = np.zeros(len(self.expert_names))
        self.signal_scale = np.ones(len(self.expert_names))
        self.baseline_coefficient: np.ndarray | None = None

    def _fit_scaler(self, values: np.ndarray, mask: np.ndarray) -> None:
        median = np.zeros(values.shape[1], dtype=np.float64)
        scale = np.ones(values.shape[1], dtype=np.float64)
        for index in range(values.shape[1]):
            observed = values[mask[:, index], index]
            observed = observed[np.isfinite(observed)]
            if len(observed) == 0:
                continue
            median[index] = float(np.median(observed))
            q25, q75 = np.quantile(observed, [0.25, 0.75])
            robust = float(q75 - q25)
            if robust <= 1e-12:
                mad = float(np.median(np.abs(observed - median[index])))
                robust = mad * 1.4826
            scale[index] = robust if robust > 1e-12 else 1.0
        self.feature_median = median
        self.feature_scale = scale

    def transform(self, values: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if self.feature_median is None or self.feature_scale is None:
            raise RuntimeError("model is not fitted")
        filled = np.where(mask, values, self.feature_median)
        result = (filled - self.feature_median) / self.feature_scale
        return np.clip(
            result,
            -self.config.winsor_limit,
            self.config.winsor_limit,
        )

    def _expert_matrix(
        self,
        transformed: np.ndarray,
        coefficients: dict[str, np.ndarray] | None = None,
    ) -> np.ndarray:
        selected = coefficients or self.expert_coefficients
        columns = []
        for name in self.expert_names:
            columns.append(
                _ridge_predict(
                    selected[name],
                    transformed[:, self.group_indices[name]],
                )
            )
        return np.column_stack(columns)

    def _fit_gate(
        self,
        transformed: np.ndarray,
        target: np.ndarray,
        dates: np.ndarray,
    ) -> None:
        unique_dates = np.asarray(sorted(set(dates)))
        validation_count = max(
            2,
            int(np.ceil(
                len(unique_dates) * self.config.gate_validation_fraction
            )),
        )
        if len(unique_dates) - validation_count < 3:
            return
        validation_dates = unique_dates[-validation_count:]
        early = ~np.isin(dates, validation_dates)
        validation = ~early
        if early.sum() < len(self.expert_names) * 3 or validation.sum() < 4:
            return
        coefficients = {}
        for name in self.expert_names:
            indices = self.group_indices[name]
            coefficients[name] = _fit_ridge(
                transformed[early][:, indices],
                target[early],
                self.config.ridge_alpha,
            )
        predictions = self._expert_matrix(
            transformed[validation], coefficients
        )
        validation_row_dates = dates[validation]
        date_rank = {item: index for index, item in enumerate(validation_dates)}
        ages = np.asarray([
            len(validation_dates) - 1 - date_rank[item]
            for item in validation_row_dates
        ])
        recency = np.power(
            0.5,
            ages / max(self.config.gate_half_life_quarters, 1e-6),
        )
        recency /= recency.sum()
        losses = np.sum(
            recency[:, None]
            * np.square(predictions - target[validation, None]),
            axis=0,
        )
        target_variance = max(float(np.var(target[validation])), 1e-8)
        normalized = losses / target_variance
        weights = _softmax(-normalized / max(self.config.gate_temperature, 1e-6))
        floor = min(
            self.config.gate_floor,
            0.99 / len(self.expert_names),
        )
        weights = np.maximum(weights, floor)
        self.gate_weights = weights / weights.sum()
        self.gate_losses = normalized

    def fit(
        self,
        values: np.ndarray,
        mask: np.ndarray,
        target: np.ndarray,
        dates: np.ndarray,
    ) -> "CausalPricingMoE":
        if len(values) < self.config.minimum_train_rows:
            raise ValueError("not enough training rows for the pricing MOE")
        self._fit_scaler(values, mask)
        transformed = self.transform(values, mask)
        feature_index = {name: index for index, name in enumerate(self.feature_names)}
        self.group_indices = {
            name: np.asarray([feature_index[item] for item in features], dtype=int)
            for name, features in EXPERT_FEATURES.items()
        }
        for name in self.expert_names:
            indices = self.group_indices[name]
            self.expert_coefficients[name] = _fit_ridge(
                transformed[:, indices], target, self.config.ridge_alpha
            )
        self.baseline_coefficient = _fit_ridge(
            transformed, target, self.config.ridge_alpha
        )
        self._fit_gate(transformed, target, dates)
        signals = self._expert_matrix(transformed)
        self.signal_mean = np.mean(signals, axis=0)
        self.signal_scale = np.std(signals, axis=0)
        self.signal_scale[self.signal_scale <= 1e-8] = 1.0
        return self

    @property
    def embedding_names(self) -> tuple[str, ...]:
        return tuple(
            [f"expert_signal:{name}" for name in self.expert_names]
            + [f"market_gate:{name}" for name in self.expert_names]
            + [f"priced_signal:{name}" for name in self.expert_names]
        )

    def predict(
        self, values: np.ndarray, mask: np.ndarray
    ) -> dict[str, np.ndarray]:
        transformed = self.transform(values, mask)
        signals = self._expert_matrix(transformed)
        prediction = signals @ self.gate_weights
        uniform_prediction = np.mean(signals, axis=1)
        if self.baseline_coefficient is None:
            raise RuntimeError("model is not fitted")
        ridge_prediction = _ridge_predict(
            self.baseline_coefficient, transformed
        )
        normalized_signals = np.clip(
            (signals - self.signal_mean) / self.signal_scale,
            -4.0,
            4.0,
        )
        gates = np.repeat(
            self.gate_weights[None, :], len(values), axis=0
        )
        embedding = np.column_stack([
            normalized_signals,
            gates,
            normalized_signals * gates,
        ])
        return {
            "prediction": prediction,
            "uniform_prediction": uniform_prediction,
            "ridge_prediction": ridge_prediction,
            "expert_signals": signals,
            "embedding": embedding,
            "gate_weights": gates,
        }


def _rank_ic(actual: np.ndarray, predicted: np.ndarray) -> float | None:
    if len(actual) < 3 or np.std(actual) <= 1e-12 or np.std(predicted) <= 1e-12:
        return None
    value = spearmanr(actual, predicted).statistic
    return float(value) if np.isfinite(value) else None


def _bootstrap_mean_ci(values: list[float], seed: int = 20260818) -> list[float] | None:
    if len(values) < 3:
        return None
    rng = np.random.default_rng(seed)
    array = np.asarray(values, dtype=np.float64)
    samples = rng.choice(array, size=(1000, len(array)), replace=True).mean(axis=1)
    return [float(item) for item in np.quantile(samples, [0.025, 0.975])]


class WalkForwardMoEEvaluator:
    """Evaluate embeddings with strict label-realization cutoffs."""

    def __init__(self, config: MoEConfig | None = None):
        self.config = config or MoEConfig()

    def run(
        self,
        dataset: FundamentalPricingDataset,
        inference_snapshot: FundamentalPricingSnapshot | None = None,
        current_memberships: dict[str, tuple[str, ...]] | None = None,
    ) -> EmbeddingEvaluation:
        dataset.validate()
        if inference_snapshot is not None:
            inference_snapshot.validate()
            if inference_snapshot.feature_names != dataset.feature_names:
                raise ValueError(
                    "inference snapshot and training dataset features differ"
                )
        predictions: list[dict[str, Any]] = []
        previous_embeddings: dict[str, np.ndarray] = {}
        embedding_names: tuple[str, ...] = ()
        dates = sorted(set(dataset.feature_dates))
        leakage_violations = 0
        for test_date in dates:
            train = dataset.rows_before(test_date)
            test = dataset.rows_on(test_date)
            train_dates = sorted({
                item
                for item, selected in zip(dataset.feature_dates, train)
                if selected
            })
            if (
                train.sum() < self.config.minimum_train_rows
                or len(train_dates) < self.config.minimum_train_dates
                or test.sum() < 2
            ):
                continue
            if any(
                end >= test_date
                for end, selected in zip(dataset.label_end_dates, train)
                if selected
            ):
                leakage_violations += 1
                continue
            model = CausalPricingMoE(dataset.feature_names, self.config).fit(
                dataset.values[train],
                dataset.availability_mask[train],
                dataset.excess_returns[train],
                np.asarray(dataset.feature_dates, dtype=object)[train],
            )
            result = model.predict(
                dataset.values[test], dataset.availability_mask[test]
            )
            embedding_names = model.embedding_names
            row_indices = np.flatnonzero(test)
            for local_index, row_index in enumerate(row_indices):
                symbol = dataset.symbols[row_index]
                raw_embedding = result["embedding"][local_index]
                previous = previous_embeddings.get(symbol)
                stable_embedding = (
                    raw_embedding
                    if previous is None
                    else self.config.embedding_smoothing_alpha * raw_embedding
                    + (1.0 - self.config.embedding_smoothing_alpha) * previous
                )
                previous_embeddings[symbol] = stable_embedding
                predictions.append(
                    {
                        "feature_date": test_date.isoformat(),
                        "label_end_date": dataset.label_end_dates[
                            row_index
                        ].isoformat(),
                        "symbol": symbol,
                        "actual_excess_return": float(
                            dataset.excess_returns[row_index]
                        ),
                        "actual_return": float(dataset.forward_returns[row_index]),
                        "prediction": float(result["prediction"][local_index]),
                        "uniform_prediction": float(
                            result["uniform_prediction"][local_index]
                        ),
                        "ridge_prediction": float(
                            result["ridge_prediction"][local_index]
                        ),
                        "expert_signals": {
                            name: float(result["expert_signals"][local_index, pos])
                            for pos, name in enumerate(model.expert_names)
                        },
                        "gate_weights": {
                            name: float(model.gate_weights[pos])
                            for pos, name in enumerate(model.expert_names)
                        },
                        "raw_embedding": raw_embedding.tolist(),
                        "stable_embedding": stable_embedding.tolist(),
                    }
                )

        metrics, baselines = self._prediction_metrics(predictions)
        cohort_metrics = self._current_cohort_metrics(
            predictions,
            current_memberships or {},
        )
        metrics["current_membership_cohorts"] = cohort_metrics
        stability = self._stability(predictions)
        expert_diagnostics = self._expert_diagnostics(predictions)
        feature_coverage = {
            name: {
                "filled": int(dataset.availability_mask[:, index].sum()),
                "total": len(dataset.symbols),
                "fill_rate": float(
                    dataset.availability_mask[:, index].mean()
                ),
            }
            for index, name in enumerate(dataset.feature_names)
        }
        feature_index = {
            name: index for index, name in enumerate(dataset.feature_names)
        }
        expert_coverage = {}
        for expert_name, feature_names in EXPERT_FEATURES.items():
            indices = [feature_index[name] for name in feature_names]
            required_fields = max(2, (len(indices) + 1) // 2)
            usable = (
                dataset.availability_mask[:, indices].sum(axis=1)
                >= required_fields
            )
            expert_coverage[expert_name] = {
                "required_fields": required_fields,
                "feature_count": len(indices),
                "usable_rows": int(usable.sum()),
                "usable_rate": float(usable.mean()),
            }
        test_dates = sorted({item["feature_date"] for item in predictions})
        test_symbols = sorted({item["symbol"] for item in predictions})
        test_rows_per_quarter = [
            sum(item["feature_date"] == test_date for item in predictions)
            for test_date in test_dates
        ]
        median_test_rows = (
            float(np.median(test_rows_per_quarter))
            if test_rows_per_quarter
            else 0.0
        )
        mean_ic = metrics.get("mean_quarterly_rank_ic")
        ridge_ic = baselines["single_ridge"].get("mean_quarterly_rank_ic")
        uniform_metrics = baselines["uniform_expert_mix"]
        uniform_ic = uniform_metrics.get("mean_quarterly_rank_ic")
        moe_mse = metrics.get("mse")
        uniform_mse = uniform_metrics.get("mse")
        stable_signal_cosine = stability.get(
            "stable_signal_median_cosine"
        )
        gate_value_added = (
            (
                mean_ic is not None
                and uniform_ic is not None
                and mean_ic >= uniform_ic + 0.01
            )
            or (
                moe_mse is not None
                and uniform_mse is not None
                and moe_mse <= uniform_mse * 0.98
            )
        )
        cohort_ics = [
            item["mean_quarterly_rank_ic"]
            for item in cohort_metrics.values()
            if item.get("mean_quarterly_rank_ic") is not None
            and item.get("median_rows_per_quarter", 0.0) >= 30
            and item.get("evaluated_quarters", 0) >= 12
        ]
        cohort_robust = (
            len(cohort_ics) >= 3
            and min(cohort_ics) >= -0.03
            and sum(value > 0.0 for value in cohort_ics) >= 2
        )
        framework_ready = (
            leakage_violations == 0
            and len(test_dates) >= 6
            and len(test_symbols) >= 5
            and stable_signal_cosine is not None
            and stable_signal_cosine >= 0.80
        )
        production_ready = (
            framework_ready
            and len(test_symbols) >= 100
            and len(test_dates) >= 12
            and median_test_rows >= 100
            and min(
                item["usable_rate"] for item in expert_coverage.values()
            ) >= 0.60
            and mean_ic is not None
            and mean_ic >= 0.03
            and metrics.get("positive_ic_quarter_rate", 0.0) >= 0.55
            and metrics.get(
                "mean_top_bottom_quarterly_excess_spread", 0.0
            ) > 0.0
            and gate_value_added
            and cohort_robust
            and (
                ridge_ic is None
                or mean_ic >= ridge_ic - 0.01
            )
        )
        latest_date = max(test_dates) if test_dates else None
        latest_embeddings = [
            {
                "feature_date": item["feature_date"],
                "symbol": item["symbol"],
                "embedding_names": embedding_names,
                "embedding": item["stable_embedding"],
                "gate_weights": item["gate_weights"],
                "prediction": item["prediction"],
                "source": "walk_forward_evaluation",
            }
            for item in predictions
            if item["feature_date"] == latest_date
        ]
        inference_metadata: dict[str, Any] = {
            "requested": inference_snapshot is not None,
            "generated": False,
        }
        if inference_snapshot is not None:
            final_train = dataset.rows_before(inference_snapshot.feature_date)
            final_train_dates = sorted({
                item
                for item, selected in zip(dataset.feature_dates, final_train)
                if selected
            })
            inference_metadata.update({
                "feature_date": inference_snapshot.feature_date.isoformat(),
                "row_count": len(inference_snapshot.symbols),
                "training_row_count": int(final_train.sum()),
                "training_quarter_count": len(final_train_dates),
            })
            if (
                final_train.sum() >= self.config.minimum_train_rows
                and len(final_train_dates) >= self.config.minimum_train_dates
                and len(inference_snapshot.symbols) > 0
            ):
                final_model = CausalPricingMoE(
                    dataset.feature_names, self.config
                ).fit(
                    dataset.values[final_train],
                    dataset.availability_mask[final_train],
                    dataset.excess_returns[final_train],
                    np.asarray(dataset.feature_dates, dtype=object)[final_train],
                )
                current = final_model.predict(
                    inference_snapshot.values,
                    inference_snapshot.availability_mask,
                )
                embedding_names = final_model.embedding_names
                latest_embeddings = []
                for row_index, symbol in enumerate(
                    inference_snapshot.symbols
                ):
                    raw_embedding = current["embedding"][row_index]
                    previous = previous_embeddings.get(symbol)
                    stable_embedding = (
                        raw_embedding
                        if previous is None
                        else self.config.embedding_smoothing_alpha
                        * raw_embedding
                        + (1.0 - self.config.embedding_smoothing_alpha)
                        * previous
                    )
                    latest_embeddings.append({
                        "feature_date": (
                            inference_snapshot.feature_date.isoformat()
                        ),
                        "symbol": symbol,
                        "embedding_names": embedding_names,
                        "embedding": stable_embedding.tolist(),
                        "raw_embedding": raw_embedding.tolist(),
                        "gate_weights": {
                            name: float(final_model.gate_weights[position])
                            for position, name in enumerate(
                                final_model.expert_names
                            )
                        },
                        "prediction": float(
                            current["prediction"][row_index]
                        ),
                        "source": "current_unlabelled_snapshot",
                    })
                inference_metadata["generated"] = True
        return EmbeddingEvaluation(
            contract="causal-fundamental-pricing-moe-2",
            dataset={
                **dataset.metadata,
                "row_count": len(dataset.symbols),
                "symbol_count": len(set(dataset.symbols)),
                "feature_date_count": len(set(dataset.feature_dates)),
                "test_symbol_count": len(test_symbols),
                "test_quarter_count": len(test_dates),
                "median_test_rows_per_quarter": median_test_rows,
                "leakage_violations": leakage_violations,
                "current_inference": inference_metadata,
            },
            config=asdict(self.config),
            metrics=metrics,
            baselines=baselines,
            stability=stability,
            expert_diagnostics={
                **expert_diagnostics,
                "expert_usable_coverage": expert_coverage,
            },
            feature_coverage=feature_coverage,
            predictions=predictions,
            latest_embeddings=latest_embeddings,
            acceptance={
                "framework_ready": framework_ready,
                "production_ready": production_ready,
                "production_requires_at_least_symbols": 100,
                "production_requires_at_least_test_quarters": 12,
                "production_median_rows_per_quarter_floor": 100,
                "production_expert_usable_rate_floor": 0.60,
                "production_mean_rank_ic_floor": 0.03,
                "production_positive_ic_quarter_rate_floor": 0.55,
                "production_gate_value_added": gate_value_added,
                "production_current_cohort_robust": cohort_robust,
                "current_cohort_rule": (
                    "At least three current-membership cohorts with >=30 "
                    "rows/quarter and >=12 quarters; worst IC >= -0.03 and "
                    "at least two cohorts have positive IC. Membership is "
                    "diagnostic only and is never a model feature."
                ),
                "gate_value_added_rule": (
                    "MOE rank IC >= uniform expert mix + 0.01 OR "
                    "MOE MSE <= uniform expert mix * 0.98"
                ),
                "stable_signal_median_cosine_floor": 0.80,
                "note": (
                    "Local small-universe predictive metrics are diagnostic; "
                    "production acceptance waits for the 807-company panel."
                ),
            },
        )

    @staticmethod
    def _prediction_metrics(
        predictions: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        by_date: dict[str, list[dict[str, Any]]] = {}
        for item in predictions:
            by_date.setdefault(item["feature_date"], []).append(item)

        def summarize(key: str) -> dict[str, Any]:
            actual = np.asarray([
                item["actual_excess_return"] for item in predictions
            ])
            predicted = np.asarray([item[key] for item in predictions])
            quarter_ics = []
            spreads = []
            for items in by_date.values():
                y = np.asarray([item["actual_excess_return"] for item in items])
                p = np.asarray([item[key] for item in items])
                ic = _rank_ic(y, p)
                if ic is not None:
                    quarter_ics.append(ic)
                count = max(1, len(items) // 4)
                order = np.argsort(p)
                spreads.append(float(np.mean(y[order[-count:]]) - np.mean(y[order[:count]])))
            return {
                "mse": float(np.mean(np.square(predicted - actual)))
                if len(actual) else None,
                "mae": float(np.mean(np.abs(predicted - actual)))
                if len(actual) else None,
                "directional_accuracy": float(np.mean(
                    np.sign(predicted) == np.sign(actual)
                )) if len(actual) else None,
                "mean_quarterly_rank_ic": float(np.mean(quarter_ics))
                if quarter_ics else None,
                "median_quarterly_rank_ic": float(np.median(quarter_ics))
                if quarter_ics else None,
                "positive_ic_quarter_rate": float(np.mean(
                    np.asarray(quarter_ics) > 0
                )) if quarter_ics else None,
                "rank_ic_95pct_bootstrap_ci": _bootstrap_mean_ci(quarter_ics),
                "mean_top_bottom_quarterly_excess_spread": float(np.mean(spreads))
                if spreads else None,
                "evaluated_rows": len(actual),
                "evaluated_quarters": len(quarter_ics),
            }

        moe = summarize("prediction")
        return moe, {
            "uniform_expert_mix": summarize("uniform_prediction"),
            "single_ridge": summarize("ridge_prediction"),
            "zero_excess_return": {
                "mse": float(np.mean(np.square([
                    item["actual_excess_return"] for item in predictions
                ]))) if predictions else None,
                "mean_quarterly_rank_ic": None,
            },
        }

    @classmethod
    def _current_cohort_metrics(
        cls,
        predictions: list[dict[str, Any]],
        memberships: dict[str, tuple[str, ...]],
    ) -> dict[str, Any]:
        """Score current index cohorts without exposing membership to the model."""

        cohorts = sorted({
            cohort
            for values in memberships.values()
            for cohort in values
        })
        result = {}
        for cohort in cohorts:
            selected = [
                item
                for item in predictions
                if cohort in memberships.get(item["symbol"], ())
            ]
            if not selected:
                continue
            summary, _ = cls._prediction_metrics(selected)
            counts: dict[str, int] = {}
            for item in selected:
                counts[item["feature_date"]] = (
                    counts.get(item["feature_date"], 0) + 1
                )
            summary["median_rows_per_quarter"] = float(
                np.median(list(counts.values()))
            )
            summary["membership_semantics"] = (
                "current-membership diagnostic; never a historical feature"
            )
            result[cohort] = summary
        return result

    @staticmethod
    def _stability(predictions: list[dict[str, Any]]) -> dict[str, Any]:
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for item in predictions:
            by_symbol.setdefault(item["symbol"], []).append(item)
        raw_cosines = []
        stable_cosines = []
        raw_turnover = []
        stable_turnover = []
        raw_signal_cosines = []
        stable_signal_cosines = []
        raw_signal_turnover = []
        stable_signal_turnover = []
        for items in by_symbol.values():
            items.sort(key=lambda item: item["feature_date"])
            for previous, current in zip(items, items[1:]):
                raw_left = np.asarray(previous["raw_embedding"])
                raw_right = np.asarray(current["raw_embedding"])
                stable_left = np.asarray(previous["stable_embedding"])
                stable_right = np.asarray(current["stable_embedding"])
                raw_cosine = _cosine(raw_left, raw_right)
                stable_cosine = _cosine(stable_left, stable_right)
                if raw_cosine is not None:
                    raw_cosines.append(raw_cosine)
                if stable_cosine is not None:
                    stable_cosines.append(stable_cosine)
                scale = max(1.0, np.sqrt(len(raw_left)))
                raw_turnover.append(float(np.linalg.norm(raw_right - raw_left) / scale))
                stable_turnover.append(float(
                    np.linalg.norm(stable_right - stable_left) / scale
                ))
                expert_count = len(EXPERT_FEATURES)
                raw_signal_left = raw_left[:expert_count]
                raw_signal_right = raw_right[:expert_count]
                stable_signal_left = stable_left[:expert_count]
                stable_signal_right = stable_right[:expert_count]
                raw_signal_cosine = _cosine(
                    raw_signal_left, raw_signal_right
                )
                stable_signal_cosine = _cosine(
                    stable_signal_left, stable_signal_right
                )
                if raw_signal_cosine is not None:
                    raw_signal_cosines.append(raw_signal_cosine)
                if stable_signal_cosine is not None:
                    stable_signal_cosines.append(stable_signal_cosine)
                signal_scale = max(1.0, np.sqrt(expert_count))
                raw_signal_turnover.append(float(
                    np.linalg.norm(raw_signal_right - raw_signal_left)
                    / signal_scale
                ))
                stable_signal_turnover.append(float(
                    np.linalg.norm(stable_signal_right - stable_signal_left)
                    / signal_scale
                ))
        return {
            "raw_median_cosine": float(np.median(raw_cosines))
            if raw_cosines else None,
            "stable_median_cosine": float(np.median(stable_cosines))
            if stable_cosines else None,
            "raw_median_l2_turnover": float(np.median(raw_turnover))
            if raw_turnover else None,
            "stable_median_l2_turnover": float(np.median(stable_turnover))
            if stable_turnover else None,
            "stable_p90_l2_turnover": float(np.quantile(stable_turnover, 0.90))
            if stable_turnover else None,
            "raw_signal_median_cosine": float(
                np.median(raw_signal_cosines)
            ) if raw_signal_cosines else None,
            "stable_signal_median_cosine": float(
                np.median(stable_signal_cosines)
            ) if stable_signal_cosines else None,
            "raw_signal_median_l2_turnover": float(
                np.median(raw_signal_turnover)
            ) if raw_signal_turnover else None,
            "stable_signal_median_l2_turnover": float(
                np.median(stable_signal_turnover)
            ) if stable_signal_turnover else None,
            "stable_signal_p90_l2_turnover": float(
                np.quantile(stable_signal_turnover, 0.90)
            ) if stable_signal_turnover else None,
            "comparison_count": len(stable_turnover),
        }

    @staticmethod
    def _expert_diagnostics(
        predictions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        gates_by_date: dict[str, np.ndarray] = {}
        for item in predictions:
            gates_by_date[item["feature_date"]] = np.asarray(
                list(item["gate_weights"].values()), dtype=np.float64
            )
        if not gates_by_date:
            return {}
        dates = sorted(gates_by_date)
        matrix = np.vstack([gates_by_date[item] for item in dates])
        names = tuple(predictions[0]["gate_weights"])
        entropy = -np.sum(matrix * np.log(np.clip(matrix, 1e-12, 1.0)), axis=1)
        js = [
            _js_divergence(gates_by_date[left], gates_by_date[right])
            for left, right in zip(dates, dates[1:])
        ]
        return {
            "expert_names": names,
            "mean_gate_weights": {
                name: float(matrix[:, index].mean())
                for index, name in enumerate(names)
            },
            "latest_gate_weights": {
                name: float(matrix[-1, index])
                for index, name in enumerate(names)
            },
            "mean_effective_expert_count": float(np.mean(np.exp(entropy))),
            "mean_max_gate_weight": float(np.mean(np.max(matrix, axis=1))),
            "mean_quarterly_gate_js_divergence": float(np.mean(js)) if js else 0.0,
            "quarter_count": len(dates),
        }
