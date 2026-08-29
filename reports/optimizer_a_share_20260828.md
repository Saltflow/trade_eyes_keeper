# A 股完整搜参报告

执行 run：`20260828T081648527167_technical_ensemble`
产物时间：`2026-08-28T08:46:54.061971`（Asia/Shanghai）

## 1. 执行结论

本报告直接由本轮保留的 `a_share_best_params.yaml` 生成，包含最佳候选的参数、
22 个窗口、Gate、局部敏感性和 leave-one-out 稳健性结果。

- Solver 主预算：`155000`；实际保留 survivors：`500`。
- WF score：`-3.811875`；最终 selection score：`-5.578750`。
- 本轮只跑 A 股，不能组成三市场完整候选，因此没有发布或激活。
- 当前活动策略保持：`20260730T041138145386_percentile` / `percentile`。

## 2. 搜参标的边界

配置池没有删除任何标的。A 股配置共 19 只；原有 `skip_search` 命中 6 只：
`000958`、`159655`、`510300`、`512810`、`513520`、`518660`。

本轮数据合同预检只对 Solver 输入做过滤，另外排除 5 只覆盖不足 84 个月的标的：

- `508091`：2025-09-29 起
- `515180`：2019-11-26 起
- `588510`：2026-05-29 起
- `520810`：2025-12-22 起
- `520650`：2025-10-29 起

实际进入本轮 Solver 的 8 只：`000333`、`002594`、`510500`、`600011`、
`600036`、`601088`、`601398`、`601985`。以上 5 只仍保留在配置、回填和日报/监控范围内。

## 3. 搜索合同与执行语义

- 策略：`technical_ensemble`；参数 schema：`technical-ensemble/2`。
- Solver：`local_genetic`；Gate profile：`standard`。
- 窗口：22 = 16 ranking + 2 purge + 4 holdout；状态回看 12 个月；每窗口测试 9 个月；步长 3 个月。
- 总时间窗口 84 个月；holdout 为 4 个重叠的 9 个月窗口，累计测试月数 36 个月。
- 指标/信号使用 qfq；成交、持仓估值和 NAV 使用 raw；现金分红、送转股和成本按公司行为模拟。
- 买入成交模型：`max_high_t_minus_1_t_t_plus_1`；卖出成交模型：`trigger_day_low`；执行模型：`target_weight`。

### Solver 配置

```yaml
budget: 155000
phase1_random_samples: 30000
phase1_top_keep: 10000
num_generations: 5
population_size: 5000
offspring_size: 25000
crossover_rate: 0.7
gene_mutation_rate: 0.15
mandatory_local_mutation: true
max_local_step: 3
step_schedule: linear_to_one
random_immigrant_rate: 0.1
duplicate_retry_limit: 64
random_seed: 20260726
```

### 最佳候选参数

```yaml
weight_close: 4
weight_ma60: 2
weight_deviation: 0
weight_rsi: 3
weight_macd: 2
weight_macd_signal: 1
weight_macd_hist: 0
weight_vol_ratio: 2
weight_boll_pct_b: 2
weight_adx: 0
weight_atr: 1
weight_adx_pct: 1
weight_rsi_pct: 4
weight_deviation_pct: 3
weight_vol_ratio_pct: 1
weight_ma200_dev_pct: 4
weight_high: 0
weight_low: 0
weight_ma200: 3
weight_ma200_slope: 1
weight_plus_di: 0
weight_minus_di: 2
buy_threshold: 3
sell_threshold: 7
per_symbol_cap: 0
total_exposure_cap: 0
```

### 执行参数

```yaml
model: target_weight
per_symbol_cap: 0.15
total_exposure_cap: 0.6
min_holding_calendar_days: 30
buy_price_model: max_high_t_minus_1_t_t_plus_1
sell_price_model: trigger_day_low
```

## 4. 22 个窗口明细

收益、excess 和多数基准超额单位均为百分比；P 为 purge isolation，H 为 holdout。

