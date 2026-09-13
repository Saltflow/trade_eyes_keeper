# 云端三市场搜参故障修复（2026-09-07 / 08）

## 根因与实际影响

9 月 6、7 日的定时搜参都在启动配置校验阶段退出，尚未进入任何市场的搜索。
云端代码已是 `72965cb`，但不受 Git 管理的 `config/config.yaml` 仍只有旧配置
`optimizer.engine: technical_ensemble`，没有 `optimizer.markets`。

日志中的直接异常是：

```text
ValueError: global optimizer fallback fields are forbidden: engine
```

部署流程此前默认只同步 `alerts.yaml`，恢复服务器旧配置后没有验证三市场合同；
即使显式传入 `--sync-config`，上传的新配置也会被随后的恢复操作覆盖。
因此“代码部署成功”不代表实际运行配置能被新版优化器接受。

这不是三个市场同时遇到 Solver 崩溃，也没有证据表明这两次失败来自 OOM。

## 已实施的修复

1. 云端仅更新 optimizer 市场映射，保留原股票池、84 个月窗口、预算、profile、
   参考日期及通知配置。完整配置先备份、先校验，再原子替换；不增加任何回退。
2. 部署调整为先恢复云端配置，再应用显式同步；同步失败立即停止。
   对最终生效的配置调用与运行时相同的严格校验，失败时不继续系统测试、cron、
   服务重启或成功通知。示例配置显式列出三个市场。
3. optimizer guard 的错误报告携带具体配置原因，启动阶段没有 run 时，也在
   `data/optimizer/failures/<timestamp>_<market>/` 保存独立失败记录。
   `--group` 的错误记录不混入其他市场。
4. 无候选也写入 `*_search_diagnostics.yaml`，保留 Solver/Gate/参数 schema 和
   配置指纹、实际评估数、各硬 Gate 淘汰次数。淘汰次数可重叠，不能相加当作候选数。
5. 正常返回路径均保存单市场 `run_summary.yaml` 和 `*_run_status.html`；
   运行异常保留具体错误，不再只报告“市场失败”。报告区分“搜索完成但无候选”与
   “计算中断”，没有候选时不虚构收益、Sharpe 或 holdout 指标。
6. 保留清理按市场保留最近的终态运行，包含有候选、无候选及失败诊断。
   诊断记录不是候选 manifest，不能被激活，也不会放宽 Gate。

云端原配置备份：

```text
/root/trade_eyes_keeper/config/config.yaml.before-market-repair-20260907T191048.bak
```

市场合同：

| 市场 | Strategy | Solver | Gate | Walk-Forward | Execution | Benchmark |
| --- | --- | --- | --- | --- | --- | --- |
| a_share | technical_ensemble | local_genetic | standard | a_share_84m | a_share_cny | a_share |
| hk | regime_pullback | simulated_annealing | standard | hk_84m | hk_hkd | hk |
| us | percentile | random | exploratory | us_84m | us_usd | us |

## 云端全量验证

9 月 7 日 19:11:30 启动配置全量股票池与原始预算，20:30:36 完成。
验证禁用外发通知：`SKIP_NOTIFICATIONS=true`；没有自动激活。

| 市场 | 独立 run | 搜索结果 | 激活资格 |
| --- | --- | --- | --- |
| a_share | `20260907T191130932244_technical_ensemble_a_share` | 155,000 次配置预算评估，保留 500 个候选 | `eligible=false`，holdout、universe robustness 未通过 |
| hk | `20260907T201457920564_regime_pullback_hk` | 搜索完成，无有效候选 | 无候选，不激活 |
| us | `20260907T202542050693_percentile_us` | 10,000 次配置预算评估，保留 500 个候选 | `eligible=false`；exploratory Gate 不可用于生产激活 |

日志：`/root/trade_eyes_keeper/logs/optimizer_repair_20260907.log`。

首次运行暴露了第二个缺陷：HK 无候选，没有 candidate manifest，被原清理规则
立即删除，连同 archive、checkpoint、readiness 一起丢失。此次修复将终态诊断纳入
独立市场保留，需单独全量复跑 HK 验证淘汰归因，不重跑已经完成的 A/US 搜索。

