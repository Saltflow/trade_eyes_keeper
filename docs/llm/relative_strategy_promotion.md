# Relative strategy promotion contract

Reviewed against local source on 2026-10-29. The production optimizer compares,
publishes and activates each market independently. A candidate and its incumbent
use the same frozen evaluation slice, symbol pool, benchmarks and execution
configuration through `make_signals -> Backtester -> EvaluationReport`.

## Policy scope

`config/promotion_policy.yaml` defines replacement thresholds. For each optimizer
market, `PromotionPolicy.for_independent_market()` sets `required_group_count`
and `minimum_improved_groups` to one. Missing sibling markets do not invalidate
that comparison. The general comparison API retains the historical combined
portfolio defaults for callers that explicitly request them; those defaults do
not require the production optimizer to finish three markets before switching one.

The candidate must improve its market return by more than the configured minimum
and satisfy safety checks: finite metrics, matching benchmark names and values,
the minimum benchmark and trade counts, and configured return/drawdown limits.
Default safety values include at least three benchmarks, one trade, maximum
drawdown no worse than -40%, and drawdown regression no greater than five
percentage points. Actual thresholds come from the loaded policy.

## Evidence and activation

A passed relative comparison may supply the admission path when absolute
performance checks fail. Raw holdout and robustness results remain unchanged;
artifacts keep `absolute_eligible`, the original flags and a separate `promotion`
evidence block. Relative admission cannot bypass artifact completeness, execution
contract or activation checks.

Candidate publication and active-pointer updates are separate operations.
Automatic activation requires both a passed same-market promotion decision and
`auto_activate_if_better: true`. A failed/incomplete comparison or a disabled
automatic switch preserves the current pointer and retains diagnostics.
`main.py --activate-run RUN_ID --group MARKET` provides explicit activation with
the same artifact qualification checks. The implementation accepts recorded
relative admission or the applicable absolute holdout path; it does not require
both performance paths to pass simultaneously.

Other markets retain their own bindings. Existing reference portfolios retain
their bound run, parameters and execution contract after a pointer change.

Source: [promotion policy and comparison](../../src/search/promotion.py),
[optimizer orchestration](../../main.py),
[artifact publication and activation](../../src/search/artifacts.py).
See the [search diagram](../architecture_overview.md#搜参子系统放大图) for the full flow.
