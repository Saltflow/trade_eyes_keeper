# 2026-10-30 本地优先合并范围

本轮已在`codex/local-preferred-merge`执行源码级合并，以`effee64`的所选业务实现为准，并从2026-10-30 21:10:41生产实际文件快照保留未选评价/运维实现。生产含未跟踪源码，因此按真实文件及修改块合并，没有用服务器Git HEAD冒充磁盘版本。未推送、部署、重启或改写生产配置。

用户输入“高ing”按“告警”处理。评价期限（9m→24m）及生产运行设置没有被明确点选，按“有争议保留”，保留两侧现状，不顺带整包替换。

## 已明确的模块来源

| 模块 | 采用内容 | 保留边界 |
|---|---|---|
| 搜索自动激活 | 采用本地：开关允许且相对晋升比较通过后自动激活 | 策略指针的自动切换；沿用生产既有promotion政策值，不扩大为实盘下单 |
| 日报/简报触发 | 采用本地配置化条件、事件资料准备及空条件/手动行为 | 时点、股票池、渠道、资源保留生产；评价证据期限另拆 |
| 告警 | 采用本地路径解析、调用者快照、失败显式返回及共用规则 | alerts.yaml两侧值已相同，不另换阈值 |
| 参考持仓 | 采用本地TradePlan路径及旧rebalance接口退役 | 账户、现金、持仓、版本/绑定及既有数据均保留，不重算回卷 |
| 财务数据 | 采用本地归母/合并利润口径、披露/时点及新财报数据实现 | DCF涉及最低评价期的差异单独保留；共用估值/行业组件不按目录删除 |
| 进程启动/发布身份 | 采用本地启动通知和部署ID唯一性修复 | 复用原systemd与公共宿主；不新增常驻进程或另一条同App长连接 |
| 对话/研究 | 采用本地Pi、巨潮图像/全文检索、财报提取→摘要→横评→三轮研究 | 连同桥接依赖、schema、Prompt及冻结依赖；不自动执行付费任务或扩预算 |
| 退役源码 | 35个有确证退役记录的源码路径及1个无消费者旧配置列删除 | 不删除数据、历史产物和原件；55个来源/旧链争议文件保留 |

## 253个原始差异的逐路径取舍

原差异为210个源码路径、43个配置路径。逐项清单由同一次实际快照产生，不能把90个线上独有文件等同于90个本轮删除。

| 类别 | 文件数 | 处理 |
|---|---:|---|
| 本地修改源码 | 41 | 选定本地版本，仍需与修改块/依赖一起装配 |
| 本地新增源码 | 39 | 新链路及直接依赖 |
| 本地研究合同/Prompt | 33 | 随选定研究链路纳入；历史版本不自动启用 |
| 确认退役删除 | 36 | 35源码+config/optimizer.yaml |
| 混合文件按修改块拆分 | 6 | 同文件中已选业务采用本地，争议接口/期限保留 |
| 评价期限争议保留 | 18 | 17源码+optimizer_constraints.yaml |
| 旧链来源/接口争议保留 | 55 | 不删除、不自动注册/接回新链路 |
| 运维/验收工具或模板保留 | 19 | 不默认运行、安装、上传或替换测试应用 |
| 其余运行/旧输入配置保留 | 6 | .env、主配置、主示例、等值promotion配置、两个旧手工输入 |

## 源码合并实施与本轮验证

- 5个混合业务文件保留本地自动激活、日报/简报条件、启动通知、研究工具及财务执行修复；期限helper、holdout统计/证据和回测入口恢复生产9m实现。生产22窗口、16排名、2隔离、4个重叠holdout，覆盖18个月的holdout区间；不等同切到24m方案。
- 55个争议旧文件保留。原809行reporting逐字隔离至`src/fundamental_quant/legacy_reporting.py`，`legacy_service.py`只保留原ResearchBlocked和evidence_hash纯辅助定义，没有复制3478行旧服务。9个旧消费者只改变导入路径；worker仍用本地blocked历史壳，4个当前历史壳字节未变。
- 41个本地修改、39个本地新增及33个研究配置的已选能力保持；19个未选运维/模板保留，其中本地独有工具仅保留文件、不注册/运行。35源码+1旧配置退役保持。
- 13个最小输入/冻结/tokenizer依赖在候选中逐hash核验。Pi复用本机相同lock版本依赖（earendil两包0.87.1、typebox1.3.27），Node24下18项桥接测试通过；未安装或升级生产依赖。

