import os

import pytest

from src.search import resources
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


def test_linux_auto_workers_use_schedulable_cpu_slots(monkeypatch):
    monkeypatch.setattr(
        resources.os,
        "sched_getaffinity",
        lambda _pid: {0, 1},
        raising=False,
    )
    monkeypatch.setattr(resources, "_cgroup_cpu_quota_slots", lambda: 2)

    planner = ResourcePlanner()
    automatic = planner.plan("candidate_window", workers=None, batch_size=128)
    explicit = planner.plan("candidate_window", workers=2, batch_size=128)

    assert planner.physical_cores == 2
    assert automatic.outer_workers == 2
    assert explicit.outer_workers == 2


def test_linux_auto_workers_respect_cgroup_quota(monkeypatch):
    monkeypatch.setattr(
        resources.os,
        "sched_getaffinity",
        lambda _pid: {0, 1, 2, 3},
        raising=False,
    )
    monkeypatch.setattr(resources, "_cgroup_cpu_quota_slots", lambda: 1)

    planner = ResourcePlanner()

    assert planner.physical_cores == 1
    assert planner.plan(workers=4, batch_size=128).outer_workers == 1
