# 估值专家进入统一搜参的接口契约

## 接入边界

`run_optimizer` 增加了可选的 `context_enricher` 参数。默认值为 `None`，因此已有技术策略、活动运行和旧产物的行为不变；只有显式传入历史面板 enricher，基本面数据才会进入搜索。

上下文对象必须是可序列化的 callable，并提供稳定的 `contract_hash`。搜索工作流会在完整市场历史和每个训练/测试状态窗口上调用同一个 enricher，策略只看到带 `fundamental_as_of_dates` 的 `FundamentalStrategyMarketData`。缺少策略声明的基本面依赖或缺少 enricher 时直接失败，不会用当前快照静默填充历史。

数据指纹会同时包含行情矩阵、基本面值、可用性掩码、报告期、特征名和来源元数据；它进入 `data_contract_hash` 和 `feature_contract_hash`，避免用不同估值数据复用旧的搜参产物。隔离窗和留出窗仍由原搜索控制器管理，solver 不接触上下文实现。

## 当前消费者

新增注册策略 `valuation_aware_ensemble`：

- 复用 `technical_ensemble` 的 22 个固定方向技术特征和统一成交/持仓执行；
- 读取 `historical-valuation-context-panel-1` 中的估值与行业相对排名；
- 以类型安全的 `valuation_weight` 在技术分数和质量衰减后的估值专家分数之间做线性混合；
- 没有完整共识面板时拒绝生成信号；
- 标记为 `manual_activation`，不改变当前生产策略。

反向 DCF 共识的推荐构造方式是：

```python
from src.strategy.context_enrichment import make_consensus_context_enricher
from src.strategy.valuation_context_kernel import (
    VALUATION_CONTEXT_QUALITY_NAME,
    VALUATION_CONTEXT_SIGNAL_NAMES,
)

required = (*VALUATION_CONTEXT_SIGNAL_NAMES, VALUATION_CONTEXT_QUALITY_NAME)
enricher = make_consensus_context_enricher(dataset, required_feature_names=required)
results, constraints = run_optimizer(
    strategy,
    stocks_data,
    stock_codes,
    group="a_share",
    context_enricher=enricher,
)
```

这段接口不改变 `SearchController`、Solver、Backtester 或 Gate 的职责；增加其他估值/行业专家时，只需新增一个符合相同 callable 合同的 enricher，或新增策略消费者。

## 验证状态

已覆盖：因果 as-of 连接、未来日期拒绝、数据指纹、注册策略缺失上下文时 fail-closed、统一工作流传递面板、现有评分器兼容，以及 14 个定向回归测试。当前仍为研究候选，未自动激活或部署。
