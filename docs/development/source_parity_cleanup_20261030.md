# 在线与离线差异收尾（2026-10-30）

生产只读核查时间：2026-10-30T23:34:51+08:00。本地基线`2deb5f2`加本轮两个运行/发布辅助修复；没有推送、部署、重启、修改生产配置或发测试消息。此前源码合并见[逐路径决策](local_preferred_merge_scope_20261030.md)。

## 还剩哪些差异

| 实际文件比较 | 数量 | 含义及处理 |
|---|---:|---|
| 同路径源码/公开配置不同 | 58 | 41个已选本地源码、6个混合文件、9个旧接口导入隔离、ci_cd_deploy.py，以及本轮专用部署脚本修复。主要是待发布的本地实现，不是58个未解决冲突 |
| 本地独有源码/公开配置/工具 | 91 | 39个新业务源码、33个合同/Prompt、17个验收运维/模板、2个旧接口隔离助手；验收工具不默认注册或启动 |
| 线上仍有、已确认本地退役 | 36 | 35源码及config/optimizer.yaml；本地保持删除，线上清除留待正式部署，不在本轮直接删生产文件 |
| 环境私有文件 | 2 | config/.env与config/config.yaml不属于源码发布载荷；各环境保留，不用本机测试配置覆盖生产 |
| 新研究冻结依赖缺失 | 12 | 另核查最小13个输入/冻结依赖；12个线上缺失，tokenizer.json已存在且哈希一致。12个本地均已Git跟踪，正式发布须携带 |

原始审计视角为58修改、38线上独有（36退役+2环境私有）、103本地独有（91+12冻结依赖）、262相同。范围包含BAT和新增13个冻结输入；不能与此前253项用不同口径直接相减，也不是全磁盘/所有文档/所有测试比较。

评价期限18项保持生产9m版本，当前比较一致。55个旧争议文件继续保留：其中46个与线上相同，9个只因旧reporting/service辅助导入隔离而不同。没有以清理名义恢复旧执行，也没有重新尝试已否定的模型。

## 本轮实修的两个发布盲区

1. `scripts/deploy_feishu_assistant.py`此前只允许bridge.mjs，而当前入口会依赖shared-runtime.mjs和framing.mjs。现在三者均在明确白名单内，check与apply都要求三个文件显式成组提供并绑定各自SHA；缺一个、旧manifest漏一个、预检后删除或修改任一个，均在远程访问前拒绝。没有自动扩大用户提交的文件范围，也没有执行部署。
2. `src/core/build_info.py`此前漏算src以外的实际运行依赖。现在额外纳入config/__init__.py、requirements.txt、Pi运行三文件和package.json/package-lock.json。公开文件存在时计入集合，增加、删除、修改都会改变构建身份；不读取.env、主运行配置、账户或数据库。校验仍按进程缓存一次，额外读取7个文件共81,912字节，不新增服务或周期扫描。未进行性能基准，不据此宣称性能验收。

## 生产当前状态与未处理项

- systemd仍为active/running，PID856191，NRestarts=0；本次采样心跳约8.5秒，飞书线程存活、transport_ready=true。这是健康状态读回，不是重新完成真实消息收发验收。
- Git HEAD仍为8bd9484；登记发布仍为v1.18.0 / 6a55894。心跳已明确显示source_manifest_mismatch：公告补丁改了announcement_fetcher.py并新增hk_announcements.py、us_announcements.py，但原登记清单未更新。这三个公告文件当前本地/生产哈希相同，不能把登记不符误当源码回退。正式部署应按完整候选重新登记与重启后核验；本轮不伪造发布身份。
- 检查相关systemd unit、当前用户crontab和可读/proc命令行未发现旧研究脚本注册或运行。相关unit列表仍有trade-eyes-health.service名称，不能仅凭名称断言它在执行；本轮没有删除或启停它。人工历史使用无法由这些检查排除，因此遵守“有争议保留”。
- 保留生产9m评价合同、环境配置和历史/争议链；没有为消掉diff而覆盖这些内容。6个逐字保留的生产脚本仍有18条既有unused Ruff诊断，指纹与上轮完全一致，本轮未修改这些脚本的业务计算。