| 验证 | 本轮实际结果 | 本机证据 |
|---|---|---|
| 全量离线选择 | 4519项：首轮4476通过、3失败、40跳过；另3项按mark未选 | `.tmp/merge_evidence/full_offline.xml` |
| 原3失败最终复验 | 3通过；修正logs/cache运行目录假设及两处旧24m测试夹具，产品源码未变 | `.tmp/merge_evidence/original_failed_nodes_final.xml` |
| 最新唯一节点结果 | 4479通过、40跳过；不是另一次完整全量重跑 | `.tmp/merge_evidence/test_summary.json` |
| 项目validation | 36通过，已包含于全量 | `.tmp/merge_evidence/validation.xml` |
| 混合文件/期限专项 | 123通过、2个既有跳过；原失败记录保留，修正模块已复验 | `.tmp/mixed_horizon_merge/final_receipt.json` |
| 历史壳/旧导入兼容 | 69通过；禁网络、不构造旧LLM服务 | `.tmp/fq_legacy_compat/final.json` |
| 财报专项 | 首轮422通过、18跳过、1缺外部语料失败；显式只读原9份语料复验通过，全量也通过该用例 | `.tmp/merge_evidence/financial_gate.xml`、`context_corpus.xml` |
| 源范围独立核对 | 253项、13最小依赖；无额外误删/缺导入，支持根没有接到旧55 | `.tmp/merge_independent_review.json` |
| Ruff功能规则 | 18条既有unused诊断均在6个逐字保留生产脚本；其他文件零功能诊断 | `.tmp/merge_evidence/ruff_production_debt.json` |

没有把上述重叠gate相加充当独立用例数。40个跳过及真实通道/数据提供方/完整新环境安装仍保留验收边界。旧24m实现保留于`effee64`历史；此次没有加隐藏的9m/24m自动回退分支。候选`config/config.yaml`只是ignored、脱敏、禁外发测试profile，不是生产原件或部署载荷；`.env`没有复制。

## 旧基本面：保留哪些

`src/fundamental_quant`四个文件（__init__/archive/service/reporting）仍由CLI/Bot历史查询使用，保留历史只读和旧执行明确阻断。`src/fundamental_embedding`剩余17个文件中，13个由支持根经包初始化/策略动态发现可达，3个为CAPCO/CSRC/资本市场显式工具依赖，intrinsic_value为测试/诊断依赖且按争议保留。不能按旧目录名一删了之。

线上旧手工入口`scripts/run_longitudinal_research.py`形成35文件闭包，未发现从现行main/runtime/Bot/新财报/研究沙箱支持根进入它的证据。另23个线上旧包文件也没有支持根可达证据，但没有重新检查线上OS注册与人工使用，按用户保守规则暂不删。这35个闭包路径与55个线上独有争议路径有重叠，不能相加计数。

### 两处不能整文件覆盖的冲突

1. **评价期限**：main、邮件、研究CLI和DCF等调用本地中心期限合同。实际只读加载线上约束文件得到`ValueError: evaluation_horizon_policy is required`。因此“采用本地全部源码但保持线上9m YAML”不是已验证可运行的组合。执行时已精确恢复生产期限节点，并保留本地业务块；不切24m，也未编写隐藏兼容回退。

2. **旧reporting接口**：旧手工孤岛有6处依赖旧`reporting`辅助接口，直接换成本地49行历史格式化壳会破坏其导入。执行时历史只读输出采用本地，原reporting以legacy_reporting隔离并只重定向旧消费者；必要service纯助手另隔离。没有只恢复一个旧CLI、加入新旧双调度或重新启用旧付费流水线。

## 新研究链路的最小文件闭包

除了原253项，还需核对下列被执行器当作模型输入、冻结依据或token计数依据的文件。它们不是可随意漏掉的测试/文档；当前生产是否存在与哈希未包含于此前运行源码清单，正式应用前必须逐项预检，不能假装已核验。

