"""External price and assumption changes cannot reuse prior search results."""

import hashlib
from types import SimpleNamespace

import numpy as np

from src.search.workflow import _hash_in_kind_inputs


def _digest(*, price=20.0, policy="first", label="strategy", empty=False):
    ledger = SimpleNamespace(
        events=np.zeros((0 if empty else 1, 30)),
        kernel_args=lambda: (np.array([[price]]), np.array([[1.0]])),
        asset_codes=("09618",),
        event_ids=("event",),
        metadata=({"policy_hash": policy},),
        dates=("2022-03-29",),
    )
    hasher = hashlib.sha256(b"same-parent-prices-and-parameters")
    _hash_in_kind_inputs(hasher, SimpleNamespace(in_kind=ledger), label)
    return hasher.hexdigest()


def test_revised_external_price_invalidates_search_identity():
    assert _digest(price=20) != _digest(price=21)


def test_policy_change_invalidates_even_identical_numerical_inputs():
    assert _digest(policy="first") != _digest(policy="changed")


def test_benchmark_and_strategy_entitlements_are_distinct_inputs():
    assert _digest(label="strategy") != _digest(label="benchmark:09618")


def test_no_events_preserve_existing_non_hk_search_identity():
    expected = hashlib.sha256(b"same-parent-prices-and-parameters").hexdigest()
    assert _digest(empty=True) == expected