## 本轮验证

- [x] 同一次稳定读中对实际线上文件逐hash核对，未用Git HEAD代替磁盘版本；所有剩余差异都有原决策或新增原因。
- [x] 首轮119通过、2个系统符号链接权限跳过；随后扩大相关回归315项：313通过、2跳过、0失败/错误。包括完整tests/validation/、发布辅助、版本展示、runtime_files、配置部署和隔离回填入口。两个集合重叠，不相加。
- [x] Pi六种不完整组合在check/apply均拒绝；完整三文件内容发送、预检后删除/修改、现有配置保护与回滚测试通过。均为离线mock远程执行，不是实部署。
- [x] 7个公开依赖的增删改、凭据/主配置不读取、并发读只算一次、原版本展示测试通过。两个真实符号链接测试因Windows权限跳过，未伪称通过。
- [x] 修改的4个Python文件Ruff通过；全src/scripts/main/config功能规则仍仅18条原有诊断，无新增。
- [ ] 生产发布、更新登记清单与重启后的真实收发验收：未执行，不能将本机313项结果冒充生产验收。

有效回归命令：

```powershell
$env:SKIP_EMAIL='true'
$env:SKIP_NOTIFICATIONS='true'
python -X utf8 -m pytest -o addopts= tests/test_deploy_feishu_assistant.py tests/test_build_info.py tests/notification/ tests/test_runtime_files.py tests/test_reference_universe_isolated_script.py tests/test_ci_cd_deploy_runtime_service.py tests/test_ci_cd_deploy_prechecks.py tests/test_deploy_optimizer_config.py tests/validation/ --basetemp=.tmp/parity_cleanup/regression_final -q --junitxml=.tmp/parity_cleanup/regression_final.xml
```

本机证据：`.tmp/parity_cleanup/live.json`、`remaining.json`、`acceptance.json`、`regression_final.xml`、`ruff_functional.json`。初次扩大命令因写错一个不存在的测试文件而0项收集，原regression.xml保留，不能计为验证；上述有效命令完整通过。产品文件实际SHA与源码基线在acceptance.json中记录，提交号另记录于commit_receipt.json。

## 13个必要冻结输入

| 路径 | 线上实际状态 |
|---|---|
| `docs/design/company_summary_contract.md` | 缺失 |
| `docs/design/company_summary_example.md` | 缺失 |
| `docs/design/research_report_example_v6.md` | 缺失 |
| `docs/development/peer_research_paid_20261024.md` | 缺失 |
| `tests/test_company_summary_execution.py` | 缺失 |
| `tests/test_peer_research_execution.py` | 缺失 |
| `data/fundamental_quant/tokenizer/tokenizer.json` | 哈希一致 |
| `data/fundamental_quant/tokenizer/tokenizer.provenance.json` | 缺失 |
| `tests/fixtures/fundamental_data_v2_contract/blocks_2024.json` | 缺失 |
| `tests/fixtures/fundamental_data_v2_contract/blocks_2025.json` | 缺失 |
| `tests/fixtures/fundamental_data_v2_contract/examples.json` | 缺失 |
| `tests/fixtures/fundamental_data_v2_contract/invalid_cases.json` | 缺失 |
| `tests/fixtures/fundamental_data_v2_contract/README.md` | 缺失 |

## 同路径修改（58）