| 路径 | 实际用途 |
|---|---|
| `docs/design/company_summary_example.md` | company_summary_contract.prompts.example及摘要执行器冻结清单 |
| `docs/design/company_summary_contract.md` | run_company_summary.py的canonical冻结清单 |
| `docs/design/research_report_example_v6.md` | research_report.json.prompts的实际模型输入 |
| `docs/development/peer_research_paid_20261024.md` | run_peer_research.py执行计划的冻结清单；只读历史说明不重复付费 |
| `tests/test_company_summary_execution.py` | 摘要执行器canonical冻结清单 |
| `tests/test_peer_research_execution.py` | 横评执行器冻结清单 |
| `data/fundamental_quant/tokenizer/tokenizer.json` | 提取、摘要、横评、正式研究的实际token计数依赖 |
| `data/fundamental_quant/tokenizer/tokenizer.provenance.json` | 已版本化tokenizer来源记录 |
| `tests/fixtures/fundamental_data_v2_contract/blocks_2024.json` | prepare_company_summary.py冻结输入目录，保留完整数据合同夹具 |
| `tests/fixtures/fundamental_data_v2_contract/blocks_2025.json` | prepare_company_summary.py冻结输入目录，保留完整数据合同夹具 |
| `tests/fixtures/fundamental_data_v2_contract/examples.json` | prepare_company_summary.py冻结输入目录，保留完整数据合同夹具 |
| `tests/fixtures/fundamental_data_v2_contract/invalid_cases.json` | prepare_company_summary.py冻结输入目录，保留完整数据合同夹具 |
| `tests/fixtures/fundamental_data_v2_contract/README.md` | prepare_company_summary.py冻结输入目录，保留完整数据合同夹具 |

## 验证状态与后续执行边界

- [x] 原253个差异均有唯一决策，绑定本地/生产文件SHA与原diff。
- [x] 本地4+17旧组件、策略动态发现、研究worker与显式工具依赖已做静态闭包核验。
- [x] 保留旧手工孤岛并列出reporting接口冲突，未把封闭旧簇的__main__当作当前活根。
- [x] 本地期限加载器与生产原配置的不兼容已实际只读复现。
- [x] 6个混合文件已完成修改块合并；保留生产9m实现与本地所选业务。旧reporting冲突以显式legacy模块隔离，不恢复旧服务。
- [x] 23:34完成新增冻结依赖生产存在性、相关OS旧注册与实时源SHA只读预检：13个输入中12个线上缺失、tokenizer一致；未见相关旧研究脚本注册或运行，人工旧链仍按争议保留。详见[差异收尾](source_parity_cleanup_20261030.md)，不表示已完成发布。
- [ ] 合并后生产部署与真实链路验收；本轮未执行。

范围盘点阶段未改产品；用户随后授权执行源码合并，现已完成实现与本轮离线回归。生产运行配置、策略账户和业务数据未改。没有重跑真实生产优化、报告发送或付费模型。详细静态证据和完整逐文件决策位于本机`.tmp/production_parity_20261030/merge_scope/`；原始完整diff在相邻`all_diffs/`。

## 采用本地的修改源码（41）

