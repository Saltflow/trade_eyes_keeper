# Relative strategy promotion contract

The active production strategy is not protected merely because it predates the
current optimizer gates. After a complete search, the candidate and incumbent
are evaluated by the same `make_signals -> Backtester -> EvaluationReport`
path on the same frozen nine-month slice, symbol pool, benchmarks and execution
configuration.

`config/promotion_policy.yaml` is the authority for replacement thresholds.
By default, the equal-capital three-market portfolio return must improve, at
least two markets must improve, no market may lose more than five percentage
points versus the incumbent, drawdown regression is capped at five percentage
points, and every market must retain three identical benchmark observations,
at least one trade and a drawdown above -40%.

A passed relative comparison may override performance admission failures such
as one holdout window or leave-one-symbol-out instability. It never rewrites
those raw results: artifacts retain `absolute_eligible`, the original holdout
and universe flags, plus a separate `promotion` evidence block. A successful
comparison atomically publishes the complete three-market run; a failed or
incomplete comparison preserves the current active pointer.
