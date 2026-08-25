"""Company-conditional latent experts without predefined peer labels."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.stats import rankdata, spearmanr

from .api import FundamentalPricingDataset, FundamentalPricingSnapshot
from .exposure import RobustFeatureTransformer


VALUATION_FEATURES = (
    "earnings_yield",
    "book_yield",
    "fcf_yield",
    "dividend_yield",
)

OPERATING_FEATURES = (
    "roe_ttm",
    "net_margin",
    "adjusted_margin",
    "fcf_margin",
    "cash_conversion",
    "capex_intensity",
    "revenue_yoy",
    "revenue_qoq",
    "net_income_yoy",
    "net_income_qoq",
    "revenue_cagr_3y",
    "net_income_cagr_3y",
    "revenue_growth_stability",
    "income_growth_stability",
)


@dataclass(frozen=True)
class LatentPeerMoEConfig:
    expert_count: int = 4
    ridge_alpha: float = 8.0
    gate_l2: float = 0.02
    gate_learning_rate: float = 0.08
    gate_steps: int = 120
    em_iterations: int = 35
    restarts: int = 4
    responsibility_temperature: float = 0.35
    uniform_responsibility_mix: float = 0.04
    valuation_loss_weight: float = 0.60
    alpha_loss_weight: float = 1.0
    winsor_limit: float = 4.0
    minimum_train_rows: int = 48
    minimum_train_dates: int = 8
    random_seed: int = 20260819


def _softmax_rows(values: np.ndarray) -> np.ndarray:
    shifted = values - np.max(values, axis=1, keepdims=True)
    exponent = np.exp(np.clip(shifted, -50.0, 50.0))
    return exponent / np.maximum(exponent.sum(axis=1, keepdims=True), 1e-12)


def _rank_by_date(
    dates: tuple[date, ...],
    values: np.ndarray,
    mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    result = np.zeros_like(values, dtype=np.float64)
    available = (
        np.ones_like(values, dtype=bool) if mask is None else mask.copy()
    )
    date_array = np.asarray(dates, dtype=object)
    if values.ndim == 1:
        result = result[:, None]
        available = available[:, None]
        values = values[:, None]
    for current_date in sorted(set(dates)):
        selected_date = date_array == current_date
        for column in range(values.shape[1]):
            selected = (
                selected_date
                & available[:, column]
                & np.isfinite(values[:, column])
            )
            count = int(selected.sum())
            if count <= 1:
                available[selected_date, column] = False
                continue
            ranks = rankdata(values[selected, column], method="average")
            result[selected, column] = (
                ranks - (count + 1.0) / 2.0
            ) / ((count - 1.0) / 2.0)
    return result, available


def _weighted_ridge(
    x: np.ndarray,
    y: np.ndarray,
    weight: np.ndarray,
    alpha: float,
) -> np.ndarray:
    design = np.column_stack((np.ones(len(x)), x))
    weighted = design * weight[:, None]
    penalty = np.eye(design.shape[1], dtype=np.float64) * alpha
    penalty[0, 0] = 0.0
    return np.linalg.pinv(design.T @ weighted + penalty) @ (
        weighted.T @ y
    )


def _predict_ridge(x: np.ndarray, coefficient: np.ndarray) -> np.ndarray:
    return coefficient[0] + x @ coefficient[1:]


def _rank_ic(prediction: np.ndarray, target: np.ndarray) -> float | None:
    if len(prediction) < 3 or np.std(prediction) <= 1e-12:
        return None
    value = spearmanr(prediction, target).statistic
    return float(value) if np.isfinite(value) else None


class LatentPeerMoE:
    """Mixture of linear valuation/alpha experts with a row-dependent gate."""

    def __init__(
        self,
        feature_names: tuple[str, ...],
        config: LatentPeerMoEConfig | None = None,
    ):
        self.feature_names = feature_names
        self.config = config or LatentPeerMoEConfig()
        index = {name: position for position, name in enumerate(feature_names)}
        self.operating_indices = np.asarray(
            [index[name] for name in OPERATING_FEATURES], dtype=int
        )
        self.valuation_indices = np.asarray(
            [index[name] for name in VALUATION_FEATURES], dtype=int
        )
        self.operating_scaler = RobustFeatureTransformer(
            OPERATING_FEATURES, self.config.winsor_limit
        )
        self.valuation_scaler = RobustFeatureTransformer(
            VALUATION_FEATURES, self.config.winsor_limit
        )
        self.gate_coefficient: np.ndarray | None = None
        self.valuation_coefficients: np.ndarray | None = None
        self.alpha_coefficients: np.ndarray | None = None
        self.baseline_alpha_coefficient: np.ndarray | None = None
        self.baseline_valuation_coefficients: np.ndarray | None = None
        self.training_responsibilities: np.ndarray | None = None

    def _features(
        self,
        values: np.ndarray,
        mask: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        operating_mask = mask[:, self.operating_indices]
        operating = self.operating_scaler.transform(
            values[:, self.operating_indices], operating_mask
        )
        valuation_mask = mask[:, self.valuation_indices]
        valuation = self.valuation_scaler.transform(
            values[:, self.valuation_indices], valuation_mask
        )
        alpha = np.column_stack((
            operating,
            valuation,
            operating_mask.astype(np.float64),
            valuation_mask.astype(np.float64),
        ))
        return operating, alpha, valuation_mask

    def _fit_gate(self, x: np.ndarray, target: np.ndarray) -> np.ndarray:
        design = np.column_stack((np.ones(len(x)), x))
        coefficient = self.gate_coefficient
        if coefficient is None:
            coefficient = np.zeros(
                (design.shape[1], self.config.expert_count),
                dtype=np.float64,
            )
        for _ in range(self.config.gate_steps):
            probability = _softmax_rows(design @ coefficient)
            gradient = design.T @ (probability - target) / len(x)
            gradient += self.config.gate_l2 * coefficient
            gradient[0] -= self.config.gate_l2 * coefficient[0]
            coefficient -= self.config.gate_learning_rate * gradient
        return coefficient

    def _fit_experts(
        self,
        valuation_x: np.ndarray,
        alpha_x: np.ndarray,
        valuation_target: np.ndarray,
        valuation_mask: np.ndarray,
        alpha_target: np.ndarray,
        responsibility: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        experts = self.config.expert_count
        valuation = np.zeros(
            (
                experts,
                valuation_x.shape[1] + 1,
                valuation_target.shape[1],
            )
        )
        alpha = np.zeros((experts, alpha_x.shape[1] + 1))
        for expert in range(experts):
            weight = responsibility[:, expert]
            alpha[expert] = _weighted_ridge(
                alpha_x,
                alpha_target,
                weight,
                self.config.ridge_alpha,
            )
            for output in range(valuation_target.shape[1]):
                observed_weight = weight * valuation_mask[:, output]
                if observed_weight.sum() <= 1e-8:
                    continue
                valuation[expert, :, output] = _weighted_ridge(
                    valuation_x,
                    valuation_target[:, output],
                    observed_weight,
                    self.config.ridge_alpha,
                )
        return valuation, alpha

    @staticmethod
    def _expert_predictions(
        valuation_x: np.ndarray,
        alpha_x: np.ndarray,
        valuation_coefficient: np.ndarray,
        alpha_coefficient: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        valuation = np.stack([
            np.column_stack([
                _predict_ridge(
                    valuation_x, coefficient[:, output]
                )
                for output in range(coefficient.shape[1])
            ])
            for coefficient in valuation_coefficient
        ], axis=1)
        alpha = np.column_stack([
            _predict_ridge(alpha_x, coefficient)
            for coefficient in alpha_coefficient
        ])
        return valuation, alpha

    def _loss(
        self,
        valuation_prediction: np.ndarray,
        alpha_prediction: np.ndarray,
        valuation_target: np.ndarray,
        valuation_mask: np.ndarray,
        alpha_target: np.ndarray,
    ) -> np.ndarray:
        valuation_error = (
            np.square(
                valuation_prediction - valuation_target[:, None, :]
            )
            * valuation_mask[:, None, :]
        )
        valuation_count = np.maximum(
            valuation_mask.sum(axis=1, keepdims=True), 1
        )
        valuation_loss = valuation_error.sum(axis=2) / valuation_count
        alpha_loss = np.square(
            alpha_prediction - alpha_target[:, None]
        )
        return (
            self.config.valuation_loss_weight * valuation_loss
            + self.config.alpha_loss_weight * alpha_loss
        )

    def fit(
        self,
        values: np.ndarray,
        mask: np.ndarray,
        feature_dates: tuple[date, ...],
        alpha_target: np.ndarray,
    ) -> "LatentPeerMoE":
        self.operating_scaler.fit(
            values[:, self.operating_indices],
            mask[:, self.operating_indices],
        )
        self.valuation_scaler.fit(
            values[:, self.valuation_indices],
            mask[:, self.valuation_indices],
        )
        operating_x, alpha_x, valuation_mask = self._features(values, mask)
        valuation_target, valuation_mask = _rank_by_date(
            feature_dates,
            values[:, self.valuation_indices],
            valuation_mask,
        )
        ranked_alpha, _ = _rank_by_date(feature_dates, alpha_target)
        ranked_alpha = ranked_alpha[:, 0]
        generator = np.random.default_rng(self.config.random_seed)
        best: tuple[float, Any] | None = None
        for _ in range(self.config.restarts):
            random_gate = generator.normal(
                scale=0.15,
                size=(operating_x.shape[1] + 1, self.config.expert_count),
            )
            self.gate_coefficient = random_gate
            responsibility = _softmax_rows(
                np.column_stack((np.ones(len(operating_x)), operating_x))
                @ random_gate
            )
            for _ in range(self.config.em_iterations):
                valuation_coef, alpha_coef = self._fit_experts(
                    operating_x,
                    alpha_x,
                    valuation_target,
                    valuation_mask,
                    ranked_alpha,
                    responsibility,
                )
                valuation_prediction, alpha_prediction = (
                    self._expert_predictions(
                        operating_x,
                        alpha_x,
                        valuation_coef,
                        alpha_coef,
                    )
                )
                loss = self._loss(
                    valuation_prediction,
                    alpha_prediction,
                    valuation_target,
                    valuation_mask,
                    ranked_alpha,
                )
                gate = _softmax_rows(
                    np.column_stack((np.ones(len(operating_x)), operating_x))
                    @ self.gate_coefficient
                )
                posterior = _softmax_rows(
                    np.log(np.maximum(gate, 1e-12))
                    - loss / self.config.responsibility_temperature
                )
                mix = self.config.uniform_responsibility_mix
                responsibility = (
                    (1.0 - mix) * posterior
                    + mix / self.config.expert_count
                )
                self.gate_coefficient = self._fit_gate(
                    operating_x, responsibility
                )
            score = float(np.mean(np.sum(responsibility * loss, axis=1)))
            load = responsibility.mean(axis=0)
            score += float(np.maximum(0.05 - load, 0.0).sum())
            if best is None or score < best[0]:
                best = (
                    score,
                    (
                        self.gate_coefficient.copy(),
                        valuation_coef.copy(),
                        alpha_coef.copy(),
                        responsibility.copy(),
                    ),
                )
        if best is None:
            raise RuntimeError("latent peer MOE failed to initialize")
        (
            self.gate_coefficient,
            self.valuation_coefficients,
            self.alpha_coefficients,
            self.training_responsibilities,
        ) = best[1]
        self.baseline_alpha_coefficient = _weighted_ridge(
            alpha_x,
            ranked_alpha,
            np.ones(len(alpha_x)),
            self.config.ridge_alpha,
        )
        self.baseline_valuation_coefficients = np.zeros(
            (operating_x.shape[1] + 1, valuation_target.shape[1]),
            dtype=np.float64,
        )
        for output in range(valuation_target.shape[1]):
            self.baseline_valuation_coefficients[:, output] = (
                _weighted_ridge(
                    operating_x,
                    valuation_target[:, output],
                    valuation_mask[:, output].astype(np.float64),
                    self.config.ridge_alpha,
                )
            )
        return self

    def align_to(self, reference: "LatentPeerMoE") -> None:
        if (
            self.alpha_coefficients is None
            or reference.alpha_coefficients is None
        ):
            return
        current = self.alpha_coefficients[:, 1:]
        prior = reference.alpha_coefficients[:, 1:]
        distance = np.linalg.norm(
            current[:, None, :] - prior[None, :, :], axis=2
        )
        rows, columns = linear_sum_assignment(distance)
        order = rows[np.argsort(columns)]
        self.gate_coefficient = self.gate_coefficient[:, order]
        self.valuation_coefficients = self.valuation_coefficients[order]
        self.alpha_coefficients = self.alpha_coefficients[order]
        self.training_responsibilities = (
            self.training_responsibilities[:, order]
        )

    def predict(
        self,
        values: np.ndarray,
        mask: np.ndarray,
    ) -> dict[str, np.ndarray]:
        if (
            self.gate_coefficient is None
            or self.valuation_coefficients is None
            or self.alpha_coefficients is None
            or self.baseline_alpha_coefficient is None
            or self.baseline_valuation_coefficients is None
        ):
            raise RuntimeError("latent peer MOE is not fitted")
        operating_x, alpha_x, _ = self._features(values, mask)
        design = np.column_stack((np.ones(len(operating_x)), operating_x))
        gate = _softmax_rows(design @ self.gate_coefficient)
        valuation_experts, alpha_experts = self._expert_predictions(
            operating_x,
            alpha_x,
            self.valuation_coefficients,
            self.alpha_coefficients,
        )
        return {
            "gate": gate,
            "valuation_prediction": np.sum(
                valuation_experts * gate[:, :, None], axis=1
            ),
            "ridge_valuation_prediction": np.column_stack([
                _predict_ridge(
                    operating_x,
                    self.baseline_valuation_coefficients[:, output],
                )
                for output in range(
                    self.baseline_valuation_coefficients.shape[1]
                )
            ]),
            "alpha_prediction": np.sum(alpha_experts * gate, axis=1),
            "uniform_alpha_prediction": np.mean(alpha_experts, axis=1),
            "global_gate_alpha_prediction": (
                alpha_experts
                @ self.training_responsibilities.mean(axis=0)
            ),
            "ridge_alpha_prediction": _predict_ridge(
                alpha_x, self.baseline_alpha_coefficient
            ),
            "expert_alpha_prediction": alpha_experts,
        }

    def diagnostics(self) -> dict[str, Any]:
        responsibility = self.training_responsibilities
        load = responsibility.mean(axis=0)
        entropy = -np.sum(
            responsibility * np.log(np.maximum(responsibility, 1e-12)),
            axis=1,
        )
        gate_features = ("intercept", *OPERATING_FEATURES)
        summaries = []
        for expert in range(self.config.expert_count):
            coefficients = self.gate_coefficient[:, expert]
            top = np.argsort(np.abs(coefficients[1:]))[::-1][:5] + 1
            summaries.append({
                "expert": expert,
                "load": float(load[expert]),
                "top_gate_features": [
                    {
                        "feature": gate_features[index],
                        "coefficient": float(coefficients[index]),
                    }
                    for index in top
                ],
            })
        return {
            "expert_load": load.tolist(),
            "minimum_expert_load": float(load.min()),
            "mean_effective_experts": float(np.mean(np.exp(entropy))),
            "collapsed": bool(load.min() < 0.05),
            "experts": summaries,
        }


class LatentPeerWalkForwardEvaluator:
    def __init__(self, config: LatentPeerMoEConfig | None = None):
        self.config = config or LatentPeerMoEConfig()

    @staticmethod
    def _metrics(
        predictions: list[dict[str, Any]],
        score_name: str,
    ) -> dict[str, Any]:
        dates = sorted({row["feature_date"] for row in predictions})
        quarterly = {}
        spreads = []
        for feature_date in dates:
            rows = [
                row for row in predictions
                if row["feature_date"] == feature_date
            ]
            score = np.asarray([row[score_name] for row in rows])
            target = np.asarray([row["actual_excess_return"] for row in rows])
            quarterly[feature_date] = _rank_ic(score, target)
            if len(rows) >= 4:
                order = np.argsort(score)
                count = max(1, len(rows) // 4)
                spreads.append(float(
                    target[order[-count:]].mean()
                    - target[order[:count]].mean()
                ))
        valid = [value for value in quarterly.values() if value is not None]
        return {
            "mean_quarterly_rank_ic": (
                float(np.mean(valid)) if valid else None
            ),
            "quarterly_rank_ic_std": (
                float(np.std(valid, ddof=1)) if len(valid) > 1 else 0.0
            ),
            "positive_ic_quarter_rate": (
                float(np.mean(np.asarray(valid) > 0.0)) if valid else None
            ),
            "mean_top_bottom_spread": (
                float(np.mean(spreads)) if spreads else None
            ),
            "quarterly_rank_ic": quarterly,
        }

    @staticmethod
    def _peers(
        symbols: tuple[str, ...],
        gate: np.ndarray,
        count: int = 5,
    ) -> list[dict[str, Any]]:
        rows = []
        for index, symbol in enumerate(symbols):
            distance = np.sum(
                gate[index]
                * np.log(
                    np.maximum(gate[index], 1e-12)
                    / np.maximum(gate, 1e-12)
                ),
                axis=1,
            )
            nearest = [
                position
                for position in np.argsort(distance)
                if position != index
            ][:count]
            rows.append({
                "symbol": symbol,
                "gate": gate[index].tolist(),
                "peers": [
                    {
                        "symbol": symbols[position],
                        "kl_distance": float(distance[position]),
                    }
                    for position in nearest
                ],
            })
        return rows

    @staticmethod
    def _valuation_metrics(
        predictions: list[dict[str, Any]],
        prediction_field: str = "valuation_prediction",
    ) -> dict[str, Any]:
        result = {}
        for output, feature_name in enumerate(VALUATION_FEATURES):
            quarterly = {}
            for feature_date in sorted({
                row["feature_date"] for row in predictions
            }):
                rows = [
                    row for row in predictions
                    if row["feature_date"] == feature_date
                    and row["valuation_available"][output]
                ]
                quarterly[feature_date] = (
                    _rank_ic(
                        np.asarray([
                            row[prediction_field][output]
                            for row in rows
                        ]),
                        np.asarray([
                            row["valuation_actual_rank"][output]
                            for row in rows
                        ]),
                    )
                    if rows
                    else None
                )
            valid = [
                value for value in quarterly.values()
                if value is not None
            ]
            result[feature_name] = {
                "mean_quarterly_rank_ic": (
                    float(np.mean(valid)) if valid else None
                ),
                "evaluated_quarters": len(valid),
                "quarterly_rank_ic": quarterly,
            }
        values = [
            item["mean_quarterly_rank_ic"]
            for item in result.values()
            if item["mean_quarterly_rank_ic"] is not None
        ]
        return {
            "by_valuation_feature": result,
            "mean_valuation_rank_ic": (
                float(np.mean(values)) if values else None
            ),
        }

    def run(
        self,
        dataset: FundamentalPricingDataset,
        snapshot: FundamentalPricingSnapshot | None = None,
    ) -> dict[str, Any]:
        dataset.validate()
        dates = np.asarray(dataset.feature_dates, dtype=object)
        predictions = []
        previous_model: LatentPeerMoE | None = None
        leakage_violations = 0
        for test_date in sorted(set(dataset.feature_dates)):
            train = dataset.rows_before(test_date)
            test = dataset.rows_on(test_date)
            train_dates = sorted(set(dates[train]))
            if (
                int(train.sum()) < self.config.minimum_train_rows
                or len(train_dates) < self.config.minimum_train_dates
                or int(test.sum()) < 2
            ):
                continue
            if any(
                end >= test_date
                for end, selected in zip(dataset.label_end_dates, train)
                if selected
            ):
                leakage_violations += 1
                continue
            model = LatentPeerMoE(
                dataset.feature_names, self.config
            ).fit(
                dataset.values[train],
                dataset.availability_mask[train],
                tuple(dates[train]),
                dataset.excess_returns[train],
            )
            if previous_model is not None:
                model.align_to(previous_model)
            result = model.predict(
                dataset.values[test], dataset.availability_mask[test]
            )
            valuation_actual, valuation_available = _rank_by_date(
                tuple(dates[test]),
                dataset.values[test][:, model.valuation_indices],
                dataset.availability_mask[test][
                    :, model.valuation_indices
                ],
            )
            for local, row_index in enumerate(np.flatnonzero(test)):
                predictions.append({
                    "feature_date": test_date.isoformat(),
                    "label_end_date": (
                        dataset.label_end_dates[row_index].isoformat()
                    ),
                    "symbol": dataset.symbols[row_index],
                    "actual_excess_return": float(
                        dataset.excess_returns[row_index]
                    ),
                    "latent_moe": float(result["alpha_prediction"][local]),
                    "single_ridge": float(
                        result["ridge_alpha_prediction"][local]
                    ),
                    "uniform_experts": float(
                        result["uniform_alpha_prediction"][local]
                    ),
                    "global_gate": float(
                        result["global_gate_alpha_prediction"][local]
                    ),
                    "gate": result["gate"][local].tolist(),
                    "valuation_prediction": (
                        result["valuation_prediction"][local].tolist()
                    ),
                    "ridge_valuation_prediction": (
                        result["ridge_valuation_prediction"][local].tolist()
                    ),
                    "valuation_actual_rank": (
                        valuation_actual[local].tolist()
                    ),
                    "valuation_available": (
                        valuation_available[local].tolist()
                    ),
                })
            previous_model = model
        if not predictions:
            raise ValueError("no eligible latent peer walk-forward rows")
        metrics = self._metrics(predictions, "latent_moe")
        baselines = {
            name: self._metrics(predictions, name)
            for name in ("single_ridge", "uniform_experts", "global_gate")
        }
        latest = []
        diagnostics = previous_model.diagnostics()
        if snapshot is not None:
            train = dataset.rows_before(snapshot.feature_date)
            final_model = LatentPeerMoE(
                dataset.feature_names, self.config
            ).fit(
                dataset.values[train],
                dataset.availability_mask[train],
                tuple(dates[train]),
                dataset.excess_returns[train],
            )
            if previous_model is not None:
                final_model.align_to(previous_model)
            current = final_model.predict(
                snapshot.values, snapshot.availability_mask
            )
            latest = self._peers(snapshot.symbols, current["gate"])
            diagnostics = final_model.diagnostics()
        gate_matrix = np.asarray([row["gate"] for row in predictions])
        return {
            "contract": "latent-peer-dual-head-moe-1",
            "config": asdict(self.config),
            "dataset": {
                **dataset.metadata,
                "row_count": len(dataset.symbols),
                "symbol_count": len(set(dataset.symbols)),
                "test_row_count": len(predictions),
                "test_quarter_count": len({
                    row["feature_date"] for row in predictions
                }),
                "leakage_violations": leakage_violations,
            },
            "metrics": metrics,
            "valuation_metrics": self._valuation_metrics(predictions),
            "valuation_baselines": {
                "single_ridge": self._valuation_metrics(
                    predictions, "ridge_valuation_prediction"
                )
            },
            "baselines": baselines,
            "gate_diagnostics": {
                **diagnostics,
                "oos_mean_gate": gate_matrix.mean(axis=0).tolist(),
                "oos_gate_cross_sectional_std": (
                    gate_matrix.std(axis=0).tolist()
                ),
                "company_conditioned": bool(
                    np.max(gate_matrix.std(axis=0)) > 1e-3
                ),
            },
            "predictions": predictions,
            "latest_latent_peers": latest,
            "acceptance": {
                "framework_ready": (
                    leakage_violations == 0
                    and not diagnostics["collapsed"]
                    and bool(np.max(gate_matrix.std(axis=0)) > 1e-3)
                ),
                "production_ready": False,
                "peer_labels_used": False,
                "company_codes_used_as_features": False,
            },
        }
