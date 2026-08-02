import os

import pytest

from src.search.resources import ResourcePlanner


def test_apply_uses_numba_runtime_mask_without_mutating_environment():
    numba = pytest.importorskip("numba")
    planner = ResourcePlanner(physical_cores=2)
    plan = planner.plan("candidate_window", workers=1, batch_size=128)
    before_env = os.environ.get("NUMBA_NUM_THREADS")
    before_threads = numba.get_num_threads()

    with planner.apply(plan):
        assert numba.get_num_threads() == 1
        assert os.environ.get("NUMBA_NUM_THREADS") == before_env

    assert numba.get_num_threads() == before_threads
    assert os.environ.get("NUMBA_NUM_THREADS") == before_env


def test_candidate_process_axis_keeps_numba_single_threaded():
    planner = ResourcePlanner(physical_cores=8)

    plan = planner.plan("candidate_window", workers=6, batch_size=256)

    assert (plan.outer_workers, plan.numba_threads) == (6, 1)
