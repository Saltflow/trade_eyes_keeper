# 多专家反向 DCF 诊断（2026-08-22）

## 目的

反向 DCF 只回答“当前价格隐含了多高的未来增长”，不把市场隐含增长率回灌到公允价值或策略预测。折现率使用逐标的历史 beta 估计的 CAPM 股权成本；没有债务、现金和利息税项时，不伪造 WACC，也不把 CAPM 股权成本命名为 WACC。

## 667 个 A 股快照结果

输入为 `partial_first_pass_20260822/dataset` 的逐时点财务与行情，评价日为 2026-08-18。四个估值专家逐一计算：FCFE 代理 DCF、可分配盈利 DCF、股息折现和剩余收益。只有有正的股权现金流代理、CAPM 折现率和可解的增长区间时，才生成反向 DCF 候选。反向诊断边界显式为 `-50%~100%`，不改变内在价值预测的增长边界。

- 667 个快照中，43 个没有任何正的股权现金流代理；这是真实数据/经济口径缺失，不使用默认值填充。
- 577 个至少有一个可用专家可以用 CAPM 成本解出隐含五年增长率，其中 495 个有至少两个专家可交叉验证，82 个只有单一专家。
- 90 个仍不可解，主要是估值专家本身不可用或价格超出明确诊断边界，而不是用零值填充。
- beta 实际历史估计覆盖 662/667，CAPM 股权成本覆盖 667/667。这里的 CAPM 是 FCFE 代理的股权贴现率；资本结构数据不完整的标的没有被伪造为 WACC。

旧的“只取主专家”报告只有 251 个可解标的。多专家候选集合在相同逐时点数据上提高到 577 个，增加 326 个；其中 282 个专家结果的中位绝对离散度超过 5 个百分点，必须显示为高分歧画像，不能当作精确目标价。

逐行审计结果：

- `data/analysis/reverse_dcf_consensus_667_20260822/report.html`
- `data/analysis/reverse_dcf_consensus_667_20260822/report.json`
- `data/analysis/reverse_dcf_consensus_667_20260822/reverse_dcf_consensus_all.csv`

## 设计结论

反向 DCF 分类不能依赖单一 `dominant_expert`。`src/fundamental_embedding/reverse_dcf_consensus.py` 保留每个专家的现金流口径、CAPM 折现率、隐含增长率和状态，并在至少两个可解专家时给出按兼容性加权的中位数；只有一个时标记 `solved_single_expert`，没有时标记 `unresolved`。同时保留上下界和离散度，避免把股息模型与盈利模型的分歧伪装成确定性增长率。

模块合同为 `market-cost-reverse-dcf-consensus-1`。该模块是画像和研究输入，尚未接入生产搜参；接入量化策略前必须在带发布日期的历史逐时点面板上 OOS 验证，并把 `expert_count`、`dispersion` 和 `status` 作为质量掩码。
