# 当前主链路：策略搜参、买入执行与回测合同

状态：截至 2026-09-04 的代码与配置整理。

本文描述普通股票/ETF 策略主链路，不把独立的 ETF 期权 Collar 研究脚本混入普通策略优化器。

## 1. 主链路概览

```text
config/config.yaml
  └─ 选择策略插件（当前默认 technical_ensemble）
config/optimizer_constraints.yaml
  └─ 搜参器、Walk-Forward、Gate、执行参数和基准
        ↓
PIT 数据准备
  └─ qfq 指标数据 + raw OHLC 执行数据 + corporate actions
        ↓
WalkForwardManager
  └─ 生成 ranking / purge / holdout 窗口
        ↓
SearchController + Solver
  └─ ask → 候选评估 → Gate → tell
        ↓
TradingStrategy.make_signals()
  └─ 生成统一 TradePlan
        ↓
FastEvaluator / Backtester
  └─ 统一成交价、整手、手续费、持有期、分红和 NAV
        ↓
ValidationController
  └─ 参数敏感性、标的留一、Holdout
        ↓
保存 candidate artifact
  └─ 显式 --activate-run 后才成为生产策略
```

策略只负责生成 `TradePlan`，不负责选择 Solver、撮合成交或计算收益。统一接口见 `src/strategy/api.py`，统一回测入口见 `src/backtest/engine.py`。

## 2. 当前支持的策略插件

策略插件与搜索算法是两层概念：策略决定信号和目标仓位，Solver 决定如何搜索参数。

| 策略 ID | 逻辑 | 搜参维度 | 执行模型 |
| --- | --- | ---: | --- |
| `technical_ensemble` | 22 个技术因子加权评分；买入需连续 3 日确认 | 26 | target weight |
| `valuation_aware_ensemble` | 技术集成 + 历史可用估值/DCF 特征 | 27 | target weight |
| `regime_pullback` | MA200 上升趋势、回撤和恢复确认 | 16 | target weight |
| `percentile` | 每只标的自身 252 日指标分位评分 | 14 | cash cap |
| `builder` | 5 个买入规则 + 3 个卖出规则，支持 lock/reset/confirmation | 18 | cash cap |
| `simplified` | 简化条件信号 + 现金档位 | 18 | cash cap |
| `ma60_band` | MA60 上下 5%，四个固定 25% 仓位槽 | 0 | target weight |
| `capm_dcf_value` | CAPM-DCF 公允价值与入场价策略 | 2 | cash cap |

当前配置默认选择 `technical_ensemble`。不过日报会优先读取最新完整的 active optimizer run；没有 active artifact 时才回退到 `config/config.yaml` 的策略选择。

### 2.1 Cash-cap 策略

`builder`、`simplified`、`percentile` 和 `capm_dcf_value` 使用统一现金上限。买入和卖出上限分别从以下档位中搜参：

```text
10,000 / 20,000 / 30,000 / 40,000 / 50,000
```

这是单次订单现金上限，不是月度额度，也不是仓位比例。

### 2.2 Target-weight 策略

`technical_ensemble`、`valuation_aware_ensemble`、`regime_pullback` 和 `ma60_band` 直接输出目标仓位。技术集成和趋势回撤策略还会搜索：

- 单标的上限：15%～25%；
- 总仓位上限：60%～100%。

目标仓位变化时，执行器先处理退出和减仓，再按照目标仓位补仓；现金不足时按比例缩小买入，不会为了给新信号融资而强行卖出未满足持有期的普通仓位。

## 3. 当前搜参器

当前默认 Solver：

```yaml
search:
  solver_id: local_genetic
```

支持的 Solver：

- `local_genetic`
- `genetic`
- `random`
- `simulated_annealing`

当前 `local_genetic` 合同：

- 每个市场组预算 155,000 个候选；
- 初始随机候选 30,000 个；
- 保留前 10,000 个进入遗传阶段；
- 5 代；population 5,000；每代 offspring 25,000；
- crossover rate 70%；gene mutation rate 15%；
- 局部搜索步长最多 3，并逐代收缩到 1；
- random immigrant 10%；重复候选最多重试 64 次；
- 固定随机种子 `20260726`；
- 候选 batch size 128；
- `process` backend，外层使用可用 CPU，单个 Numba/BLAS worker 不再嵌套多线程；
- checkpoint 开启，候选保留池默认保留已评估候选的 5%。

所有候选都通过策略声明的 typed `ParameterSchema` 验证、归一化和缓存。策略不能读取 Solver 状态，也不能绕过统一评估器。

## 4. Walk-Forward 与评分

