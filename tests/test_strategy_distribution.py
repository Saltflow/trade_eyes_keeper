"""策略分布模型测试 — 四维度评分 + 贝叶斯更新 + 漂移追踪。"""

import numpy as np

from src.analysis.strategy_distribution import (
    StrategyDistribution,
    compute_long_term,
    compute_short_term_factor,
    compute_style_switch_prob,
    compute_temporal_drift,
    compute_overall_score,
    bayesian_update,
    match_strategy,
    StrategyDistributionPool,
)


class TestLongTerm:
    def test_mean_and_std(self):
        scores = [7.8, 5.2, 6.1, 4.9, 8.3, 5.7]
        lt = compute_long_term(scores)
        assert abs(lt.mean - 6.33) < 0.1
        assert lt.std > 0

    def test_empty_scores(self):
        lt = compute_long_term([])
        assert lt.mean == 0.0
        assert lt.std == 0.0


class TestShortTermFactor:
    def test_normal(self):
        factor = compute_short_term_factor(recent_return=3.1, long_term_mean=6.33)
        assert abs(factor - 0.49) < 0.05

    def test_capped_low(self):
        factor = compute_short_term_factor(recent_return=-10.0, long_term_mean=6.0)
        assert factor == 0.5

    def test_capped_high(self):
        factor = compute_short_term_factor(recent_return=20.0, long_term_mean=5.0)
        assert factor == 2.0

    def test_zero_long_term(self):
        factor = compute_short_term_factor(recent_return=3.0, long_term_mean=0.0)
        assert factor == 1.0


