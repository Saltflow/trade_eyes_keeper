# 估值上下文到量化输入的桥接

历史估值上下文通过以下顺序进入量化输入：

1. `build_historical_valuation_context()` 将带发布日期的 DCF/CAPM 与行业相对字段转换为 rank、availability 和质量字段。
2. `attach_historical_valuation_context()` 执行逐标的 `feature_date <= market_date` 的 as-of join，并记录每个日行情行的财务来源日期。
3. `ValuationContextScorer` 只读取已验证的 context panel，按统一经济方向聚合可用 rank，并用观测比例衰减信号；不拟合参数、不读取未来标签、不覆盖缺失值。

代码入口：

- `src/fundamental_embedding/valuation_context.py`
- `src/strategy/valuation_context_panel.py`
- `src/strategy/valuation_context_scorer.py`

当前 scorer 是研究组件。它不能自动改变活动策略，也不能绕过统一搜参的候选评价和留出门槛。历史 OOS 结果显示 context 目前降低 Rank IC，因此默认不启用收益排序；它可用于候选筛选、风险解释和后续 gated expert。
