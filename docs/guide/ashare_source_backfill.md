# 筛选后七年底层资料后台补齐

2026-10-24按用户提供的《沪深港五项筛选_20260927.xlsx》缩小深采范围。旧全A股批次 `data/fundamental_quant/source_backfill/themes_depth7_20261024` 保留STOP，不再恢复。第一、二阶段、摘要、研究流程及旧产物不修改，不调用付费模型。

2026-10-25用户明确重申“有多少看多少”。现行批次为 `data/fundamental_quant/source_backfill/screened_best_effort_20261025`，只读复用已停止的 `screened_depth7_20261024`；旧plan、进度、原始文件不覆盖。补字段结果为335通过、5,146未通过、91仍待核，不重新请求已有筛选响应。

同日用户进一步授权公司间2–3路并行。现行批次改为 `data/fundamental_quant/source_backfill/screened_parallel3_20261025`；旧best_effort批次已通过STOP正常退出，保留全部断点。新计划冻结company_workers=3，复用旧批次及真实小批的文件，不重采已有完整或partial结果。

## 初筛与待核

当前本地A股名单5,572只（2026-10-05快照）完整分为：335只通过、4,879只未通过、358只待核。通过池含42银行、5保险及保险控股、30能源、258其他；待核包括原表未覆盖的356只和中国石油、宏桥控股。原表A股行情为2026-09-24快照，不是实时全市场重筛。

五项沿用原表严格不等号：市值>10亿元、0<TTM PE<25、0<扣非TTM PE<25、总负债/总资产<70%、历史正常化盈利收益代理>7%。用户明确仅银行和保险豁免第四项，其余照用。

第五项按原表行业预设路径：PB用FY2021–FY2025扣非加权ROE中位数（至少3个有效年度）/正PB；PE用FY2023–FY2025扣非归母利润中位数（3年齐全）/报价对应普通股权价值。TTM固定FY2025+2026H1−2025H1。不是当前PE倒数，也不是未来收益保证，不按结果挑选更有利路径。

`src/data/source_screening.py`先对待核补字段：每只最多一次免费F10指标请求、一次腾讯A股报价请求，5秒请求间隔，原始响应先保存再解析。价格及总普通股本取同一报价响应；BPS/负债率取2026H1；所有日期明示。缺扣非ROE不用未扣非ROE替代。原表未覆盖者不从项目另一套valuation_path猜取，重组等待核原因保留。其他条件明确失败可判未通过；缺路径或字段且无明确失败则仍待核，不深采七年PDF。

补字段结果在 `screening_fields/<code>/`，解析汇总为 `screening_resolved.json`，原始筛选与plan不变。普通失败仅记录一次、保留待核；401/403/429停止本批等待介入，不反复刷请求。通过的才进入深采队列，未通过的不下载。

## 下载与重试

最多3家公司同时推进，每家仍先尝试其全部年度及接口。各公司使用独立的巨潮HTTP session；查询与PDF请求起点共享SQLite原子限速，每次至少隔5秒。PDF下载总并发仍为3，而非3家公司各3份合计9份：公司下载批次通过一个协调锁调用既有归档器，避免多个run把其他在途任务误标为中断；保留相同URL锁和跨进程总锁。某家公司partial不影响其他在途公司或后续派发。

公司线程只向队列提交进展、写自己的公司目录；主线程统一写status/events并更新计数。active_companies列出所有在途公司，不再用current一个字段代表整个运行。每次只派发最多company_workers个任务，STOP或全局故障后不再派发，等待在途任务退出，避免已退出公司的晚到进度覆盖最终状态。

本批显式取消PDF单文件大小门槛，流式落盘，磁盘剩余低于10GiB停止。每个归档任务600秒，公司下载批次2,100秒。文本解析仍沿用8MiB/1,000页及解析器60秒，与原件状态分开；其他旧调用者未显式配置时保留原有大小策略。

移除大小上限仅重开旧 `document_exceeds_size_limit/cached_document_exceeds_size_limit` 任务，不重置源站封禁。404、报告未找到、非PDF等局部失败写入年度缺口，继续其余年度与接口；网络错误保留下载器持久退避和Retry-After，不为同一缺口反复请求或填造数据。HTTP401/403/429、Baostock封禁/访问预算、磁盘不足或程序异常才停止整个任务并通知，不能通过跳公司绕过源站控制。

Baostock保持已有单连接、跨进程锁、2秒间隔、40,000每日协议请求预算和持久封禁。日线请求2019-01-01至2026-10-23；上市前不适用必须有官方上市日期。沿用7自然日端点比对作缺口标注，超出也不阻塞队列，且端点比对不能证明中间无缺口。

公司有7个年度状态、适用PDF及哈希、成功行情/财务文件及哈希且无已知来源缺口时记complete；有缺口时记partial，gaps保存缺年、缺接口和实际行情起止。已经保存的成功接口可直接复用，不为已知起点缺口重复抓相同缓存。重启时核验partial中可用文件的哈希，保持已处理状态，后续补盲另行决定。

汇总分开processed（已尝试）、completed（无已知来源缺口）和partial（有缺口）。themes_processed表示优先主题已跑完，themes_complete仍只表示无来源缺口；最终状态可为complete_with_gaps。报告必须展示缺口，不能把“已尝试”说成“完整补齐”。原件下载、文本解析、字段完整、模型提取仍是四种不同状态；免费接口成功不代表完整三表或研究ready。

## 运行

```powershell
python -B -X utf8 scripts/backfill_ashare_sources.py prepare --output data/fundamental_quant/source_backfill/screened_parallel3_20261025 --screening data/fundamental_quant/source_backfill/screened_best_effort_20261025/screening_resolved.json --reuse-run data/fundamental_quant/source_backfill/screened_best_effort_20261025 --company-workers 3
python -B -X utf8 scripts/backfill_ashare_sources.py run --output data/fundamental_quant/source_backfill/screened_parallel3_20261025
python -B -X utf8 scripts/backfill_ashare_sources.py status --output data/fundamental_quant/source_backfill/screened_parallel3_20261025
```

prepare必须显式给筛选JSON；禁止默认全市场盲采。`--company`仅接受通过代码供小批验收，不跑全市场补字段。`--company-workers 1/2/3`由prepare写入冻结计划，run读取计划。新批次只读复用旧进度并核验真实文件和哈希，不写回旧批次；小批验证的新成果也可原字节复制到新批次公司目录，并记录来源/哈希，后续运行直接核验复用。生产仍仅一个后台进程，进程内至多3个公司任务；Baostock的整个SDK会话受既有跨线程及跨进程锁保护，实际连接始终串行。

plan.json/plan.sha256冻结名单、配置、筛选输入及源码；launch.json记录PID/启动时间/命令。status.json/events.jsonl与companies/<code>/progress.json、interfaces.json、downloads.json记录进度。计划可含待核候选，但运行先补字段、再选已通过者深采。

创建本批STOP可停止；不删数据。恢复前核验PID/启动时间/命令、冻结哈希及源站状态；用户主动停止、明确全局blocked或封禁均不自动恢复。heartbeat正常进度和局部partial保持安静，只报需要介入或主题已处理完。主题通知仅指筛选通过池，必须区分完整和部分、列出缺口及91只待核，不称原241只全行业完成；通知前逐公司核验可用文件/哈希/上市证据/行情起止，其余通过池继续。不向其他聊天或邮箱发消息。
