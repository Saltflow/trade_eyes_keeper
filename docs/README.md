# 文档索引

整理日期：2026-10-29。当前使用方式以入口指南和源码合同为准；带日期的开发、审计与报告记录只说明当时的结果。

## 架构与运行

| 文档 | 内容 |
|---|---|
| [架构图独立页](architecture_overview.md) | 全项目、飞书开关与路由、公共层、告警、搜参、财报研究六张图，以及本机/线上环境对照 |
| [架构与模块职责](architecture.md) | 当前执行机制、业务合同、发布边界与依赖债务 |
| [快速开始](guide/quickstart.md) | Python 环境、配置模板、运行和查看结果 |
| [部署与运行](deployment.md) / [首次部署](guide/setup.md) | 服务、依赖、发布及核验 |
| [配置参考](configuration.md) | 主配置、环境变量、市场策略与执行设置 |
| [飞书与 Telegram](guide/feishu_telegram_setup.md) | Bot 凭证、权限及命令 |
| [个人监控隔离测试](guide/personal_monitor_feishu_test.md) | 测试隔离与当前停用状态；不自动恢复本机任务 |

## 业务指南与合同

| 文档 | 内容 |
|---|---|
| [策略搜索与回测](llm/mainline_strategy_search_backtest.md) / [相对晋升](llm/relative_strategy_promotion.md) | 统一策略、回测、搜索窗口与激活合同 |
| [财报流程](guide/financial_pipeline.md) | 提取、跨年整合、摘要、第三阶段文件交接 |
| [正式研究](guide/research_report.md) | 输入驱动的 v6 模板、固定三轮与图表交接 |
| [单公司摘要](design/company_summary_contract.md) / [横评](design/peer_research_v1.md) | 阶段输入输出合同 |
| [财报数据 V2](design/fundamental_data_contract_v2.md) | 提取结构与边界 |
| [公告](guide/announcements.md) / [A 股源资料采集](guide/ashare_source_backfill.md) | 数据抓取、归档、缺口与回填 |
| [数据集目录](guide/dataset_catalog.md) / [逐时点数据](llm/point_in_time_data.md) | 数据发现及防前视合同 |
| [策略插件](../src/strategy/README.md) / [Solver 插件](../src/search/README.md) | 扩展实现说明 |
| [跨股票分派账本](development/in_kind_ledger_20261031.md) | 统一权益与结算、港股通估算政策、真实三事件及完整搜参验收边界 |

## 变更、验收与退役

- [v1.20.0实物分派发布](development/in_kind_release_20261031.md)：同一提交对齐本地、GitHub与生产，最小估算开关同步及配置/账户保护证据。
- [基本面量化实验终止](development/failed_quant_experiments_retirement_20261030.md)：旧估值/行业评分/消融、MOE训练、Ke及状态回放因效果始终未正删除，后续不再尝试。
- [项目变更记录](llm/proj4llm.md)：按日期记录实现和验收，不作为当前状态总表。
- [财报与prompt缓存审计](development/financial_prompt_cache_audit_20261030.md)：真实阶段命中率、公共前缀断点、零丢失排列实验及费用优化边界；未执行付费改版验证。
- [公共运行层迁移记录](development/shared_runtime_migration.md)：历史失败、生产部署证据及本机未完成项。公共层已于 10 月 28 日部署，本机测试任务随后主动停用，旧测试源码退役仍未完成。
- [飞书研究问答修复](development/feishu_research_qa_20261029.md)：去模板短路、强制Pi检索、真实模型回放与两文件发布候选；尚未部署。
- [旧研究退役与历史只读](development/research_retirement_20261024.md)：旧执行链已移除；历史查询和已有文件保留。被替代的旧模块操作说明已合并至此。
- [基本面量化复盘](reports/fundamental_quant_postmortem_20261023.md)：历史问题与经验，不能按其中旧入口启动任务。
- `development/` 中其他带日期记录、`reports/`、`archive/`：历史审计、验收与产物说明；旧命令和状态不代表当前能力。
- `design/` 及 `llm/` 中研究性合同：设计或实验边界，是否接入生产以架构页和具体发布证据为准。

HTTP 管理/OTP 已下线；旧基本面研究、PDF 会话、Pi 专用研究包装及自动引用复核执行器已退役。指定旧量化评分、MOE训练和Ke实验已终止。通用助手/Pi桥、财报数据流程、技术策略及保留诊断仍有各自现行入口。