原清理流程先删除 3 个未受保护旧 run（约 4.64 GiB），随后删除 HK 无候选 run
（约 0.03 GiB）；活动指针引用的目录保留，未发现这些被删产物的备份。

## 尚未消失的数据与策略限制

- A 股部分标的不满 84 个月、历史过旧或缺少可解释的公司行动；按原合同排除。
  首轮输入 12 个可搜索配置标的，最终 4 个通过历史校验；没有改动配置股票池。
- 港股 5 个、美股 3 个标的使用了通过现有时效校验的 PIT 库；补数请求仍遇到
  Yahoo HTTP 403。`query1`、`query2` 都返回 403，并非换一个域名即可修复。
- 补数器按自然日结束日期检查完整性，凌晨或休市日可能报告未覆盖当天；运行时
  PIT 时效检查与补数完整性检查是两个不同口径，本次未降低任一数据门槛。
- 旧 schema v2 活动指针没有被自动迁移或激活；候选存在不等于允许生产使用。
  必须完成各市场的验证，再由用户明确按市场激活。
- Telegram 出口仍报 `Network is unreachable`；没有自动换用未知代理或更改通知
  配置。9 月 8 日定时搜参的飞书完成通知成功送达，Telegram 失败不影响搜索结果。

## 验证记录

- 首轮定向配置/部署/激活/guard 回归：67 passed。
- `pytest tests/validation/`：36 passed。
- 首轮全量：829 passed、7 failed、7 skipped、1 xfailed；其中 6 项因验证进程
  设置 `SKIP_NOTIFICATIONS=true` 与通知构造测试预期冲突。关闭真实发送、恢复
  通知对象构造后的相关测试为 8 passed。
- 另 1 项为真实美股行情探针：返回最新日期 9 月 4 日，9 月 7 日按“2 个自然日”
  判断为过旧；不能把本次全量测试描述为全绿。
- 新终态记录/无候选合同测试：6 passed；本地组合定向回归 187 passed、2 skipped。
- 最终云端定向回归（含配置、部署、保留、guard、激活、validation）：180 passed。
  部署预检返回 `[OPTIMIZER_CONFIG_OK]`，原 A/US candidate 均通过保留识别。
- 最终全量回归（允许构造通知对象，但阻止真实 HTTP 写入和 SMTP 连接）：
  908 passed、1 failed、7 skipped、1 xfailed。唯一失败仍为上述真实行情探针，
  9 月 8 日读取的最新日期是 9 月 4 日。JUnit 结果在
  `cache/analysis/cloud_repair_20260908/pytest.xml`。

## 补丁部署与 HK 复核

只上传本次修复的 8 个运行/配置文件和 4 个回归测试文件，不混入本地未提交的
策略研究、Collar 或插件重构。所有目标逐文件校验部署前/后 SHA-256，原文件备份于：

```text
/root/trade_eyes_keeper/.repair-20260908-_tcbzmwb/backup/
```

`deployment_manifest.json` 保存部署清单。云端三市场配置指纹与首轮完全相同，
活动指针 SHA-256 保持
`d7d8d1354db90f66a27138893ca64c57ad24b0f6b53a782720188a6bb7f68ad8`。
首次上线通过逐文件热修完成；后续 Git 归档仅包含本次修复，不混入其余研究改动。

HK 复核使用 `python3 main.py --optimize --group hk`、完整 5 个配置标的、原
10,000 次预算，并禁用外发通知；日志为 `logs/optimizer_repair_hk_20260908.log`。
独立 run 为 `20260908T003655328605_regime_pullback_hk`，00:47:37 正常结束：
实际评估 10,000 次，ranking 可行候选 0，archive 无无效记录。

| 硬 Gate | 淘汰次数 |
| --- | ---: |
| 多数基准平均超额必须为正 | 10,000 |
| 多数基准胜出窗口数至少 6 | 10,000 |
| 正收益窗口数至少 6 | 9,990 |
| 平均仓位至少 5% | 9,990 |
| 加权策略收益必须为正 | 2,160 |

