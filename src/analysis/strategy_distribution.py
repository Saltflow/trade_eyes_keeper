"""策略分布模型 — 四维度评分 + 贝叶斯更新 + 漂移追踪。

每个策略不只是一个固定参数组合，而是一个随时间演化的分布。
每天搜索后用新结果更新已有分布，实现增量改善。

四个评分维度:
1. 长期有效性: 6个 Walk-Forward 窗口得分的均值和标准差
2. 短期有效性: 最近1个月收益 / 长期均值 (capped [0.5, 2.0])
3. 风格切换概率: 1 - min(7组) / max(7组)
4. 时间偏离跨度: 每日最优参数的变异系数 CV = std/|mean|
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
# 四维度评分函数
# ════════════════════════════════════════════════════════════


@dataclass
class LongTermScore:
    mean: float = 0.0
    std: float = 0.0
    scores: list[float] = field(default_factory=list)


def compute_long_term(wf_scores: list[float]) -> LongTermScore:
    """长期有效性: 6 个 Walk-Forward 窗口得分。"""
    if not wf_scores:
        return LongTermScore()
    arr = np.array(wf_scores)
    return LongTermScore(
        mean=float(np.mean(arr)),
        std=float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        scores=wf_scores[:],
    )


def compute_short_term_factor(
    recent_return: float, long_term_mean: float
) -> float:
    """短期有效性: 最近1个月收益 / 长期均值，capped [0.5, 2.0]。"""
    if long_term_mean == 0:
        return 1.0
    factor = recent_return / long_term_mean
    return max(0.5, min(2.0, factor))


def compute_style_switch_prob(scores_7: list[float]) -> float:
    """风格切换概率: 1 - min / max。

    7组得分（6 WF + 1近月），最好和最差的比。
    越接近1 = 跨周期稳定，切换概率低。
    """
    if not scores_7:
        return 1.0
    best = max(scores_7)
    worst = min(scores_7)
    if best <= 0:
        return 1.0
    ratio = worst / best
    if ratio < 0:
        return 1.0
    return 1.0 - ratio


@dataclass
class TemporalDrift:
    avg_cv: float = 0.0
    stability_factor: float = 1.0
    param_cvs: dict[str, float] = field(default_factory=dict)


def compute_temporal_drift(
    param_history: dict[str, list[float]],
) -> TemporalDrift:
    """时间偏离跨度: 每日最优参数的变异系数。

    Args:
        param_history: {param_name: [day1_value, day2_value, ...]}

    Returns:
        TemporalDrift with avg_cv and stability_factor = 1/(1+avg_cv)
    """
    if not param_history:
        return TemporalDrift()

    cvs = {}
    for name, values in param_history.items():
        if len(values) < 2:
            cvs[name] = 0.0
            continue
        arr = np.array(values, dtype=float)
        mean = np.mean(arr)
        std = np.std(arr, ddof=1)
        if abs(mean) < 1e-10:
            cvs[name] = 0.0 if std < 1e-10 else 1.0
        else:
            cvs[name] = float(abs(std / mean))

    avg_cv = float(np.mean(list(cvs.values())))
    stability = 1.0 / (1.0 + avg_cv)
    return TemporalDrift(avg_cv=avg_cv, stability_factor=stability, param_cvs=cvs)


def compute_overall_score(
    long_term_mean: float,
    style_switch_prob: float,
    short_term_factor: float,
    stability_factor: float,
) -> float:
    """综合评分 = 长期 × (1 - 风格切换) × 短期 × 稳定性。"""
    return (
        long_term_mean
        * (1.0 - style_switch_prob)
        * short_term_factor
        * stability_factor
    )


# ════════════════════════════════════════════════════════════
# 贝叶斯更新
# ════════════════════════════════════════════════════════════


@dataclass
class UpdatedParam:
    mean: float
    std: float


def bayesian_update(
    prior_mean: float,
    prior_std: float,
    new_samples: list[float],
) -> UpdatedParam:
    """贝叶斯更新正态分布参数。

    先验 N(μ₀, σ₀²) + 新证据 → 后验 N(μ_post, σ_post²)
    用精度（方差的倒数）加权合并。
    """
    if not new_samples:
        return UpdatedParam(prior_mean, prior_std)

    n = len(new_samples)
    prior_var = prior_std ** 2
    sample_mean = float(np.mean(new_samples))
    sample_var = float(np.var(new_samples, ddof=1)) if n > 1 else prior_var

    if sample_var < 1e-10:
        sample_var = prior_var

    # 精度加权
    posterior_var = 1.0 / (1.0 / prior_var + n / sample_var)
    posterior_mean = posterior_var * (
        prior_mean / prior_var + n * sample_mean / sample_var
    )

    return UpdatedParam(
        mean=float(posterior_mean),
        std=float(np.sqrt(posterior_var)),
    )


# ════════════════════════════════════════════════════════════
# 策略匹配
# ════════════════════════════════════════════════════════════


@dataclass
class StrategyDistribution:
    """单个策略的分布模型。"""

    params: dict[str, tuple[float, float]] = field(default_factory=dict)
    # param_name → (mean, std)
    wf_scores: list[float] = field(default_factory=list)
    recent_return: float = 0.0
    daily_param_values: dict[str, list[float]] = field(default_factory=dict)
    age_days: int = 0
    overall_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "params": {k: {"mean": v[0], "std": v[1]} for k, v in self.params.items()},
            "wf_scores": self.wf_scores,
            "recent_return": self.recent_return,
            "daily_param_values": self.daily_param_values,
            "age_days": self.age_days,
            "overall_score": self.overall_score,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyDistribution":
        params = {}
        for k, v in d.get("params", {}).items():
            params[k] = (v["mean"], v["std"])
        return cls(
            params=params,
            wf_scores=d.get("wf_scores", []),
            recent_return=d.get("recent_return", 0.0),
            daily_param_values=d.get("daily_param_values", {}),
            age_days=d.get("age_days", 0),
            overall_score=d.get("overall_score", 0.0),
        )


def match_strategy(
    dist: StrategyDistribution,
    new_params: dict[str, float],
    sigma_threshold: float = 2.0,
) -> bool:
    """判断新策略参数是否落在已有分布的 2σ 范围内。"""
    if not dist.params:
        return False
    if set(dist.params.keys()) != set(new_params.keys()):
        # 只比较有交集的 key
        common = set(dist.params.keys()) & set(new_params.keys())
        if not common:
            return False
        check_keys = common
    else:
        check_keys = dist.params.keys()

    for key in check_keys:
        mean, std = dist.params[key]
        val = new_params[key]
        if std < 1e-10:
            if abs(val - mean) > 1e-6:
                return False
        elif abs(val - mean) > sigma_threshold * std:
            return False

    return True


# ════════════════════════════════════════════════════════════
# 分布池
# ════════════════════════════════════════════════════════════


class StrategyDistributionPool:
    """策略分布池 — 管理多个策略分布的持久化和更新。"""

    MAX_POOL_SIZE = 10

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.distributions: list[StrategyDistribution] = []
        self.load()

    def update(self, search_results: list[dict]) -> None:
        """用新一轮搜索结果更新分布池。

        Args:
            search_results: [{"params": {...}, "wf_scores": [...], "recent_return": float}, ...]
        """
        for result in search_results:
            new_params = result["params"]
            wf_scores = result.get("wf_scores", [])
            recent_return = result.get("recent_return", 0.0)

            # 尝试匹配已有分布
            matched = False
            for dist in self.distributions:
                if match_strategy(dist, new_params):
                    self._update_distribution(dist, new_params, wf_scores, recent_return)
                    matched = True
                    break

            if not matched and len(self.distributions) < self.MAX_POOL_SIZE:
                self._add_distribution(new_params, wf_scores, recent_return)

        # 重新评分并排序
        self._rescore()
        self.save()

    def _update_distribution(
        self,
        dist: StrategyDistribution,
        new_params: dict[str, float],
        wf_scores: list[float],
        recent_return: float,
    ) -> None:
        """贝叶斯更新已有分布。"""
        for key, val in new_params.items():
            if key in dist.params:
                old_mean, old_std = dist.params[key]
                updated = bayesian_update(old_mean, old_std, [val])
                dist.params[key] = (updated.mean, updated.std)
            else:
                dist.params[key] = (val, 0.01)

            # 记录每日值
            if key not in dist.daily_param_values:
                dist.daily_param_values[key] = []
            dist.daily_param_values[key].append(val)

        dist.wf_scores = wf_scores if wf_scores else dist.wf_scores
        dist.recent_return = recent_return
        dist.age_days += 1

    def _add_distribution(
        self,
        params: dict[str, float],
        wf_scores: list[float],
        recent_return: float,
    ) -> None:
        """新增一个策略分布。"""
        dist = StrategyDistribution(
            params={k: (v, 0.01) for k, v in params.items()},
            wf_scores=wf_scores,
            recent_return=recent_return,
            daily_param_values={k: [v] for k, v in params.items()},
            age_days=1,
        )
        self.distributions.append(dist)

    def _rescore(self) -> None:
        """重新计算所有分布的综合评分。"""
        for dist in self.distributions:
            lt = compute_long_term(dist.wf_scores)
            stf = compute_short_term_factor(dist.recent_return, lt.mean)
            scores_7 = dist.wf_scores + [dist.recent_return]
            ssp = compute_style_switch_prob(scores_7)
            drift = compute_temporal_drift(dist.daily_param_values)
            dist.overall_score = compute_overall_score(
                lt.mean, ssp, stf, drift.stability_factor
            )

        # 按评分降序排列
        self.distributions.sort(key=lambda d: d.overall_score, reverse=True)

    def get_top_n(self, n: int = 3) -> list[StrategyDistribution]:
        """返回综合评分最高的 N 个策略。"""
        return self.distributions[:n]

    def save(self) -> None:
        """持久化到 YAML。"""
        data = {
            "distributions": [d.to_dict() for d in self.distributions],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    def load(self) -> None:
        """从 YAML 加载。"""
        if not self.path.exists():
            self.distributions = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            self.distributions = [
                StrategyDistribution.from_dict(d)
                for d in data.get("distributions", [])
            ]
        except Exception as e:
            logger.warning(f"加载策略分布失败: {e}")
            self.distributions = []