| # | 类型 | 训练区间 → 测试区间 | 收益 | excess | 多数基准超额 | 最大回撤 | Sharpe | 交易 | 末仓位 | 期末持仓 |
|---:|:---:|:---|---:|---:|---:|---:|---:|---:|---:|:---|
| 1 | R | 2019-08-27~2020-08-27 → 2020-08-27~2021-05-26 | 7.01% | -2.09% | 2.62% | -12.80% | 0.7982 | 5 | 58.17% | 000333, 002594, 510500, 600036 |
| 2 | R | 2019-11-27~2020-11-27 → 2020-11-27~2021-08-26 | 7.13% | 2.50% | 5.68% | -10.38% | 0.8656 | 3 | 37.81% | 000333, 002594, 600036 |
| 3 | R | 2020-02-27~2021-02-27 → 2021-02-27~2021-11-26 | -0.55% | -5.11% | -1.99% | -3.27% | -0.0926 | 3 | 44.88% | 510500, 600036, 601985 |
| 4 | R | 2020-05-27~2021-05-27 → 2021-05-27~2022-02-26 | 0.38% | -3.23% | -1.06% | -4.39% | 0.1243 | 2 | 29.85% | 510500, 601985 |
| 5 | R | 2020-08-27~2021-08-27 → 2021-08-27~2022-05-26 | 3.44% | 2.05% | 5.86% | -5.70% | 0.5542 | 5 | 47.07% | 510500, 601088, 601985 |
| 6 | R | 2020-11-27~2021-11-27 → 2021-11-27~2022-08-26 | 4.95% | -2.09% | 3.51% | -3.04% | 1.1982 | 1 | 17.87% | 601088 |
| 7 | R | 2021-02-27~2022-02-27 → 2022-02-27~2022-11-26 | 4.35% | 2.91% | 2.97% | -3.04% | 0.9255 | 1 | 17.40% | 601088 |
| 8 | R | 2021-05-27~2022-05-27 → 2022-05-27~2023-02-26 | -1.09% | -4.31% | -2.53% | -2.37% | -0.4003 | 1 | 11.26% | 601088 |
| 9 | R | 2021-08-27~2022-08-27 → 2022-08-27~2023-05-26 | 0.00% | -3.50% | -1.41% | 0.00% | 0.0000 | 0 | 0.00% | 现金 |
| 10 | R | 2021-11-27~2022-11-27 → 2022-11-27~2023-08-26 | 0.00% | -3.33% | -1.44% | 0.00% | 0.0000 | 0 | 0.00% | 现金 |
| 11 | R | 2022-02-27~2023-02-27 → 2023-02-27~2023-11-26 | 0.00% | -2.99% | -1.44% | 0.00% | 0.0000 | 0 | 0.00% | 现金 |
| 12 | R | 2022-05-27~2023-05-27 → 2023-05-27~2024-02-26 | 0.00% | -5.05% | -1.42% | 0.00% | 0.0000 | 0 | 0.00% | 现金 |
| 13 | R | 2022-08-27~2023-08-27 → 2023-08-27~2024-05-26 | 1.44% | -4.37% | 0.05% | -1.14% | 1.0924 | 1 | 16.29% | 601088 |
| 14 | R | 2022-11-27~2023-11-27 → 2023-11-27~2024-08-26 | 1.81% | -2.94% | 0.37% | -2.84% | 0.7937 | 2 | 30.19% | 601088, 601985 |
| 15 | R | 2023-02-27~2024-02-27 → 2024-02-27~2024-11-26 | -1.15% | -12.23% | -2.59% | -6.10% | -0.1585 | 7 | 55.44% | 002594, 601088, 601398, 601985 |
| 16 | R | 2023-05-27~2024-05-27 → 2024-05-27~2025-02-26 | 0.87% | -9.62% | -0.57% | -5.31% | 0.1828 | 6 | 52.98% | 002594, 601088, 601398, 601985 |
| 17 | P | 2023-08-27~2024-08-27 → 2024-08-27~2025-05-26 | 5.80% | -10.05% | 0.12% | -2.65% | 1.5925 | 3 | 28.66% | 002594, 601398 |
| 18 | P | 2023-11-27~2024-11-27 → 2024-11-27~2025-08-26 | 0.00% | -15.36% | -5.80% | 0.00% | 0.0000 | 0 | 0.00% | 现金 |
| 19 | H | 2024-02-27~2025-02-27 → 2025-02-27~2025-11-26 | -0.53% | -15.77% | -6.15% | -1.34% | -0.6329 | 1 | 14.21% | 510500 |
| 20 | H | 2024-05-27~2025-05-27 → 2025-05-27~2026-02-26 | 2.72% | -20.51% | -3.46% | -1.34% | 1.5928 | 1 | 16.80% | 510500 |
| 21 | H | 2024-08-27~2025-08-27 → 2025-08-27~2026-05-26 | 2.97% | -7.60% | 1.59% | -2.38% | 1.2418 | 1 | 17.00% | 510500 |
| 22 | H | 2024-11-27~2025-11-27 → 2025-11-27~2026-08-26 | 0.00% | -9.60% | -1.65% | 0.00% | 0.0000 | 0 | 0.00% | 现金 |

