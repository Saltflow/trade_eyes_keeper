# 历史基本面面板接入合同

`src/strategy/fundamental_panel.py` 是季度基本面数据进入策略输入的唯一时间桥接点。

## 时间规则

- 输入必须是 `quarterly-fundamental-pricing-data-*` 合同的数据集。
- 对每个标的、每个交易日，只选择 `feature_date <= market_date` 的最新季度行。
- 首个可用季度行之前全部缺失；不使用未来季度行，也不把当前截面广播到历史。
- 每个可用单元保存 `fundamental_as_of_dates`，便于回测审计。
- 同一标的、同一季度出现重复行，或交易日不严格递增时立即失败。

## 策略接入

```python
from src.strategy.fundamental_panel import (
    attach_historical_fundamental_dataset,
)

panel = attach_historical_fundamental_dataset(market_data, dataset)
panel.require_historical_walk_forward_eligibility()
```

返回的 `FundamentalStrategyMarketData` 携带特征名、来源日期、可用掩码和
`historical-fundamental-panel-1` 合同。技术策略不读取这些字段；未来基本面策略
通过同一 `StrategyMarketData` 接口声明所需特征后，再交给统一的
`EvaluationService` 和 `Backtester`。

当前行业分类快照仍标记为 `historical_walk_forward_eligible=false`，不能通过本桥接
层进入历史搜参。只有逐时点财务快照与带发布日期的行业分类同时齐备，才允许生成
可搜参面板。
