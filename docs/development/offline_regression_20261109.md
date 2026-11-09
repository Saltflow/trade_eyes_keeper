# 两层项目工具：原功能离线回归（2026-11-09）

状态：本轮离线验收完成。修复后按用例身份去重，5611 passed、19 skipped、0 未解决失败；含受控补充验收用例。真实行情 e2e 单列排除，不计为通过。基线 `9d8e4cabb571d63a415e0d74dc54ec2166387ec9`；工作区保留此前未提交内容，本轮没有提交、推送、部署或真实通知。

## 验收边界

- 全仓 pytest 排除标记为 smoke / integration / e2e 的外部依赖用例；另跑最终修改版本的机器人、个人持仓、报告、资料读取与触发专项。
- 主 pytest 进程使用 Python audit hook 拦截非本机网络连接和 DNS；保留 guard 路径的 Python 子进程继承该保护。自行重建 PYTHONPATH 的测试子进程依赖其本地回调/mock 边界，不能把此钩子描述为系统级防火墙。模型使用受控响应，Pi 桥接使用本地实际 Node 子进程，通知传输使用本地回调或 mock。
- `main.py --help`、`--status` 实际离线运行；本机没有可用服务心跳，返回 unavailable，不据此判断生产状态。
- 首轮失败和复现证据保存在 `.tmp/offline_regression_20261109/`，后续复验使用新文件，不覆盖原始失败。最终计数按测试身份去重，不能将重跑数量简单相加。

## 新增回归与修复

| 编号 | 跨接口复现 | 最小修复及验收 |
|---|---|---|
| O01 报告使用旧配置 | 显式 research profile 下，修改监控池后，新报告预览校验当前配置，但实际队列 snapshot 仍使用启动配置 | 在既有 ConfigStore 锁内校验、读取、再校验并提交同一份配置；不改个人 profile 的配置隔离。覆盖两种 profile、新旧监控池/渠道、确认前与读取期间变更、入队时并发写入 |
| O02 引用坐标不一致 | TXT/MD 的换页符、垂直制表符或 Unicode 行分隔符被搜索额外拆行，按返回行号读取却得到其他正文；既有 `【PAGE n】` 标记失去页号 | 原始文件和缓存解析共用与文件读取一致的 CR/LF/CRLF 分行；保持控制字符原文，显式中英文页标兼容，不从普通正文猜页。覆盖搜索→旧 read_text→data_read，以及缓存 PDF/HTML |
| O03 工具参数契约不一致 | data_search schema 允许 query 256 字符、limit 100，实现只接受 120/50；合法模型调用会被拒绝 | 公布实际支持的 120/50 上限，并验证边界参数可执行、超限一致拒绝 |

O01 原始失败：`bot/profile.xml`；O02 修复前后：`catalog/citation_control_repro.before.json`、`citation_control_repro.json`、`page_marker_repro.before.json`、`page_marker_repro.json`。源码修改不迁移、改写或重新解析已有财报原文。

## 既存测试与故障路径

- 邮件布局的 4 个旧断言在 HEAD 同样失败：旧表头/“未就绪”与现有报价质量语义不一致，禁止正文出现任何“基本面”也误伤合法说明。改为检查实际状态、来源时间、缺失值、已知基本面保留和 HTML 安全；没有修改产品去迁就旧文案。证据 `reports/head_layout.xml`、`legacy_mode_before.xml`、`quote_contract_recheck.xml`。
- config_store 旧测试使用错误的项目目录布局，并假设三份相同版本的监控池预览全部可以并发成功。修正目录、隔离证券解析，强制同版本竞争并验证一份成功、两份明确冲突；显式重新提交后各项保留，运行时凭证不落 YAML。产品并发语义未改。
- 部署门禁测试仍引用已经删除的优化器测试文件，导致故障注入从未命中。替换为现有回测恢复及缓存/行情测试，并检查节点文件存在；仍要求核心失败立即阻断后续步骤，没有降低产品部署门禁。
- 服务状态文件旧夹具通过 `__new__` 绕过构造，却未初始化既有 `trigger_runtime` 字段，造成两项原有测试失败。补齐夹具后，真实 Windows 短锁恢复、永久锁失败、JSON 策略及服务生命周期 49 项通过；没有修改运行时文件写入实现。
- 一个真实飞书行情用例缺少 e2e 标记，可能混入离线测试。补标记，并让意外程序异常直接失败，避免宽泛异常捕获将 BUG 伪装成数据源不可用。
- G03/G04 已确认存在于原提交并继续修复：单渠道构造失败阻断其他渠道；worker 将无效 trigger binding 错记为正常撤销。原复现 `reports/g03_g04_probe.json`、修前 `g03_g04_red.xml`（36 failed / 4 passed）保留。