期末持仓的精确 shares、cost、price、value、weight、pnl，以及每个窗口的基准原始收益，见同 run 的 YAML 产物。

## 5. Ranking 窗口汇总与 Gate

### Ranking 汇总

| 指标 | 结果 |
|:---|---:|
| 加权策略收益 | 1.79% |
| 正收益窗口 | 9/16 |
| 多数基准超额均值 | 0.41% |
| 多数基准胜出 | 7/16 |
| 平均仓位 | 16.25% |
| 最差回撤 | -12.80% |
| excess 标准差 | 3.81% |
| 平均 Sharpe | 0.3677 |
| WF score | -3.811875 |

### Gate 逐项结果

| 规则 | 模式 | 期望 | 实际 | 结果 |
|:---|:---|---:|---:|:---:|
| `positive_weighted_return` | hard | gt 0.00 | 1.79 | PASS |
| `positive_return_windows` | hard | ge 6.00 | 9.00 | PASS |
| `positive_mean_majority_excess` | hard | gt 0.00 | 0.41 | PASS |
| `majority_benchmark_win_windows` | hard | ge 6.00 | 7.00 | PASS |
| `minimum_average_position` | hard | ge 5.00 | 16.25 | PASS |
| `maximum_drawdown` | hard | ge -40.00 | -12.80 | PASS |
| `maximum_excess_instability` | diagnostic | le 15.00 | 3.81 | PASS |
| `sharpe_quality` | diagnostic | ge 0.50 | 0.37 | FAIL |
| `trade_density` | diagnostic | ge 0.00 | 0.00 | PASS |

Ranking hard Gate 全部通过；`sharpe_quality` 是 diagnostic，实际未达到 0.5，但不改变上述 hard Gate 结论。

## 6. 敏感性与 Universe Robustness

### 局部参数敏感性

- 样本：`41`；可行：`38`；可行比例：`92.68%`。
- base score：`-3.811875`；最差可行 score：`-5.506250`；drop：`1.694375`。
- 局部稳健性：`PASS`。

### Leave-one-out 标的稳健性

- 8 个 leave-one-out 变体中正变体 `8` 个；要求 `7` 个。
- leave-one-out：`PASS`；symbol order invariant：`FAIL`。
- 总体 Universe Robustness：`FAIL`；失败原因是候选结果不满足顺序不变性。

