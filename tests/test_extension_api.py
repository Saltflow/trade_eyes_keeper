"""The extension points must be discoverable without reading implementations."""

from src.search import Solver, list_solvers
from src.strategy import TradingStrategy, list_strategy_ids


def test_public_solver_api_and_automatic_discovery():
    assert Solver.__abstractmethods__ == {
        "initialize",
        "ask",
        "tell",
        "should_stop",
        "finalists",
        "state_dict",
        "load_state_dict",
    }
    assert list_solvers() == ("genetic", "random", "simulated_annealing")


def test_public_strategy_api_and_automatic_discovery():
    assert TradingStrategy.__abstractmethods__ == {
        "make_signals",
        "param_space",
        "to_human_readable",
    }
    assert list_strategy_ids() == (
        "builder",
        "percentile",
        "regime_pullback",
        "simplified",
        "technical_ensemble",
    )
