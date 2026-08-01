# Adding a trading strategy

Trading strategies answer one question: given parameters and causal market
data, what is the complete `TradePlan`? They do not choose parameters or
calculate performance.

1. Add one module under `src/strategy/plugins/`, for example
   `value_momentum.py`.
2. Decorate the concrete class with `@register_strategy("value_momentum")`.
3. Implement `param_space`, `make_signals`, and `to_human_readable`.
4. Set `optimizer.engine: value_momentum` in configuration when it should run.

Minimal shape:

```python
from src.strategy.api import ParamSpace, Params, StrategyMarketData, TradePlan
from src.strategy.api import TradingStrategy
from src.strategy.registry import register_strategy


@register_strategy("value_momentum")
class ValueMomentumStrategy(TradingStrategy):
    name = "value_momentum"
    label = "Value momentum"

    @property
    def param_space(self) -> ParamSpace:
        ...

    def make_signals(
        self, params: Params, market_data: StrategyMarketData
    ) -> TradePlan:
        ...

    def to_human_readable(self, params: Params) -> str:
        ...
```

For strategies naturally expressed as score and boolean matrices, inherit
`ArraySignalStrategy` and implement `score_signals` plus `signal_arrays`; its
shared implementation builds the canonical cash-cap `TradePlan`.

Discovery scans every non-private module in `plugins/`. Do not edit `main.py`,
the registry, SearchController, Backtester, daily reports, or another strategy.