| 路径 | 处理依据 |
|---|---|
| `tools/feishu-pi/bridge.mjs` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `tools/feishu-pi/bridge.test.mjs` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_embedding/__init__.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_embedding/api.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `pyproject.toml` | 已选对话/Pi及财务PDF功能所需依赖声明以本地为准；这只界定源码清单，不授权当前安装或变更生产环境。 |
| `requirements.txt` | 已选对话/Pi及财务PDF功能所需依赖声明以本地为准；这只界定源码清单，不授权当前安装或变更生产环境。 |
| `src/markets.py` | 当前支持根、src/scripts/main/tests没有_get_lot_size/_get_month_key/_eval_lookback_days或旧常量的有效调用；此差异只删除这些旧帮助项，不引入24m策略。闭合旧岛未知调用仍保留其原件，正式切换前复核旧接口消费者。 |
| `config/__init__.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/interactive/assistant/client.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/interactive/assistant/harness.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/interactive/assistant/knowledge.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/interactive/assistant/service.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/interactive/command_dispatcher.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/interactive/command_parser.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/interactive/telegram_bot.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/data/backtest_data.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/data/dataset_catalog.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/data/dataset_documents.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/data/hk_corporate_actions.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/data/llm/base.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/data/market_history.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/data/point_in_time_backfill.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/data/reference_universe_backfill.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/data/technical_indicators.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/notification/brief_market.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/notification/chart_generator.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/notification/feishu_notifier.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/notification/report_builders.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/notification/telegram_notifier.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_quant/__init__.py` | 采用本地历史只读兼容实现/新增archive，阻止旧执行链恢复；线上额外旧模块不随本项自动删除，仍单列待调用链核验。 |
| `src/fundamental_quant/service.py` | 采用本地历史只读兼容实现/新增archive，阻止旧执行链恢复；线上额外旧模块不随本项自动删除，仍单列待调用链核验。 |
| `src/instruments/models.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/instruments/point_in_time.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/core/build_info.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/core/condition_checker.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/core/data_fetcher.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/core/ref_portfolio.py` | Root逐行及导入核验：该文件仅使用稳定哈希合同，没有评价期限加载或新24m字段依赖；旧rebalance已在用户选中范围，可采用本地。入口main的评价期限修改另拆。 |
| `src/core/schedule_manager.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/search/validation.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/backfill_reference_universe.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/run_reference_universe_isolated.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |

## 采用本地的新增源码（39）

| 路径 | 处理依据 |
|---|---|
| `tools/feishu-pi/framing.mjs` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `tools/feishu-pi/shared-runtime.mjs` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/data/source_screening.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/__init__.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/api_client.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/api_format.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/api_io.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/api_pipeline.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/disclosure/__init__.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/disclosure/absence.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/disclosure/contract.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/disclosure/gaps.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/disclosure/layout.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/disclosure/note_totals.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/handoff.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/history.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/metadata.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/model_context.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/v2_history.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/v2_schema.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/v2_transport.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_data/v2_view.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `src/fundamental_quant/archive.py` | 采用本地历史只读兼容实现/新增archive，阻止旧执行链恢复；线上额外旧模块不随本项自动删除，仍单列待调用链核验。 |
| `src/instruments/statement_scope.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/backfill_ashare_sources.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/build_financial_context.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/build_fundamental_data.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/build_fundamental_data_v2.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/financial_pipeline.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/ocr_financial_page.ps1` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/prepare_company_summary.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/refresh_csi_reference_manifest.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/refresh_monitoring_h1_fundamentals.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/research_inputs.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/research_render.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/research_report.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/research_rounds.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/run_company_summary.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |
| `scripts/run_peer_research.py` | 属于已选本地为准的搜索自动激活、告警、财务数据、参考组合、进程身份或对话/Pi链路及其直接源码/依赖清单；此分类不授权执行脚本、安装服务或部署。 |

## 采用本地的研究合同与Prompt（33）

| 路径 | 处理依据 |
|---|---|
| `config/company_summary_contract.json` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/fundamental_data_api.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/fundamental_data_v2.json` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/company_summary.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/company_summary_recovery.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/peer_research.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_audit_v2.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_audit_v3.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_audit_v4.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_audit_v5.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_audit_v6.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_basis_v2.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_basis_v3.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_basis_v4.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_common.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_conclusion.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_draft_v2.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_draft_v3.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_draft_v4.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_draft_v5.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_draft_v6.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_final_v2.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_final_v3.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_final_v4.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_final_v5.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_final_v6.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_review.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_valuation_v4.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_valuation_v5.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_valuation_v6.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_visuals.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/prompts/research_write.md` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |
| `config/research_report.json` | 用户已选择对话/研究及财务数据以本地为准；该配置/协议/Prompt随本地链路纳入候选。历史Prompt保留便于冻结复现，不自动设为默认，不运行模型或增加预算。 |

## 明确退役的源码删除（35）

| 路径 | 处理依据 |
|---|---|
| `src/fundamental_embedding/causal_context_gate.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/causal_rank_context_gate.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/industry_blend_evaluation.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/industry_factor_evaluation.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/intrinsic_evaluation.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/latent_peer_moe.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/moe.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/pricing.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/reverse_dcf_consensus.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/split_evaluator.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/valuation_features.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `src/fundamental_embedding/valuation_industry_evaluation.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/build_current_valuation_feature_snapshot.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/build_historical_valuation_consensus_features.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/build_historical_valuation_features.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/build_reverse_dcf_consensus_report.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/diagnose_market_cost_expert_coverage.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/diagnose_valuation_expert_coverage.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_causal_context_gate.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_causal_rank_context_gate.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_industry_rank_blends.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_industry_relative_factors.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_industry_relative_fundamentals.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_intrinsic_value.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_reverse_dcf_batch.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_valuation_causal_rank_gate.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_valuation_consensus_context.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_valuation_consensus_quality.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_valuation_context_increment.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_valuation_feature_ablation.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_valuation_industry_increment.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/evaluate_valuation_rank_blends.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/train_fundamental_embedding.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/train_latent_peer_moe.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |
| `scripts/train_split_fundamental_pricing.py` | 此路径确由756cbba退役提交删除，用户明确退役源码以本地为准；Root最终调用链核验无活动消费者后可纳入删除清单。 |

## 无消费者旧配置删除（1）

| 路径 | 处理依据 |
|---|---|
| `config/optimizer.yaml` | Root检查src/main/scripts及当前指南无读取路径；权威配置为optimizer_constraints.yaml和主配置optimizer.markets。该旧Builder/Bayesian文件已在本地退役，按用户退役以本地为准列删除；不删除历史产物。 |

## 需按修改块合并的混合文件（6）

| 路径 | 处理依据 |
|---|---|
| `main.py` | main同时承载用户已选行为与未授权评价期限，不能整文件本地覆盖。 |
| `src/interactive/commands/handlers.py` | 对话以本地为准不等于授权评价期限升级；回测下限须拆出。 |
| `src/templates/appendix_methodology.md` | 附录应随最终实际合同，不先用本地研究期限叙述替换线上。 |
| `src/notification/email_notifier.py` | 已选日报/财务改动和未授权24m证据合同混杂；先保留生产期限行为，按hunk拆。 |
| `src/fundamental_quant/reporting.py` | 本地Bot历史元数据输出为准；线上旧手工孤岛有6处依赖旧reporting接口，不能整文件覆盖后破坏保留链。旧接口及传递依赖先保留原件/原线上实现，归入争议组；没有擅自加入新兼容层或恢复旧执行入口。 |
| `src/search/workflow.py` | 搜索自动激活获选，但新搜索评价期限合同未获选；工作流含财务执行修复，需分拆验证依赖。 |

## 评价期限保留（18）

| 路径 | 处理依据 |
|---|---|
| `src/fundamental_embedding/dcf_entry_calibration.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `src/backtest/engine.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `src/experiments/strategy_benchmark.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `src/experiments/universe_robustness.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `src/search/artifacts.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `src/search/config.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `src/search/reporting.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `scripts/analyze_collar_robustness.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `scripts/backtest_510300_collar.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `scripts/backtest_capm_dcf_value.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `scripts/backtest_collar_nav_sizing.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `scripts/backtest_justified_pb_value.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `scripts/benchmark_technical_strategies.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `scripts/justified_pb_ablation.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `scripts/screen_csi300_500_justified_pb.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `scripts/screen_csi300_500_justified_pb_v3.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `scripts/screen_justified_pb_v4.py` | 实际线上→本地差异以中心24m期限、相应窗口/输出口径或九个月历史入口隔离为主；用户未选择评价期限行，本轮不自动采用。 |
| `config/optimizer_constraints.yaml` | 评价期限未被用户明确点选。本地24m、生产9m两侧原件保留；本次不整文件覆盖。纯加载核验已证实本地policy loader不能直接读取原生产配置，不能伪造混合版本已可运行。 |

## 旧链/来源争议保留（55）

| 路径 | 处理依据 |
|---|---|
| `src/fundamental_quant/acceptance_campaign.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/auditing.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/bootstrap.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/calculations.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/cashflow_bridge.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/comparison_transport.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/contracts.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/cover_workflow.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/currency_definitions.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/email_report.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/evaluation.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/extraction_audit.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/extraction_repair.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/fr_image_evidence.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/fr_pack.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/fr_pack_contract.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/fr_pack_gate.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/fr_pack_render.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/llm.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/longitudinal_acquisition.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/longitudinal_costs.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/longitudinal_data.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/longitudinal_discovery.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/longitudinal_export.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/longitudinal_release.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/longitudinal_report.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/longitudinal_tools.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/longitudinal_workflow.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/notifications.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/page_evidence.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/pdf_sessions.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/peer_panel.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/peer_panel_render.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/prompts.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/request_layout.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/research_calculations.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/research_contracts.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/research_prompts.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/research_release.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/research_workflow.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/retrieval.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/session_baseline.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/session_context.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/session_notebook.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/session_prompts.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/session_review.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/session_sources.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/session_store.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/session_workflow.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/share_count_sources.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/sources.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/statement_restatement.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/store.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `src/fundamental_quant/worker.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |
| `scripts/run_longitudinal_research.py` | 线上独有但9b与eff两端均未被Git跟踪，并非本轮已提交删除；按旧基本面“有链路保留、有争议保留”先保留，由Root核验无链路后另列删除。 |

## 运维/验收配套保留（19）

| 路径 | 处理依据 |
|---|---|
| `scripts/accept_personal_monitor.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/audit_shared_runtime.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/benchmark_shared_runtime.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/check_full_comparison.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/cleanup.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/deploy_feishu_assistant.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/pack_shared_runtime_benchmark.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/personal_monitor_process.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/personal_monitor_shared_test.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/personal_monitor_transport.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/push_dataset_snapshot.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/push_dataset_snapshot_sftp.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/render_pi_campaign_costs.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/run_personal_monitor_feishu_group_test.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/run_personal_monitor_feishu_test.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/sync_server_market_data.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `scripts/validate_reference_universe_backfill.py` | 此运维/测试/基准或部署辅助工具未获明确覆盖选择；保留当前状态，不新增OS注册、不执行部署、不启测试进程，需调用闭包证据后再纳入。 |
| `config/personal_monitor_feishu_group_test.yaml.example` | 本轮未选择更换独立验收App/会话或本机注册；模板与运维文件原样保留，不写生产凭据、开关或群。 |
| `config/personal_monitor_feishu_test.yaml.example` | 本轮未选择更换独立验收App/会话或本机注册；模板与运维文件原样保留，不写生产凭据、开关或群。 |