## 继续修复 G03/G04

- G03 只修改原 NotifierManager：逐个启用渠道导入和构造，失败渠道保留 `failed/channel_initialization_failed`，其余渠道继续。原分发汇总接收这些失败回执，不把它们当成禁用；全部失败为 failed，部分成功为 partial。部署通知返回对应渠道 false，测试邮件明确区分初始化失败与禁用。没有重试、替代通知路径或新管理器。
- 末次短程链路核对确认：渠道失败经报告序列化、report_get 和机器人字段筛选后仍为 failed；failed/partial/unknown 汇总均保留，worker 均返回失败退出码。见 `catalog/final_receipt_consistency.json`。
- G04 只对齐 worker 与 parent 的既有分类：allowed 执行业务；revoked 为 skipped/trigger_revoked、exit 0；invalid 为 failed/trigger_binding_invalid、exit 1。检查之后状态变化也在真实隔离 TriggerStore/ExecutionStore 中验收。
- 新增用例覆盖普通与受管触发报告、三渠道分别导入/构造失败、真实邮件归档目录被文件占用、全部失败、配置/环境禁用、旧注入式测试对象和正常报告。修后首批 `g03_g04_green.xml` 为 40 passed；补充分支与完整原报告/通知/触发回归以最终记录为准。

## 全仓首轮原始结果

`full.xml`：5493 passed、12 failed、19 skipped、17 deselected，耗时 4440.35 秒。collection 发生在本轮修复前；首轮失败不能被后续结果覆盖删除。修复后按用例身份复验，最终汇总另列。

- Pi 成功路径的 5 秒测试预算在子进程启动期间耗尽，独立复验通过；只将该协议成功用例的测试预算设为 30 秒，保留真实超时保护测试及产品预算不变。
- DataHub 并发首次初始化的失败是 `ExecutionStore.__init__` 在 `PRAGMA journal_mode=WAL` 抛出 `database is locked`，该初始化语句在 HEAD 已存在，属于既有真实并发缺陷，不能归为慢测试。SQLite 锁升级不保证执行 busy handler；现在只在这条初始化语句遇到 SQLITE_BUSY 时，按原有 10 秒预算等待，恢复原 busy_timeout，不重放业务或下载。空库/既有 rollback 库的真实读锁复现修前 2 failed、修后通过；最终 10 项专测包括 8 线程首次空库/已有 WAL 各 3 轮、已有请求与队列状态保留、integrity_check、持续锁超时及 readonly 错误不重试。证据 `catalog/sqlite_init/red.xml`、`complete.xml`；邻近回归另列。
- `test_retired_source_stubs` 发现公告抓取器在本轮前的未提交内容加回了 6 个已退役空方法。先保存原字节及仅删除这 6 项的补丁，隔离验证 3 项通过；用户明确同意后按哈希核验应用。最终当前源码的公告路由、状态、分红分类、港美股及基金报告专项 93 项通过，0 外部调用尝试。其他既有改动保留。
- 漏标 e2e 的真实行情用例实际失败点是引用已删除的 `_build_report_sections` 私有方法。已改用公共简报入口，验证一张原生卡片和逐渠道回执；仍为 e2e，并保留手动发送开关。通过受控行情夹具与 mock 传输分别验发送开关分支，联合 Pi 及渠道回执 79 项通过、0 网络尝试。这不是对真实行情或发送的验收。

## 最终汇总

[完整机器回执及候选源码哈希](../../.tmp/offline_regression_20261109/acceptance.json) 保存各 XML 哈希、逐项原始失败与后续结果、19 项跳过原因和最终 60 个候选源码/测试哈希。统计为 **5611 passed / 19 skipped / 0 unresolved**；其中包含本地受控的补充测试，不是分支覆盖率。首轮真实行情用例仍保留失败记录，但因属于外部 e2e，最终从离线范围明确排除；新版 e2e 未真实运行。

