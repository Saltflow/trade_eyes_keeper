# 公共运行层：旧链路退役记录（2026-10-29）

用户已确认今天三次报表没有问题，并授权立即退役整合前的冗余链路。此前取消的性能硬门槛不再作为阻塞项；原始性能结果、报表的 `partial/data_not_ready` 记录仍保留，不改写为成功。

## 实际完成

- [x] **R01 线上清点**：20:10 北京时间只读核实生产 v1.18.0 / `6a55894` / 构建 `b7e30404`；观察到一个公共宿主及其 SDK，四模块 ready，无在途任务。四个旧脚本不存在，无旧业务进程、相关 cron/timer 或 HTTP 监听。本次没有改生产代码、配置、数据库或重启服务。
- [x] **R02 源码退役**：本机七个文件按 SHA 校验后应用已发布 `6a55894` 的薄入口和测试；删除专用 watchdog、安装脚本及专属测试。删除前逐字备份，不设置旧实现回退分支。
- [x] **R03 注册退役**：核对原任务的程序、参数、目录及 Disabled 状态，确认相关 Python 进程为零，保存可解析的原任务 XML 后注销 `TradeEyesPersonalMonitorTest`。全部匹配任务为零，不再自动拉起。
- [x] **R04 数据保护**：三份 SQLite 一致性备份均 `integrity_check=ok`，退役前后源文件 SHA、备份 SHA 和逻辑摘要全部一致。持仓、账户版本、历史、截图来源、去重及未来提醒保持原处，没有导入生产或删除旧数据库。
- [x] **R05 实际回归**：对实际落盘后的文件运行 258 项定向测试，全部通过、零跳过；包含 `tests/validation/`。七个替换文件 Ruff 全部通过。测试使用独立临时数据库和模拟外部提供方，没有启动真实机器人、发送消息/邮件或重跑报表。
- [x] **R06 文档与提醒**：更新迁移清单、架构及测试指南，旧监督器文档明确只作历史记录。删除已提前完成事项对应的周末退役提醒 `automation-2`。

## 删除、替换及保留范围

| 处理 | 代码/注册 | 结果 |
| --- | --- | --- |
| 删除 | `scripts/personal_monitor_watchdog.py` | 专用监督、恢复及告警循环退役 |
| 删除 | `scripts/install_personal_monitor_watchdog.ps1` | 不再安装旧链路 |
| 删除 | `tests/interactive/test_personal_monitor_watchdog.py` | 专属旧实现测试退役 |
| 注销 | `TradeEyesPersonalMonitorTest` | 本机任务不存在，原 XML 保留 |
| 替换 | `scripts/run_personal_monitor_feishu_test.py` | 删除独立模型/业务循环，仅保留显式只读发现 |
| 替换 | `scripts/run_personal_monitor_feishu_group_test.py` | 删除旧消费入口；业务必须显式 `--host-runtime` |
| 替换 | `scripts/personal_monitor_shared_test.py` | 必须由公共宿主注入执行器，不再另建执行器/健康循环 |
| 保留 | 原业务事务、持仓数据、截图、outbox、历史数据库 | 不搬迁、不清空、不回卷 |
| 保留 | SDK/HistoryBot 传输、FileLease、公共到期调度 | 仍由公共宿主使用，不是重复业务执行器 |
| 保留 | 研究 worker 与优化器执行合同 | 不属于本轮迁移，不能误删 |
| 保留 | 旧 HTTP 服务的 `/dev/null` mask 及部署器清理兼容代码 | 防误启动/清除旧部署的保护；masked/inactive、PID 0，不是运行链路，不新增进程或调度 |

七个替换/新增文件的完整清单及前后 SHA 见 `local_apply.json`；四个测试文件分别覆盖 profile 接管、群入口、发现入口和最终原生 profile 合同。七文件均逐字等于已发布版本，因此本次不需要新业务版本或生产重启，也没有混入本机其他实验。

## 验证命令、证据与边界

证据根目录：`data/acceptance/runtime_retirement_20261029/`。实际完成时间、源码 SHA、SQLite 摘要、任务参数和回归结果保存在 `local_apply.json`、`local_task_retirement.json`、`task_before.xml`、`task_xml_encoding.json`、`pytest.xml`、`verification.json` 及 `server_before/actual_readonly.json`。源码备份在 `source_before/`，一致性数据库备份在 `databases_before/` 和 `databases_after/`；这些私人证据不应提交公开仓库。

```powershell
python -X utf8 -m pytest -o addopts= tests/test_runtime_profile_handoff.py tests/interactive/test_personal_feishu_group_test.py tests/interactive/test_personal_feishu_test_launcher.py tests/interactive/test_personal_monitor_native_profile.py tests/interactive/test_retired_bridge_successors.py tests/interactive/test_shared_personal_routing.py tests/interactive/test_shared_conversation_regressions.py tests/test_execution_health.py tests/validation/ --basetemp=.tmp/runtime_retirement_20261029 -q --junitxml=data/acceptance/runtime_retirement_20261029/pytest.xml
python -m ruff check scripts/personal_monitor_shared_test.py scripts/run_personal_monitor_feishu_group_test.py scripts/run_personal_monitor_feishu_test.py tests/test_runtime_profile_handoff.py tests/interactive/test_personal_feishu_group_test.py tests/interactive/test_personal_feishu_test_launcher.py tests/interactive/test_personal_monitor_native_profile.py
```

生产当前 owner 的判断依据是 OS 进程及新鲜宿主状态；因存在 live WAL，本次没有绕过 WAL 查询 `execution_owners`，不宣称全库历史 owner 只有一行。真实截图/纠错的历史证据保留版本边界，本轮没有重新做人类入站验收，因此 M13 继续单列未完全验收。今天三报由用户确认，不替代这些对话补验，也不把历史数据未就绪改成数据已就绪。