## 各环境凭据保留（1）

| 路径 | 处理依据 |
|---|---|
| `config/.env` | 不将Windows SSH路径、SEC User-Agent或本机环境覆盖到Linux生产；原件已安全备份。 |

## 生产运行配置保留（1）

| 路径 | 处理依据 |
|---|---|
| `config/config.yaml` | 监控池/skip_search、Telegram、飞书身份、健康群、助手资源及调度时点均保留生产。允许所选报表条件的独立键与历史只读路径进入后续最小合并，当前快照未发现这些所选业务键的不同值，不生成无根据的全文件覆盖。 |

## 混合示例配置保留（1）

| 路径 | 处理依据 |
|---|---|
| `config/config.yaml.example` | 示例同时涉及本轮未选择的资源、时点、开关和期限；不把示例值应用到生产，仅由后续文档补丁带入所选业务字段。 |

## 配置值等价无需改写（1）

| 路径 | 处理依据 |
|---|---|
| `config/promotion_policy.yaml` | 两侧规范化配置值相同，仅字节/格式不同；采用本地自动激活判断逻辑时沿用生产已有政策值，不据此更换晋升阈值。 |

## 旧手工输入配置保留（2）

| 路径 | 处理依据 |
|---|---|
| `config/fr_pack_gate.json` | 仍可能是线上旧手工孤岛的输入；未纳入新支持根也未证明可丢弃，按争议保留，不改值、不启用旧任务。 |
| `config/longitudinal_research.json` | 仍可能是线上旧手工孤岛的输入；未纳入新支持根也未证明可丢弃，按争议保留，不改值、不启用旧任务。 |