| 路径 | 处理 |
|---|---|
| `ci_cd_deploy.py` | 已选本地进程/发布逻辑：支持显式HEAD发布；清除旧健康服务而不重新mask |
| `config/__init__.py` | 已明确采用本地源码 |
| `main.py` | 已选业务但混合未授权范围，按hunk拆分 |
| `pyproject.toml` | 已明确采用本地源码 |
| `requirements.txt` | 已明确采用本地源码 |
| `scripts/backfill_reference_universe.py` | 已明确采用本地源码 |
| `scripts/deploy_feishu_assistant.py` | 本轮修复：Pi三文件显式成组预检/应用，拒绝旧的不完整manifest |
| `scripts/run_reference_universe_isolated.py` | 已明确采用本地源码 |
| `src/core/build_info.py` | 保留本地发布ID修复，并补7个公开运行依赖的发布哈希 |
| `src/core/condition_checker.py` | 已明确采用本地源码 |
| `src/core/data_fetcher.py` | 已明确采用本地源码 |
| `src/core/ref_portfolio.py` | 采用本地参考持仓实现 |
| `src/core/schedule_manager.py` | 已明确采用本地源码 |
| `src/data/backtest_data.py` | 已明确采用本地源码 |
| `src/data/dataset_catalog.py` | 已明确采用本地源码 |
| `src/data/dataset_documents.py` | 已明确采用本地源码 |
| `src/data/hk_corporate_actions.py` | 已明确采用本地源码 |
| `src/data/llm/base.py` | 已明确采用本地源码 |
| `src/data/market_history.py` | 已明确采用本地源码 |
| `src/data/point_in_time_backfill.py` | 已明确采用本地源码 |
| `src/data/reference_universe_backfill.py` | 已明确采用本地源码 |
| `src/data/technical_indicators.py` | 已明确采用本地源码 |
| `src/fundamental_embedding/__init__.py` | 已明确采用本地源码 |
| `src/fundamental_embedding/api.py` | 已明确采用本地源码 |
| `src/fundamental_quant/__init__.py` | 已明确采用本地源码 |
| `src/fundamental_quant/email_report.py` | 来源/调用链有争议，先保留线上 |
| `src/fundamental_quant/longitudinal_release.py` | 来源/调用链有争议，先保留线上 |
| `src/fundamental_quant/longitudinal_report.py` | 来源/调用链有争议，先保留线上 |
| `src/fundamental_quant/reporting.py` | 历史只读输出采用本地，旧手工链接口冲突保留 |
| `src/fundamental_quant/research_release.py` | 来源/调用链有争议，先保留线上 |
| `src/fundamental_quant/research_workflow.py` | 来源/调用链有争议，先保留线上 |
| `src/fundamental_quant/service.py` | 已明确采用本地源码 |
| `src/fundamental_quant/session_notebook.py` | 来源/调用链有争议，先保留线上 |
| `src/fundamental_quant/session_review.py` | 来源/调用链有争议，先保留线上 |
| `src/fundamental_quant/session_store.py` | 来源/调用链有争议，先保留线上 |
| `src/fundamental_quant/session_workflow.py` | 来源/调用链有争议，先保留线上 |
| `src/instruments/models.py` | 已明确采用本地源码 |
| `src/instruments/point_in_time.py` | 已明确采用本地源码 |
| `src/interactive/assistant/client.py` | 已明确采用本地源码 |
| `src/interactive/assistant/harness.py` | 已明确采用本地源码 |
| `src/interactive/assistant/knowledge.py` | 已明确采用本地源码 |
| `src/interactive/assistant/service.py` | 已明确采用本地源码 |
| `src/interactive/command_dispatcher.py` | 已明确采用本地源码 |
| `src/interactive/command_parser.py` | 已明确采用本地源码 |
| `src/interactive/commands/handlers.py` | 已选业务但混合未授权范围，按hunk拆分 |
| `src/interactive/telegram_bot.py` | 已明确采用本地源码 |
| `src/markets.py` | 采用本地死辅助接口清理 |
| `src/notification/brief_market.py` | 已明确采用本地源码 |
| `src/notification/chart_generator.py` | 已明确采用本地源码 |
| `src/notification/email_notifier.py` | 已选业务但混合未授权范围，按hunk拆分 |
| `src/notification/feishu_notifier.py` | 已明确采用本地源码 |
| `src/notification/report_builders.py` | 已明确采用本地源码 |
| `src/notification/telegram_notifier.py` | 已明确采用本地源码 |
| `src/search/validation.py` | 已明确采用本地源码 |
| `src/search/workflow.py` | 已选业务但混合未授权范围，按hunk拆分 |
| `src/templates/appendix_methodology.md` | 已选业务但混合未授权范围，按hunk拆分 |
| `tools/feishu-pi/bridge.mjs` | 已明确采用本地源码 |
| `tools/feishu-pi/bridge.test.mjs` | 已明确采用本地源码 |

