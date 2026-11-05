"""Ke-local-trial exclusion helper remains independent of retired Bot switches."""

from src.markets import get_skip_signals


def test_ke_trial_skip_signals():
    assert get_skip_signals({"skip_signals": ["508091"]}) == {"508091"}
    assert get_skip_signals({}) == set()