当前窗口合同是 84 个月：

| 项目 | 当前值 |
| --- | ---: |
| 状态/指标 lookback | 12 个月 |
| 每窗 test | 9 个月 |
| 滚动步长 | 3 个月 |
| 总窗口 | 22 |
| Holdout | 最新 4 窗 |
| overlap purge | 2 窗 |
| 实际 ranking | 16 窗 |

最新 4 个 holdout 窗口不参与候选排名、筛选或参数修改。由于它们彼此重叠，最终报告不把它们串成一条 36 个月复利曲线，而是采用等窗平均收益/超额/Sharpe，最大回撤取最差窗口。

主排序目标为：

```text
加权平均的多数基准超额收益
− 0.5 × ranking 窗口之间的收益范围
```

A 股默认控制基准为 `510880`、`510300` 和 `risk_free`，多数基准收益取三者中位数。这样可以减少策略只相对某一个 ETF 取得优势的情况。

## 5. 候选 Gate 与稳健性验证

当前 `standard` profile 的主要硬门槛：

- 加权策略收益大于 0；
- 正收益窗口至少 6 个；
- 平均多数基准超额收益大于 0；
- 多数基准胜出窗口至少 6 个；
- 平均仓位至少 5%；
- 最差窗口最大回撤不低于 -40%。

诊断指标包括跨窗口超额收益标准差、平均 Sharpe 和交易密度。

通过 ranking 的候选还会进行：

- 前 500 个候选的参数局部敏感性；
- 前 20 个候选的 leave-one-instrument-out；
- 标的顺序不变性检查；
- 最新 4 个 holdout 窗口验证。

实现上的注意点：当前 `ValidationController` 使用 `ParameterSchema.local_perturbations()` 枚举确定性的一阶参数邻域；配置中的 `sensitivity_samples` 目前没有参与这条实际验证路径。

## 6. 买入、卖出与成交价

所有策略先生成统一 `TradePlan`，然后由共享执行器处理：

- 同一天同时出现买卖信号时，卖出/退出优先，不做卖后买的 churn；
- 预热期和无效数据行不能下单；
- 订单按优先级稳定排序；
- 普通仓位最小持有期为 30 天；
- A 股 lot size 为 100；
- 初始资金为 100,000；
- 手续费率为 0.5%。

默认的保守成交价格合同：

- 买入价：前一日、当日、后一日最高价的最大值；
- 卖出价：信号当日最低价；
- NAV：收盘价估值；
- 最后一行因为没有后一交易日最高价，买入记为 pending，不成交。

后一交易日最高价只是执行延迟压力输入，不会被策略用于生成信号。

现金模型中，卖出先按卖出优先级处理，再按买入优先级处理；买入现金预算包含手续费，并按整手数量向下取整。

目标仓位模型中，退出/灾难性退出先处理，随后按单标的上限与总仓位上限计算目标；目标减少使用触发日最低价，新增仓位使用上述保守买入价。

## 7. 数据与 NAV 口径

生产优化器启用 PIT 数据准备时：

- qfq 数据用于技术指标和信号；
- raw OHLC 用于成交和 NAV；
- 现金分红在当日订单前入账；
- 拆分、合并等 share multiplier 在订单前应用；
- raw/qfq 覆盖不一致、行动信息不完整或不支持的公司行动会失败关闭；
- 当前配置要求 9 年 PIT 数据覆盖，优化器自身按 84 个月窗口并额外请求缓冲历史。

可交易基准也使用对应的成交价、手续费、整手和公司行动口径；报告同时保留策略收益、基准收益、超额收益、原始价格收益、胜率、MDD、Sharpe、交易数、平均仓位、最终现金和最终持仓等字段。

## 8. 搜参、激活与日报

主要命令：

```text
python main.py --optimize
python main.py --activate-run RUN_ID --group a_share
python main.py --once
python main.py --brief
```

`--optimize` 会为 A/HK/US 分别解析配置并分别运行；`--group` 可只运行一个市场。每个市场都生成独立的 `run_id` 和 `data/optimizer/runs/<run_id>/`，包括参数、窗口指标、搜索 archive、checkpoint、数据 readiness 和 holdout 结果。

当前代码会始终先保存为 candidate，不会因为相对 promotion policy 通过就自动改生产策略。必须显式执行 `--activate-run`。

日报读取 active artifact 后，使用同一套策略参数、成交合同和基准合同生成信号与组合评估，避免扫描器、日报和优化器各自实现一套交易逻辑。

### 8.1 三市场完全独立的策略、Solver 与激活