| 移除标的 | 多数基准超额均值 | 变化 | 变体结果 | Gate feasible |
|:---|---:|---:|:---:|:---:|
| `000333` | 1.33% | -0.92% | PASS | PASS |
| `002594` | 0.71% | -0.30% | PASS | PASS |
| `510500` | 1.45% | -1.03% | PASS | PASS |
| `600011` | 1.24% | -0.82% | PASS | PASS |
| `600036` | 0.83% | -0.42% | PASS | PASS |
| `601088` | 0.38% | 0.04% | PASS | FAIL |
| `601398` | 1.07% | -0.66% | PASS | PASS |
| `601985` | 1.36% | -0.94% | PASS | PASS |

## 7. Holdout 结论

4 个 holdout 窗口不进入 Solver，只在最终候选验收阶段计算。多数基准超额依次为：
`-6.15%`、`-3.46%`、`+1.59%`、`-1.65%`，仅 1/4 为正，因此 `holdout_passed = false`。

### Holdout 整体汇总

4 个窗口存在重叠，不能把它们直接串联复利为 36 个月净值。整体口径为：收益、超额和 Sharpe 取 4 个窗口等权均值，最大回撤取最差窗口。

| 指标 | 整体结果 |
|:---|---:|
| 整体收益（窗口均值） | **1.29%** |
| 整体最大回撤（最差窗口） | **-2.38%** |
| 整体超额（相对每窗最强基准，均值） | **-13.37%** |
| 多数基准超额（相对每窗控制基准中位数，均值） | **-2.42%** |
| 整体 Sharpe（窗口均值） | **0.5504** |

其中多数基准超额是 Holdout Gate 使用的口径；整体汇总已写入产物 `holdout_summary`。

最终 activation：

```yaml
eligible: false
holdout_passed: false
benchmarks_complete: true
local_robustness_passed: true
universe_robustness_passed: false
requires_manual_activation: true
absolute_eligible: false
relative_promotion_passed: false
```

## 8. 性能与可复现产物

```yaml
scoring_seconds: 0.0
simulation_seconds: 14678.48787603504
scheduling_seconds: 1123.9572116979398
cache_hits: 8
evaluated: 176579
worker_cpu_seconds: 11305.875
worker_wall_seconds: 14854.56443250482
process_task_count: 49599
process_batch_count: 1719
cache_hit_rate: 4.530344815869798e-05
evaluator_capabilities:
  backends:
  - cpu_scalar
  - cpu_batch
  - cpu_process
  active_backend: cpu_process
  batched: true
  gradients: false
  gpu: false
process_workers: 16
retained_records: 21587
retained_cache_entries: 21579
```

- 完整最佳候选与窗口级原始结构：
  [a_share_best_params.yaml](E:/CodeBases/trade_eyes_keeper/data/optimizer/runs/20260828T081648527167_technical_ensemble/a_share_best_params.yaml)
- 155,000 次候选搜索归档：`E:/CodeBases/trade_eyes_keeper/data/optimizer/runs/20260828T081648527167_technical_ensemble/a_share_search_archive.jsonl`
- 本次使用 `SKIP_EMAIL=true` / `SKIP_NOTIFICATIONS=true`，没有发送生产通知。

## 9. 合同哈希

```yaml
gate_profile_hash: e9c61346898ecc2cf9be3a26b75f669188f9d0162941ab024e3286407645680c
search_contract_hash: b0bdce3140a9729ce435e27487080e193a10901de05b194a91200fea03d7d47d
parameter_schema_hash: 81f341cf08e02696625fc95ba3ad874a518d1714fe98bd7a7a401188dd108397
feature_contract_hash: a099487a68a39081eb611aa61bdefda9fc29be6323848bfe4624bbaef8f80a67
data_contract_hash: 7c9c318205c67b5ab89fe98ea4de3be0f5343c2015688b0985fa82cec77f97a4
execution_contract_hash: ec53425068e4cdced93bbb54138e53ad820d4cfe9711c532358d76d71715f90a
window_contract_hash: 105c5dac7078cb72ee315355832844e36da017adcf1e27d8e77f110f5326739a
```