## 本地新增（103，包含12个冻结输入）

| 路径 | 处理 |
|---|---|
| `config/company_summary_contract.json` | 采用本地财报/研究合同与Prompt |
| `config/fundamental_data_api.md` | 采用本地财报/研究合同与Prompt |
| `config/fundamental_data_v2.json` | 采用本地财报/研究合同与Prompt |
| `config/personal_monitor_feishu_group_test.yaml.example` | 保留测试配置模板，不扩大生产范围 |
| `config/personal_monitor_feishu_test.yaml.example` | 保留测试配置模板，不扩大生产范围 |
| `config/prompts/company_summary.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/company_summary_recovery.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/peer_research.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_audit_v2.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_audit_v3.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_audit_v4.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_audit_v5.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_audit_v6.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_basis_v2.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_basis_v3.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_basis_v4.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_common.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_conclusion.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_draft_v2.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_draft_v3.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_draft_v4.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_draft_v5.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_draft_v6.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_final_v2.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_final_v3.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_final_v4.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_final_v5.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_final_v6.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_review.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_valuation_v4.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_valuation_v5.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_valuation_v6.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_visuals.md` | 采用本地财报/研究合同与Prompt |
| `config/prompts/research_write.md` | 采用本地财报/研究合同与Prompt |
| `config/research_report.json` | 采用本地财报/研究合同与Prompt |
| `data/fundamental_quant/tokenizer/tokenizer.provenance.json` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `docs/design/company_summary_contract.md` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `docs/design/company_summary_example.md` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `docs/design/research_report_example_v6.md` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `docs/development/peer_research_paid_20261024.md` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `scripts/accept_personal_monitor.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/audit_shared_runtime.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/backfill_ashare_sources.py` | 已明确采用本地新增源码 |
| `scripts/benchmark_shared_runtime.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/build_financial_context.py` | 已明确采用本地新增源码 |
| `scripts/build_fundamental_data.py` | 已明确采用本地新增源码 |
| `scripts/build_fundamental_data_v2.py` | 已明确采用本地新增源码 |
| `scripts/check_full_comparison.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/financial_pipeline.py` | 已明确采用本地新增源码 |
| `scripts/ocr_financial_page.ps1` | 已明确采用本地新增源码 |
| `scripts/pack_shared_runtime_benchmark.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/personal_monitor_process.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/personal_monitor_shared_test.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/personal_monitor_transport.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/prepare_company_summary.py` | 已明确采用本地新增源码 |
| `scripts/push_dataset_snapshot.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/push_dataset_snapshot_sftp.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/refresh_csi_reference_manifest.py` | 已明确采用本地新增源码 |
| `scripts/refresh_monitoring_h1_fundamentals.py` | 已明确采用本地新增源码 |
| `scripts/render_pi_campaign_costs.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/research_inputs.py` | 已明确采用本地新增源码 |
| `scripts/research_render.py` | 已明确采用本地新增源码 |
| `scripts/research_report.py` | 已明确采用本地新增源码 |
| `scripts/research_rounds.py` | 已明确采用本地新增源码 |
| `scripts/run_company_summary.py` | 已明确采用本地新增源码 |
| `scripts/run_peer_research.py` | 已明确采用本地新增源码 |
| `scripts/run_personal_monitor_feishu_group_test.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/run_personal_monitor_feishu_test.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/sync_server_market_data.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `scripts/validate_reference_universe_backfill.py` | 运维/测试工具未获明确选择，不自动覆盖或新增 |
| `src/data/source_screening.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/__init__.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/api_client.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/api_format.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/api_io.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/api_pipeline.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/disclosure/__init__.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/disclosure/absence.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/disclosure/contract.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/disclosure/gaps.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/disclosure/layout.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/disclosure/note_totals.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/handoff.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/history.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/metadata.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/model_context.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/v2_history.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/v2_schema.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/v2_transport.py` | 已明确采用本地新增源码 |
| `src/fundamental_data/v2_view.py` | 已明确采用本地新增源码 |
| `src/fundamental_quant/archive.py` | 已明确采用本地新增源码 |
| `src/fundamental_quant/legacy_reporting.py` | 上轮保留旧接口的隔离辅助模块，不接入当前业务根 |
| `src/fundamental_quant/legacy_service.py` | 上轮保留的纯辅助定义，不恢复旧服务 |
| `src/instruments/statement_scope.py` | 已明确采用本地新增源码 |
| `tests/fixtures/fundamental_data_v2_contract/README.md` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `tests/fixtures/fundamental_data_v2_contract/blocks_2024.json` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `tests/fixtures/fundamental_data_v2_contract/blocks_2025.json` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `tests/fixtures/fundamental_data_v2_contract/examples.json` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `tests/fixtures/fundamental_data_v2_contract/invalid_cases.json` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `tests/test_company_summary_execution.py` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `tests/test_peer_research_execution.py` | 新研究链最小输入/冻结依赖；已跟踪，必须随正式发布携带 |
| `tools/feishu-pi/framing.mjs` | 已明确采用本地新增源码 |
| `tools/feishu-pi/shared-runtime.mjs` | 已明确采用本地新增源码 |

## 线上独有（38，包含2个环境私有文件）

| 路径 | 处理 |
|---|---|
| `config/.env` | 保留各环境凭据和路径 |
| `config/config.yaml` | 生产运行配置保留，按需仅合业务键 |
| `config/optimizer.yaml` | 删除无消费者的旧优化器配置 |
| `scripts/build_current_valuation_feature_snapshot.py` | 已确认本轮退役删除 |
| `scripts/build_historical_valuation_consensus_features.py` | 已确认本轮退役删除 |
| `scripts/build_historical_valuation_features.py` | 已确认本轮退役删除 |
| `scripts/build_reverse_dcf_consensus_report.py` | 已确认本轮退役删除 |
| `scripts/diagnose_market_cost_expert_coverage.py` | 已确认本轮退役删除 |
| `scripts/diagnose_valuation_expert_coverage.py` | 已确认本轮退役删除 |
| `scripts/evaluate_causal_context_gate.py` | 已确认本轮退役删除 |
| `scripts/evaluate_causal_rank_context_gate.py` | 已确认本轮退役删除 |
| `scripts/evaluate_industry_rank_blends.py` | 已确认本轮退役删除 |
| `scripts/evaluate_industry_relative_factors.py` | 已确认本轮退役删除 |
| `scripts/evaluate_industry_relative_fundamentals.py` | 已确认本轮退役删除 |
| `scripts/evaluate_intrinsic_value.py` | 已确认本轮退役删除 |
| `scripts/evaluate_reverse_dcf_batch.py` | 已确认本轮退役删除 |
| `scripts/evaluate_valuation_causal_rank_gate.py` | 已确认本轮退役删除 |
| `scripts/evaluate_valuation_consensus_context.py` | 已确认本轮退役删除 |
| `scripts/evaluate_valuation_consensus_quality.py` | 已确认本轮退役删除 |
| `scripts/evaluate_valuation_context_increment.py` | 已确认本轮退役删除 |
| `scripts/evaluate_valuation_feature_ablation.py` | 已确认本轮退役删除 |
| `scripts/evaluate_valuation_industry_increment.py` | 已确认本轮退役删除 |
| `scripts/evaluate_valuation_rank_blends.py` | 已确认本轮退役删除 |
| `scripts/train_fundamental_embedding.py` | 已确认本轮退役删除 |
| `scripts/train_latent_peer_moe.py` | 已确认本轮退役删除 |
| `scripts/train_split_fundamental_pricing.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/causal_context_gate.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/causal_rank_context_gate.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/industry_blend_evaluation.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/industry_factor_evaluation.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/intrinsic_evaluation.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/latent_peer_moe.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/moe.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/pricing.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/reverse_dcf_consensus.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/split_evaluator.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/valuation_features.py` | 已确认本轮退役删除 |
| `src/fundamental_embedding/valuation_industry_evaluation.py` | 已确认本轮退役删除 |
