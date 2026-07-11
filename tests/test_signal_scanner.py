"""tests for signal_scanner.py"""

import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from src.analysis.signal_scanner import (
    SignalScanner, ConsensusReport, ScanResult, StrategyAlert,
)


@pytest.fixture
def scanner():
    return SignalScanner(results_dir="data/optimizer")


class TestFileLoading:
    def test_find_latest_none_when_no_files(self, scanner, tmp_path):
        scanner.results_dir = tmp_path
        assert scanner._find_latest("a_share") is None

    def test_find_latest_excludes_non_prefix(self, scanner, tmp_path):
        scanner.results_dir = tmp_path
        f = tmp_path / "20260430_000958_non_a_share_strategies.yaml"
        f.write_text("")
        assert scanner._find_latest("a_share") is None

    def test_find_latest_matches_correct_group(self, scanner, tmp_path):
        scanner.results_dir = tmp_path
        f = tmp_path / "20260430_000958_a_share_strategies.yaml"
        f.write_text("strategies: []")
        result = scanner._find_latest("a_share")
        assert result is not None
        assert result.name == "20260430_000958_a_share_strategies.yaml"

    def test_load_strategies_handles_numpy_tags(self, scanner, tmp_path):
        """YAML with numpy scalar tags should still load"""
        yaml_content = """report_id: test
group: a_share
strategies:
- rank: 1
  train_return: 3.08
  test_return: 25.78
  test_drawdown: -17.24
  sharpe: 1.59
  trade_count: 27
  params:
    _stocks: "600938,512810"
    buy_1_signal: volume_spike
    buy_1_t: '0.5'
    buy_1_frac: '0.2'
    buy_2_signal: rsi_signal
    buy_2_t: '0.3'
    buy_2_frac: '0.1'
    sell_1_signal: none
    sell_1_t: '0.0'
    sell_1_frac: '0.1'
    sell_2_signal: none
    sell_2_t: '0.0'
    sell_2_frac: '0.1'
    sell_3_signal: none
    sell_3_t: '0.0'
    sell_3_frac: '0.1'
  rules:
  - id: buy_1
    type: buy
    priority: 1
    condition: vol_ratio > 1.5
    action_amount: cash * 0.2
    reset_when: vol_ratio < 1.0
"""
        f = tmp_path / "20260430_000958_a_share_strategies.yaml"
        f.write_text(yaml_content)
        scanner.results_dir = tmp_path
        strategies = scanner._load_strategies("a_share", top_n=5)
        assert len(strategies) == 1
        assert strategies[0]["rank"] == 1


class TestConsensus:
    def test_compute_consensus_buy_signals(self, scanner):
        strategies = [
            {
                "params": {
                    "_stocks": "600938,512810",
                    "buy_1_signal": "volume_spike",
                    "buy_2_signal": "rsi_signal",
                    "sell_1_signal": "none",
                    "sell_2_signal": "none",
                    "sell_3_signal": "none",
                },
                "rules": [
                    {"type": "buy", "condition": "vol_ratio > 1.5"},
                ],
            }
        ] * 5  # 5 identical strategies
        consensus = scanner.compute_consensus(strategies)
        assert "volume_spike" in consensus.consensus_buy_signals
        assert "rsi_signal" in consensus.consensus_buy_signals
        assert "600938" in consensus.consensus_stocks

    def test_consensus_minority_filtered(self, scanner):
        # 1/5 uses deviation_cross -> should be filtered (need >=2/5)
        strategies = [
            {
                "params": {
                    "_stocks": "600938",
                    "buy_1_signal": "volume_spike",
                    "buy_2_signal": "volume_spike",
                    "sell_1_signal": "none",
                    "sell_2_signal": "none",
                    "sell_3_signal": "none",
                },
                "rules": [],
            }
        ] * 4 + [
            {
                "params": {
                    "_stocks": "000001",
                    "buy_1_signal": "deviation_cross",
                    "buy_2_signal": "deviation_cross",
                    "sell_1_signal": "none",
                    "sell_2_signal": "none",
                    "sell_3_signal": "none",
                },
                "rules": [],
            }
        ]
        consensus = scanner.compute_consensus(strategies)
        # 2/5 >= min(2) -> deviation_cross passes. volume_spike at 8/5 also passes.
        assert "volume_spike" in consensus.consensus_buy_signals
        assert "deviation_cross" in consensus.consensus_buy_signals
        # 1/5 stock inclusion (000001) should be filtered (need >=3/5)
        assert "000001" not in consensus.consensus_stocks

    def test_stock_inclusion_parses_plus_suffix(self, scanner):
        strategies = [
            {
                "params": {
                    "_stocks": "600938,512810,601398 +2",
                    "buy_1_signal": "volume_spike",
                    "buy_2_signal": "rsi_signal",
                    "sell_1_signal": "none",
                    "sell_2_signal": "none",
                    "sell_3_signal": "none",
                },
                "rules": [],
            }
        ] * 4
        consensus = scanner.compute_consensus(strategies)
        # "+2" should be filtered out (short digit-only)
        assert "2" not in consensus.stock_inclusion_counts
        assert "600938" in consensus.stock_inclusion_counts

    def test_extract_indicators_from_condition(self, scanner):
        found = scanner._extract_indicators_from_condition(
            "rsi < 30 and deviation <= -0.15 and vol_ratio > 2.0"
        )
        assert "rsi" in found
        assert "deviation" in found
        assert "vol_ratio" in found
        assert "adx" not in found

    def test_detect_divergence(self, scanner):
        strategies = [
            {"train_return": 5.0, "test_return": 25.0, "rank": 1},
            {"train_return": 12.0, "test_return": 14.0, "rank": 2},
        ]
        warnings = scanner._detect_divergence(strategies)
        assert len(warnings) == 1  # only rank 1 (diff 20 > 15)
        assert "Rank 1" in warnings[0]


