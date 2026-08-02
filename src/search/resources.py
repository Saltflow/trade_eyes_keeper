"""One-axis CPU resource planning for search and benchmark workloads."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class ResourcePlan:
    axis: str
    outer_workers: int
    numba_threads: int
    batch_size: int


class ResourcePlanner:
    VALID_AXES = {"candidate_window", "strategy_market", "window"}

    def __init__(self, physical_cores: int | None = None):
        self.physical_cores = max(1, int(physical_cores or _physical_cores()))

    def plan(
        self,
        axis: str = "candidate_window",
        workers: int | None = None,
        batch_size: int = 256,
    ) -> ResourcePlan:
        if axis not in self.VALID_AXES:
            raise ValueError(f"unknown parallel axis: {axis}")
        batch_size = int(batch_size)
        if not 128 <= batch_size <= 512:
            raise ValueError("candidate batch_size must be between 128 and 512")
        requested = min(
            max(1, int(workers or self.physical_cores)), self.physical_cores
        )
        if axis == "strategy_market":
            return ResourcePlan(axis, requested, 1, batch_size)
        # Candidate workers are independent processes. Numba and BLAS stay
        # single-threaded in each process to avoid nested oversubscription.
        return ResourcePlan(axis, requested, 1, batch_size)

    @contextmanager
    def apply(self, plan: ResourcePlan):
        """Temporarily set Numba's runtime thread mask.

        ``NUMBA_NUM_THREADS`` is a process-startup ceiling. Mutating it after
        Numba has initialized makes later JIT compilation fail, so runtime
        scheduling must use ``set_num_threads`` exclusively.
        """
        prior_numba = None
        numba = None
        try:
            try:
                import numba as numba_module

                numba = numba_module
                prior_numba = numba.get_num_threads()
                ceiling = int(numba.config.NUMBA_NUM_THREADS)
                numba.set_num_threads(min(plan.numba_threads, ceiling))
            except (ImportError, RuntimeError, ValueError):
                prior_numba = None
            yield plan
        finally:
            if prior_numba is not None and numba is not None:
                try:
                    numba.set_num_threads(prior_numba)
                except (RuntimeError, ValueError):
                    pass


def _physical_cores() -> int:
    try:
        import psutil

        return int(psutil.cpu_count(logical=False) or os.cpu_count() or 1)
    except ImportError:
        return int(os.cpu_count() or 1)
