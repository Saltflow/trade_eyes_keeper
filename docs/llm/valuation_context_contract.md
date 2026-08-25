# 历史估值上下文合同

`historical-valuation-industry-context-1` 是把逐时点反向 DCF/CAPM 与行业相对数据提供给量化层的唯一研究入口。它不让策略直接读取 DCF 原始现金流、价格或当前行业标签。

## 输入

输入必须是带日期的 `IndustryRelativeDataset`，并同时包含：

- 原始 19 个基本面字段；
- `valuation:beta`、`valuation:capm_cost_of_equity`、5 年市场隐含增长、市场-基本面增长差、基本面增长代理；
- 上述估值字段的历史行业相对副本。

所有输入字段必须满足 `published_at <= feature_date`；当前 Baostock 行业快照不得回填到历史。

## 输出

输出保留原始 19 个基本面字段，并附加 10 个低维上下文：

- beta、CAPM 成本、隐含增长、增长差和基本面增长的同日横截面 rank；
- 估值字段有效观测比例；
- 隐含增长/增长差是否同时可解；
- 行业内隐含增长和增长差 rank；
- 行业内估值观测比例。

正方向统一表示“更便宜或更低风险”：低 beta、低资本成本、低隐含增长、低市场-基本面增长差为正；基本面增长越高为正。原始缺失字段不会用中位数伪造，缺失只通过 availability mask 和质量字段表达。

实现：`src/fundamental_embedding/valuation_context.py`。

## 历史评估

在 667 个标的、14 个相同 OOS 季度上：

- 原始 19 列平均 Rank IC：0.02731；
- 加入 10 列上下文：0.02344；
- 平均 Rank IC 变化：-0.00387；
- Top-Bottom 平均收益差变化：-0.00367。

因此当前上下文只能作为可解释的估值/风险状态，不能自动成为收益排序 alpha。负结果同时说明：现阶段应该先提高历史 DCF 解出率、拆分现金流状态和风险状态，再重新评估；不得因为数据已经抓取就强行激活。

评估产物：`data/analysis/valuation_context_increment_official_full_667_20260822/report.json`。