`config/config.yaml` 必须显式声明三个市场的完整优化合同；不存在 `optimizer.engine`、`optimizer.strategy_by_group`、全局 Solver 或隐式回退。例如：

```yaml
optimizer:
  markets:
    a_share:
      strategy: technical_ensemble
      solver_id: local_genetic
      gate_profile: standard
      walk_forward_profile: a_share_84m
      execution_profile: a_share_cny
      benchmark_profile: a_share
    hk:
      strategy: regime_pullback
      solver_id: simulated_annealing
      gate_profile: standard
      walk_forward_profile: hk_84m
      execution_profile: hk_hkd
      benchmark_profile: hk
    us:
      strategy: percentile
      solver_id: random
      gate_profile: exploratory
      walk_forward_profile: us_84m
      execution_profile: us_usd
      benchmark_profile: us
```

一次全市场搜参会分别调用三个市场的策略和 Solver，生成三个独立候选 run；报告有一个汇总入口，但每个市场单独展示 strategy、Solver、Gate、Walk-Forward、execution、benchmark、数据覆盖、ranking、holdout、MDD、Sharpe、收益、交易统计和状态。也可以只运行一个市场：

```text
python main.py --optimize --group a_share
```

搜参结果仍先保存为 candidate。通过绝对 Gate 后，用以下命令只激活指定市场；当前 active manifest 的其他市场 entry 会原样保留：

```text
python main.py --activate-run RUN_ID --group a_share
```

active manifest 为 schema v4，只保存各市场独立 entry 的 run、artifact、strategy、Solver、配置指纹和激活时间；不保存可回退的全局策略。旧版单策略或混合 manifest 不会自动读取，必须重新搜参并按市场显式激活。缺少某市场 entry 时，该市场为“未激活”，日报、信号扫描、回测和参考组合均 fail closed，不会套用其他市场策略。

机器人也支持改变下一次搜参范围和候选策略：

```text
/optimize a_share
/switch_optimizer a_share regime_pullback
```

### 8.2 机器人修改参考持仓

参考持仓必须已经通过 `/ref_date` 绑定到一个已激活运行。持仓修改使用显式的原生货币成交价，系统按该市场执行合同换算为 CNY，并追加一条 `manual_bot` trade log；不直接改持仓字段，也不改变绑定策略。`set` 表示目标股数，`buy`/`sell` 表示增减股数：

```text
/ref_position set a_share 510300 10000 3.85
/ref_position buy a_share 510300 1000 3.90
/ref_position sell a_share 510300 500 4.05
```

命令会校验市场归属、整手、价格、现金和可卖数量；重复的同一目标设置不会重复记账。每组参考持仓文件仍独立保存于 `data/ref_portfolio_a.yaml`、`data/ref_portfolio_hk.yaml` 和 `data/ref_portfolio_us.yaml`。

## 9. 当前标的限制与期权分支边界

当前配置中：

- `510300` 位于 `skip_search`，默认不参加普通主链路搜参；
- `510500` 位于 `skip_signals`，默认不参加普通日报信号扫描；
- 二者仍可以作为 A 股控制基准或在独立脚本中使用。

新浪期权数据源已单独支持中金所 IO/HO/MO，以及上交所 510300/510500 ETF 期权的 Call/Put 链和日线历史。但 `SinaOptionDataSource` 尚未注册为普通 `TradingStrategy`，也没有接入 `run_optimizer → FastEvaluator`。

因此，固定 100% Put、105% Call、按 NAV 重置的 Collar 仍属于独立期权研究分支，使用：

- `src/data/option_data.py`；
- `scripts/backtest_510300_collar.py`；
- `scripts/backtest_collar_nav_sizing.py`。

它与普通股票策略的 cash-cap/target-weight 回测合同不是同一个模型，不能直接把两类结果放在同一个 optimizer artifact 中解释。

## 10. 关键代码与配置

- 策略选择与生产调用：`main.py`
- 策略注册与统一 TradePlan：`src/strategy/registry.py`、`src/strategy/api.py`
- 搜参约束与执行参数：`config/optimizer_constraints.yaml`
- 主程序配置：`config/config.yaml`
- Walk-Forward、搜索和候选保存：`src/search/workflow.py`、`src/search/controller.py`
- Gate 与稳健性验证：`src/search/gates.py`、`src/search/validation.py`
- 成交价格和公司行动：`src/backtest/execution.py`
- 统一模拟与基准：`src/backtest/engine.py`

旧的 `config/optimizer.yaml` 保留了历史 Builder/Bayesian 配置说明，但不属于当前 `main.py` 主链路的权威搜参配置；当前主链路应以 `config/optimizer_constraints.yaml` 和策略插件声明为准。
