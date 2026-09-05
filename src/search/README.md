# Adding an optimization algorithm

Search algorithms are called `Solver` plugins. Genetic search, random search,
simulated annealing, Bayesian optimization, and parameter-proposal networks all
belong here when they output parameter candidates.

1. Add one module under `src/search/solvers/`, for example `bayesian.py`.
2. Decorate the class with `@register_solver("bayesian")`.
3. Implement the seven-method ask/tell contract below.
4. Add the Solver configuration to `search.solvers`, then select it explicitly
   as `optimizer.markets.<market>.solver_id` for every market that should use it.

```python
from src.search import CandidateBatch, EvaluationBatch, SearchProblem, Solver
from src.search import register_solver


@register_solver("bayesian")
class BayesianSolver(Solver):
    solver_id = "bayesian"

    def initialize(self, problem: SearchProblem, config=None) -> None: ...
    def ask(self, batch_size: int) -> CandidateBatch: ...
    def tell(self, evaluations: EvaluationBatch) -> None: ...
    def should_stop(self) -> bool: ...
    def finalists(self, limit=None) -> tuple[str, ...]: ...
    def state_dict(self) -> dict[str, object]: ...
    def load_state_dict(self, state: dict[str, object]) -> None: ...
```

Discovery scans every non-private module in `solvers/`. Do not edit
SearchController, the registry, strategy code, Backtester, or `main.py`.

A neural component that proposes parameter candidates is a Solver. A neural
component that directly emits daily buy/sell decisions is a trading strategy.