这些计数可重叠。这是策略未过 Gate，不是 Solver 崩溃；未修改策略参数或降低门槛。
`run_summary.yaml`、`hk_search_diagnostics.yaml`、`hk_run_status.html`、
`data_readiness.json`、checkpoint 和 archive 六类产物全部保留，终态记录通过新的
保留识别；原 A/US 候选目录也仍存在。可读诊断副本位于
`cache/analysis/cloud_repair_20260908/hk_verification/`。

## 修复后的自动定时任务验收

9 月 8 日 08:49 再次只读核验，02:00 定时任务已在 03:16:04 完成三市场：

| 市场 | 独立 run | 实际评估 | 保留候选 | 状态 |
| --- | --- | ---: | ---: | --- |
| a_share | `20260908T020003642247_technical_ensemble_a_share` | 155,000 | 500 | completed / candidate |
| hk | `20260908T025936099009_regime_pullback_hk` | 10,000 | 0 | completed / no_candidates |
| us | `20260908T031053164771_percentile_us` | 10,000 | 500 | completed / candidate |

定时 HK 的 Gate 淘汰次数与单独复跑一致，两轮 HK 的无候选诊断均未被之后的
US 搜索清理。三个配置指纹不变，所有新运行 `activated=false`，活动指针 SHA-256
仍与部署前一致。健康服务为 `active`，飞书完成通知已送达；邮件 HTML 副本已归档。
这验证了真实定时入口，不仅是手动命令或本地单元测试。

## 补充：旧 v2 活动指针迁移（2026-09-08 20:45）

### 现象

9 月 5 日部署 v4 独立市场激活代码（72965cb + 当日热修）后，早盘/收盘简报中的
三个参考持仓全部显示「停单」，日志反复出现：

    ERROR - 参考持仓A股 固定运行或合同不可恢复；跳过交易，需手动 /ref_date 重置

### 根因

加载器自 72965cb 起只接受 schema_version: 4 的活动指针与运行 manifest，并
要求每个市场条目含 run_id/artifact/strategy/solver_id/gate_profile/config_hash，
且 artifact 自带与条目一致的 market_config_hash。生产环境的活动指针与
20260823T020002534795_technical_ensemble 运行仍然是 schema v2：
load_strategy_run() 直接返回 None → 绑定校验失败；同时
load_latest_strategy_run() 也为 None，日报/简报策略扫描从「活动策略」退化为
0 信号。9 月 6-8 日的新 run 均 activated=false（A 股 holdout/universe 未过、
港股无候选、美股 exploratory Gate 不可用于生产激活），不能替代旧指针。

### 迁移方式

新增显式运维脚本 scripts/migrate_legacy_optimizer_pointer.py（任何调度器都
不会自动调用）：

1. 校验当前市场配置可解析；按「旧策略/求解器今天重新求解」的同一契约计算各
   市场 v4 config_hash（沿用当前 walk_forward/execution/benchmark profile 与约束）。
2. 备份到 data/optimizer/migrations/<时间戳>_legacy_v2_to_v4/
   （latest_strategy.yaml、运行 manifest、三个 artifact）。
3. 三个 artifact 注入 market_config_hash（与条目 config_hash 一致）；运行
   manifest 与活动指针原地升级为 v4（保留 activated: true、原 run_id、
   三个市场条目）。
4. 迁移后验证：load_latest_strategy_run() 恢复三市场；参考持仓绑定条件
   （strategy / params_hash / exec_hash）全部为 True；不修改任何参考持仓文件，
   不重新评估 Gate。

### 结果

- 活动指针已从 v2 升级为 v4；原文件备份于
  data/optimizer/migrations/20260908T204505_legacy_v2_to_v4/。
- 三个资金池的绑定校验全部通过，下一次简报将恢复调仓；日报/简报策略扫描
  恢复使用活动策略（不再恒为 0 信号）。
- 新候选（9 月 7-8 日各市场 run）仍保持 activated=false：候选不等于允许
  生产使用，后续验证通过并经用户明确激活后才能替代本迁移指针。
- 迁移刻意保留了「同一 run 服务三个市场」的历史形态；后续若按市场分别激活
  新 run，activate_run 会逐市场替换活动索引条目，与本次迁移不冲突。