class TestStyleSwitchProb:
    def test_stable(self):
        """7组得分接近 → 切换概率低。"""
        scores = [6.0, 6.1, 5.9, 6.0, 6.1, 5.9, 6.0]
        prob = compute_style_switch_prob(scores)
        assert prob < 0.1

    def test_unstable(self):
        """最好和最差差距大 → 切换概率高。"""
        scores = [8.3, 5.2, 6.1, 4.9, 7.8, 5.7, 3.1]
        prob = compute_style_switch_prob(scores)
        assert prob > 0.5

    def test_all_same(self):
        scores = [5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
        prob = compute_style_switch_prob(scores)
        assert prob == 0.0


class TestTemporalDrift:
    def test_stable_params(self):
        """参数几乎不变 → CV 低 → stability 高。"""
        daily_values = [-0.052, -0.051, -0.053, -0.052, -0.050, -0.049]
        drift = compute_temporal_drift({"buy_threshold": daily_values})
        assert drift.avg_cv < 0.1
        assert drift.stability_factor > 0.9

    def test_unstable_params(self):
        """参数大幅漂移 → CV 高 → stability 低。"""
        daily_values = [-0.052, -0.110, -0.050, -0.005, -0.080, -0.030]
        drift = compute_temporal_drift({"buy_threshold": daily_values})
        assert drift.avg_cv > 0.3
        assert drift.stability_factor < 0.8

    def test_single_day(self):
        """只有1天数据 → CV=0 → stability=1。"""
        drift = compute_temporal_drift({"buy_threshold": [-0.05]})
        assert drift.avg_cv == 0.0
        assert drift.stability_factor == 1.0

    def test_multiple_params(self):
        """多个参数取平均 CV。"""
        drift = compute_temporal_drift({
            "buy_threshold": [-0.05, -0.05, -0.05],
            "sell_threshold": [0.02, 0.10, 0.02],
        })
        # buy CV=0, sell CV高 → 均值在中间
        assert 0.0 < drift.avg_cv < 1.0


class TestOverallScore:
    def test_good_strategy(self):
        """长期好 + 稳定 + 短期正常 → 高分。"""
        score = compute_overall_score(
            long_term_mean=6.0,
            style_switch_prob=0.1,
            short_term_factor=1.0,
            stability_factor=0.95,
        )
        assert score > 5.0

    def test_unstable_strategy(self):
        """风格切换高 + 参数漂移大 → 低分。"""
        score = compute_overall_score(
            long_term_mean=5.0,
            style_switch_prob=0.8,
            short_term_factor=1.5,
            stability_factor=0.3,
        )
        assert score < 1.0


class TestBayesianUpdate:
    def test_update_shifts_mean(self):
        """新证据更新后均值向新数据偏移。"""
        prior_mean = -0.05
        prior_std = 0.02
        new_samples = [-0.06, -0.055, -0.058]
        updated = bayesian_update(prior_mean, prior_std, new_samples)
        # 后验均值应该在先验和新样本均值之间
        sample_mean = np.mean(new_samples)
        assert prior_mean < updated.mean < sample_mean or abs(updated.mean - sample_mean) < 0.01

    def test_update_reduces_std(self):
        """多次更新后方差应该减小（更确定）。"""
        prior_mean = -0.05
        prior_std = 0.02
        samples = [-0.051, -0.049, -0.050]
        updated = bayesian_update(prior_mean, prior_std, samples)
        assert updated.std <= prior_std

    def test_empty_samples_no_change(self):
        """无新样本 → 不变。"""
        updated = bayesian_update(-0.05, 0.02, [])
        assert updated.mean == -0.05
        assert updated.std == 0.02


class TestMatchStrategy:
    def test_within_2sigma_matches(self):
        """参数在分布 2σ 内 → 匹配。"""
        dist = StrategyDistribution(
            params={"buy_1_t": (-0.05, 0.02)},
        )
        new_params = {"buy_1_t": -0.06}
        assert match_strategy(dist, new_params) is True

    def test_outside_2sigma_no_match(self):
        """参数超出 2σ → 不匹配。"""
        dist = StrategyDistribution(
            params={"buy_1_t": (-0.05, 0.02)},
        )
        new_params = {"buy_1_t": -0.20}
        assert match_strategy(dist, new_params) is False

    def test_different_param_keys_no_match(self):
        dist = StrategyDistribution(
            params={"buy_1_t": (-0.05, 0.02)},
        )
        new_params = {"sell_1_t": -0.05}
        assert match_strategy(dist, new_params) is False


class TestDistributionPool:
    def test_first_run_initializes(self, tmp_path):
        """首次运行：无历史 → 从搜索结果初始化。"""
        pool = StrategyDistributionPool(tmp_path / "distributions.yaml")
        assert len(pool.distributions) == 0

        # 模拟一次搜索结果
        search_results = [
            {
                "params": {"buy_1_t": -0.05, "sell_1_t": 0.02},
                "wf_scores": [6.0, 5.5, 5.8, 6.2, 5.9, 6.1],
                "recent_return": 3.0,
            },
        ]
        pool.update(search_results)
        assert len(pool.distributions) == 1
        assert pool.distributions[0].age_days == 1

    def test_second_run_updates(self, tmp_path):
        """第二次运行：匹配到已有分布 → 贝叶斯更新。"""
        pool = StrategyDistributionPool(tmp_path / "distributions.yaml")
        search_results = [
            {
                "params": {"buy_1_t": -0.05, "sell_1_t": 0.02},
                "wf_scores": [6.0, 5.5, 5.8, 6.2, 5.9, 6.1],
                "recent_return": 3.0,
            },
        ]
        pool.update(search_results)

        # 第二天，参数接近
        search_results_day2 = [
            {
                "params": {"buy_1_t": -0.052, "sell_1_t": 0.021},
                "wf_scores": [6.1, 5.6, 5.9, 6.3, 6.0, 6.2],
                "recent_return": 3.2,
            },
        ]
        pool.update(search_results_day2)
        assert len(pool.distributions) == 1  # 不新增，更新已有的
        assert pool.distributions[0].age_days == 2

    def test_save_and_load(self, tmp_path):
        """持久化：保存后重新加载。"""
        path = tmp_path / "distributions.yaml"
        pool = StrategyDistributionPool(path)
        pool.update([
            {
                "params": {"buy_1_t": -0.05},
                "wf_scores": [6.0, 5.0, 5.5, 6.5, 5.5, 6.0],
                "recent_return": 3.0,
            },
        ])
        pool.save()

        pool2 = StrategyDistributionPool(path)
        pool2.load()
        assert len(pool2.distributions) == 1
        assert pool2.distributions[0].params["buy_1_t"][0] == -0.05

    def test_new_strategy_added(self, tmp_path):
        """不匹配任何已有分布 → 新增。"""
        pool = StrategyDistributionPool(tmp_path / "distributions.yaml")
        pool.update([
            {
                "params": {"buy_1_t": -0.05},
                "wf_scores": [6.0, 5.0, 5.5, 6.5, 5.5, 6.0],
                "recent_return": 3.0,
            },
        ])
        pool.update([
            {
                "params": {"buy_1_t": -0.20},  # 完全不同的参数
                "wf_scores": [4.0, 3.0, 3.5, 4.5, 3.5, 4.0],
                "recent_return": 1.0,
            },
        ])
        assert len(pool.distributions) == 2

    def test_top_n_sorted_by_score(self, tmp_path):
        """Top N 按综合评分排序。"""
        pool = StrategyDistributionPool(tmp_path / "distributions.yaml")
        pool.update([
            {
                "params": {"buy_1_t": -0.05},
                "wf_scores": [8.0, 7.0, 7.5, 8.5, 7.5, 8.0],
                "recent_return": 5.0,
            },
            {
                "params": {"buy_1_t": -0.15},
                "wf_scores": [3.0, 2.0, 2.5, 3.5, 2.5, 3.0],
                "recent_return": 1.0,
            },
        ])
        top = pool.get_top_n(2)
        assert top[0].overall_score > top[1].overall_score
