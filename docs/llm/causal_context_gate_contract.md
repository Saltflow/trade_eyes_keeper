# Causal context gate 合同

`causal-rank-context-gate-1` 是基本面 expert 与估值/行业 context expert 的研究级组合器。

## 规则

- 每个测试日期只使用该日期之前已经完全实现的训练标签。
- gate 权重只在训练段末端验证窗口上计算，测试季度和留出窗口不可见。
- gate 目标是验证窗口的平均横截面 Rank IC，不使用 MSE 代替排序目标。
- 两个 expert 在确定 gate 后用全部已实现训练行重拟合；gate 权重保持冻结。
- context 缺失通过 availability mask 传播，不用中位数伪造估值。
- 结果必须与基础 expert、context expert 并列报告；gated 结果未超过基础结果时不得激活。

实现：`src/fundamental_embedding/causal_rank_context_gate.py`。

## 当前 667 标的 OOS 结果

在相同 14 个季度和相同公司集合上：

- 基础基本面 expert：平均 Rank IC `0.02733`；
- context expert：`0.01876`；
- Rank-IC gated：`0.02546`；
- gated 仍低于基础 expert，且 Top-Bottom 收益差也没有改善。

因此 gate 已经解决了“MSE 目标与排序目标不一致”的架构问题，但当前估值/行业 context 仍没有足够独立的 OOS 增量，只保留研究和风险解释用途。

评估产物：`data/analysis/causal_rank_context_gate_official_full_667_20260822/report.json`。