class TestContextBuilding:
    def test_build_context_defaults(self, scanner):
        today = {"close": 10.0, "deviation": -0.05, "rsi": 30}
        ctx = scanner._build_context(today, None, "test")
        assert ctx["close"] == 10.0
        assert ctx["deviation"] == -0.05
        assert ctx["prev_deviation"] is None  # no history
        assert ctx["shares"] == 0

    def test_build_context_with_history(self, scanner):
        today = {"close": 10.0, "deviation": -0.03, "rsi": 45}
        hist = pd.DataFrame({
            "close": [10.2, 10.1, 10.0],
        })
        hist["ma60"] = hist["close"].rolling(60, min_periods=1).mean()
        hist["deviation"] = (hist["close"] - hist["ma60"]) / hist["ma60"]
        ctx = scanner._build_context(today, hist, "test")
        assert ctx["prev_deviation"] is not None

    def test_describe_current(self, scanner):
        today = {"rsi": 22.3, "deviation": -0.162}
        desc = scanner._describe_current(today, "rsi < 26.7")
        assert "22.3" in desc


class TestScanDedup:
    """scan() 按 (标的, 条件) 去重：Top5 多策略同条件只报一次。"""

    def _session(self, code="00883"):
        """构造带 _historical + stocks_data 的最小 session。"""
        import pandas as pd
        dates = pd.date_range("2025-01-01", periods=80, freq="D")
        # 造一个 ADX 高、MACD 正的上涨序列
        close = pd.Series(range(100, 180))
        df = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "open": close, "high": close + 1, "low": close - 1,
            "close": close, "volume": [1e6] * 80,
        })
        sess = MagicMock()
        sess._historical = {code: df}
        sess.stocks_data = [{"stock_code": code}]
        return sess

    def test_same_condition_dedup(self, scanner, tmp_path):
        """两个策略含相同 condition → 只产 1 条告警。"""
        # 两个策略，buy_2 和 buy_4 完全相同条件
        strategies = [
            {"rules": [
                {"id": "buy_2", "type": "buy", "label": None,
                 "condition": "adx > 10 and macd_hist > -999"},
            ]},
            {"rules": [
                {"id": "buy_4", "type": "buy", "label": None,
                 "condition": "adx > 10 and macd_hist > -999"},
            ]},
        ]
        sess = self._session("00883")
        with patch.object(scanner, "_load_strategies", return_value=strategies), \
             patch.object(scanner, "compute_consensus",
                          return_value=ConsensusReport(consensus_stocks=["00883"])), \
             patch.object(scanner, "_get_stock_codes", return_value=["00883"]):
            result = scanner.scan(sess, "hk", top_n=5)
        # 相同条件只报一次
        codes = [(a.stock_code, a.condition_str) for a in result.alerts]
        assert len(codes) == len(set(codes)), f"有重复告警: {codes}"