全仓首轮在修复前 collection，并非最终源码一次全跑零失败。最终结果由首轮全仓加修改路径的定向复验组合，不能隐去这一边界。SQLite 最终集中回归 537 项时源码哈希前后一致；之后仅增加类型注解，反转该注解即精确还原该版源码，注解版再跑 10 项初始化专项通过（`catalog/sqlite_init/annotated.json`）。

## 最终版本专项结果

| 回归范围 | 原始回执 | 结果 |
|---|---|---|
| 资料、目录、DataHub、旧研究接口 | `catalog/final_tests.xml` | 316 passed / 2 skipped |
| 最终行号/页号兼容及 CNINFO | `catalog/final_coordinates.xml` | 83 passed |
| 全部 interactive、personal_monitor、report_execution、validation | `bot/final_integrated.xml` | 1771 passed / 7 skipped；13 个相关源码/测试哈希前后一致 |
| 最终 G03/G04、全部 notification/triggers、报告执行/所有权及渠道开关 | `reports/g03_g04_final.xml` | 582 passed，含 45 项新增故障分支；源码哈希前后一致 |
| 旧配置并发测试和部署门禁测试修正 | `legacy_corrected.xml` | 13 passed |
| SQLite 初始化及共享执行/DataHub/报告/触发/validation | `catalog/sqlite_init/final.xml` | 537 passed，0 外部调用尝试 |
| 最终类型注解版初始化专项 | `catalog/sqlite_init/annotated.xml` | 10 passed |
| 已批准的旧空数据源方法删除及公告链路 | `retired_source_review/applied.xml` | 93 passed |
| Pi 成功预算、飞书公共简报测试契约及渠道回执 | `bot/legacy_tests.xml` | 79 passed，真实行情未执行 |
| 服务状态文件与运行时生命周期 | `runtime_files_corrected.xml` | 49 passed |
| 旧邮件布局测试修正 | `reports/quote_contract_recheck.xml` | 13 passed |
| 分红事实用例隔离及邻近事实测试 | `reports/dividend_adapter_isolation.xml` | 61 passed，0 外部调用尝试 |

全仓首轮 guard 记录 11 次被阻止的外部 DNS/连接尝试：漏标真实行情用例 8 次、旧分红公告依赖 3 次。分别通过 e2e 隔离和公告 stub 修正；原记录 `network_attempts.jsonl` 保留，不声称首轮没有外部调用尝试。

这些集合有重叠，不能相加。机器人整组 collection 之后报告 worker/通知管理器又完成 G03/G04 修复，对应最终版本由报告专项覆盖。机器人与资料专项无外部调用尝试；报告专项曾拦截 3 次旧分红测试顺带抓公告的尝试，随后修正该测试的公告 stub 与外部调用断言，原拦截记录保留，不能写成最初也没有尝试。

被拦截的旧外部用例仍可能写入本地失败/请求策略记录；已观察到本地 `data/shared_platform/requests.sqlite3` 更新时间变化。本轮没有清空这些历史记录，不能宣称所有本地运行数据字节不变，也没有据此认定生产状态发生变化。

环境跳过共 19 项：普通符号链接缺少权限 15 项、Linux 专属的 stdin/容器权限/进程组语义 3 项、PyMuPDF 未安装 1 项。本轮另建真实 Windows junction，验证读取拒绝并保留原目标文件，清理测试联接；见 `junction_probe/receipt.json`。这不把全部符号链接测试改判通过。

## 静态检查与工作区边界

- 本轮新增/修正的业务与测试文件 Ruff 通过；`git diff --check` 通过。
- 已批准删除空方法的公告抓取器与 HEAD 源码一致；其 4 条 BLE001 宽泛异常告警也与 HEAD 完全相同，未为了清空告警扩大本轮改动。见 `retired_source_review/lint_baseline.json`。
- 本轮不提交、推送或部署；其他预存修改及未跟踪目录保留。最终测试回执与候选源码哈希写入 `.tmp/offline_regression_20261109/acceptance.json`。

## 未由离线验收证明的事项

- 外部搜索源返回实际搜索结果、真实模型选择与答案质量。
- 生产飞书/Telegram 入站、原事故触发事件、真实 SMTP 及完整报告全渠道送达。
- G05 的旧固定调度支持没有在本轮删除，也没有启用；不能把离线通过解释成所有历史调度入口已经退役。
- 环境跳过、首轮失败、修复后复验和既有缺陷必须分开记录，不宣称不存在任何 BUG。
