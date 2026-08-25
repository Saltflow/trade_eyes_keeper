# Source architecture

The source tree is organized by runtime responsibility. Public contracts live
at each package root; concrete plugins are always one level below them.

```text
strategy/       investment decisions: MarketData -> TradePlan
  api.py         TradingStrategy and shared decision contracts
  registry.py    automatic plugin discovery
  plugins/       concrete trading strategies

search/         parameter optimization: ask/tell over typed candidates
  api.py         stable Solver and search-data imports
  controller.py  solver-neutral orchestration
  registry.py    automatic Solver discovery
  solvers/       concrete optimization algorithms

backtest/       fills, portfolio simulation and evaluation reports
experiments/    offline comparisons; never activates production artifacts
data/           market/fundamental acquisition and LLM extraction
```

Intended dependency direction:

```text
SearchController <-> Solver.ask/tell
       |
       v
EvaluationService -> TradingStrategy -> TradePlan -> Backtester -> raw metrics
       |
       +-> CandidateGatePipeline / ValidationController / Search Artifacts
```

- A Solver never imports a strategy, Backtester, Gate Profile, or holdout data.
- A strategy never searches parameters, executes trades, or calculates return.
- The Backtester never branches on strategy ID or Solver ID.
- Offline experiments may use the public APIs but are not production entry
  points and cannot publish or activate optimizer artifacts.

See [strategy/README.md](strategy/README.md) and
[search/README.md](search/README.md) for extension instructions. The detailed
[architecture audit](../docs/architecture.md) also records the remaining
transitional dependency cycles and the two active scheduler implementations;
those files are not interchangeable duplicates.
