# 股票量化系统 (Stock Quant)

[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![Status](https://img.shields.io/badge/status-active-blue)]()

> **English**: A cross-market monitoring and financial research system for A-shares, US stocks, and HK stocks. It combines reports and managed triggers, explicit backtesting, annual-report research, and reusable task, data, model, and agent runtimes.

A股 / 美股 / 港股监控与财报研究系统。支持行情、公告、日报与个人触发器，提供显式手动回测，以及从年报获取到完整研究的任务编排。邮件、飞书和 Telegram 按配置投递报告与任务回执。

文档核对：2026-11-04。共享底座与统一研究已纳入提交；调查建模扩展和旧模块清理仍含未提交的工作区改动。本页按当前源码整理，线上启用情况以目标配置和发布回执为准。

## 核心功能

完整年报研究从 `python scripts/financial_pipeline.py run` 启动；原提取、整合、摘要和文件交接命令继续作为分阶段入口。分别见[统一研究指南](docs/guide/research_pipeline.md)与[财报阶段指南](docs/guide/financial_pipeline.md)。

| 功能 | 说明 |
|------|------|
| **统一年报研究** | 年报与修订获取、结构化提取、跨年整合、公司摘要、同业横评及三轮研究；支持缓存复用、预算、进度、取消和续接 |
| **调查与建模** | 当前工作区扩展：Agent 补充证据、建立公司模型，在隔离 Python 容器内计算，再交接三轮写作；已有报告可显式重新调查 |
| **回测分析** | 统一 Backtester、成交价策略与无风险/510300/同池等权三重基准比较 |
| **标的画像审计** | 公司财务推导、利润增长、ETF 前十大穿透及 REIT/商品/债券类型化画像 |
| **报表触发器** | 日期、价格、公告、公开派息日及组合条件触发完整日报或简报；optimize 仅预留、不执行 |
| **日报与简报** | HTML / xelatex PDF、行情与公告；配置示例为 19:00 日报、09:50 / 14:30 简报，实际执行受调度开关和交易日设置控制 |
| **Ke 收益展示** | 读取合格的 Ke 历史仿真曲线；缺失、过期或覆盖不足会显示相应状态 |
| **运行与管理** | 独立调度服务、本地状态、飞书长连接与 Telegram 轮询命令 |
| **参考持仓** | 按市场保留持仓估值、公司行为和显式手动管理；不再根据旧信号自动调仓 |
| **规则引擎** | YAML 驱动的条件规则与受限表达式求值 |
| **共享执行与资料底座** | 通用持久任务、源站访问策略、原件与解析、模型请求证据和 native / Pi 循环；支持显式同机共享与源码导出 |

## 架构与状态归属

系统采用模块化单体和多个工作进程，SQLite 保存持久状态。服务宿主处理在线任务；统一研究由冻结版本的 host/worker 执行。完整调用图与边界见[架构总览](docs/architecture_overview.md)。

| 层次 | 模块 | 职责 |
|---|---|---|
| 业务宿主 | `core`、`interactive`、`personal_monitor` | 调度、Bot、会话权限、日报、触发器与个人任务 |
| 领域实现 | `research_pipeline`、`strategy`、`backtest` | 财务研究编排、策略合同与手动回测 |
| 可复用基础库 | `task_runtime`、`data_hub`、`model_gateway`、`agent_runtime`、`shared_contracts` | 执行、资料、模型、Agent 循环与共享身份 |
| 项目组合 | `platform_composition.py` | 注入本项目默认配置及明确的 authority 根目录 |

`core/execution.py` 是通用执行器的兼容入口。在线队列与研究 authority 队列各自拥有状态，复用实现不表示共用全部数据库；研究费用投影也不能与模型网关账本重复相加。同机跨项目复用需显式指向同一权限域，详见[共享平台指南](docs/guide/reusable_platform.md)。

## 运行与发布边界

日报、简报和受管触发器复用已有完整报告 worker；通知层只读取合格的 Ke 历史模拟曲线，不执行旧技术评估或自动调仓。参考持仓保留估值、公司行为和显式手动管理；手动回测使用统一 TradePlan / Backtester 和显式执行合同。

策略搜参、Solver、候选评价/晋升/激活、优化进程/锁/恢复/通知及相应 CLI/Bot 已整体删除。历史搜索产物保留为只读业务资料，不再生成或激活候选。受管动作中的 `optimize` 仅保留兼容类型且拒绝执行；实际可执行报告只有 `daily`、`brief`。

固定日报、启动日报和固定简报能力保留，实际运行受目标配置控制。统一年报研究及其模型、调查、估值和数据工具继续使用原链路。实际付费执行仍需要明确预算与授权；本地源码修改不能作为生产已部署的证据。

## 日报预览

![日报布局预览（2026-08-20 版式，脱敏示意）](docs/images/daily_report_preview_2026-08-20.png)

> 此图是历史版式示意，当前报告内容随展示策略与配置变化。当前报告展示合格的 Ke 收益曲线，旧技术扫描、回测卡片及参考持仓报告卡片已删除。预览图不包含真实标的、价格、收益、收件人或网络地址。

## 快速开始

**首次部署？** → [部署五步走](docs/guide/setup.md)

```bash
pip install -r requirements.txt
cp config/.env.example config/.env   # 填入邮箱和 API Key (DeepSeek)
cp config/config.yaml.example config/config.yaml # 填入完整标的和运行设置
python main.py --once                # 单次收盘日报
python main.py --brief               # 单次早盘简报
python main.py --audit-instruments   # 全量标的画像 JSON + HTML 审计
python main.py --service             # 唯一调度器 + 已启用的管理 Bot
python main.py --status              # 本地服务状态
```

PowerShell 中将 `cp` 换为 `Copy-Item`。PDF 报告需安装 xelatex；Pi 桥需准备 Node >=22.19 与锁定依赖，配置方法见[部署指南](docs/guide/setup.md)和[Bot 指南](docs/guide/feishu_telegram_setup.md)。

### 年报研究

以下命令只准备离线计划，不调用模型；输出目录须为新目录：

```bash
python -X utf8 scripts/financial_pipeline.py run --company a_share:600348 --years 7 --output data/fundamental_data/research_runs/preview_001
python -X utf8 scripts/financial_pipeline.py status --source data/fundamental_data/research_runs/preview_001
python -X utf8 scripts/financial_pipeline.py logs --source data/fundamental_data/research_runs/preview_001
```

执行全流程时，在 `run` 命令中添加 `--paid --budget-limit-usd <已授权金额>`；`--background` 用于后台执行。当前工作区的调查建模扩展需预先准备 Docker 和本地计算镜像；`--reinvestigate` 显式建立新研究版本并复用上游底稿。具体预算、环境、缓存、取消与续接合同见[统一研究指南](docs/guide/research_pipeline.md)。

### 导出通用基础库

```bash
python -X utf8 scripts/export_reusable_platform.py --namespace my_platform --output outputs/my-platform-source
```

导出五个公共包、依赖说明及源码哈希到新目录，不包含凭据、资料库、模型原响应或金融领域实现。使用方式见[共享平台指南](docs/guide/reusable_platform.md)。

## 项目结构

```
src/
├── strategy/          # TradingStrategy API、自动注册表和具体投资策略插件
│   ├── api.py         # StrategyMarketData / TradePlan / TradingStrategy
│   ├── registry.py    # 自动发现 plugins/，无需中央策略字典
│   └── plugins/       # percentile / regime_pullback / ...
├── search/            # 保留历史命名的显式执行配置与手动 WF 适配，无搜索驱动器
├── backtest/          # 唯一成交、资金仿真和评价引擎
├── experiments/       # 保留的独立估值与 Ke 离线试验
├── markets.py         # 市场分组、手数和跳过配置
├── core/              # 服务宿主、调度、报表适配与执行器兼容入口
├── task_runtime/      # 通用持久任务、租约、并发与源站请求策略
├── data_hub/          # 原件索引、中立解析、原始行情与资料引用
├── model_gateway/     # 模型准入、实际请求证据、usage 与费用账本
├── agent_runtime/     # 宿主注入模型和工具的 native / Pi 循环
├── shared_contracts/  # 明确的 AuthorityRef 共享身份
├── platform_composition.py # 本项目配置与 authority 默认组合
├── data/              # 多源行情、公告与 LLM 数据提取
├── alerting/          # 多层报警引擎 + 状态管理
├── session/           # 当前日报运行上下文（待后续收敛命名）
├── models/            # 当前日报 Pydantic 模型（待后续收敛命名）
├── instruments/       # 类型化标的、财务推导、基金穿透和审计报告
├── notification/      # 邮件通知 + 图表生成
├── interactive/       # 飞书长连接 / Telegram 轮询，会话与受控工具
├── personal_monitor/  # 个人持仓、提醒、版本与投递 outbox
├── fundamental_data/  # 财报提取、跨年整合、摘要和研究交接
├── research_pipeline/ # 完整研究编排、预算、恢复、维修与交付
├── utils/             # CJK 字体, ETF 检测
└── templates/         # HTML/CSS 模板
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `python main.py` / `--service` | 启动统一调度与 Bot 服务 |
| `python main.py --once` | 单次收盘日报 |
| `python main.py --brief [id]` | 早盘/收盘简报 (`morning_snapshot` / `afternoon_snapshot`) |
| `python main.py --audit-instruments` | 对全量配置标的生成类型化 JSON 与详细 HTML 审计 |
| `python scripts/financial_pipeline.py run ...` | 默认离线计划；显式付费授权与预算后执行统一年报研究 |
| `python scripts/financial_pipeline.py status --source RUN_DIR` | 查询研究状态、阶段、预算及缺口 |
| `python scripts/financial_pipeline.py logs --source RUN_DIR` | 读取研究事件与日志，可加 `--follow` |
| `python scripts/financial_pipeline.py artifacts --source RUN_DIR` | 查询已生成的研究产物 |
| `python scripts/financial_pipeline.py cancel --source RUN_DIR` | 停止新的研究派发，保留在途与既有结果 |
| `python scripts/financial_pipeline.py resume --source RUN_DIR --paid` | 沿用原预算和冻结输入，续接从未发送的工作 |
| `python scripts/export_reusable_platform.py --namespace my_platform --output NEW_DIR` | 导出通用库源码到新命名空间 |
| `python main.py --interactive` | 仅启动已启用的管理 Bot |
| `python main.py --status` | 读取本地服务心跳，退出码表示是否就绪 |

HTTP/HTTPS health server、管理页面与报告临时链接已整体下线。使用飞书/TG Bot
更灵活、更安全，无需开放管理端口。设置见 [Bot 指南](docs/guide/feishu_telegram_setup.md)。

飞书支持持久个人触发器：可直接说“11月2号逆回购到期”、
“601088跌到50以下了发简报”或“任何监控标的发三季度报告的当天和次日发简报和日报”。
用“拉取已经激活的触发器”查看固定编号，`remove trigger 1,2,3` 批量删除；
更多日期、组合条件、暂停/修改和投递规则见[触发器指南](docs/development/managed_triggers_20261031.md)。

## 配置

- `config/config.yaml` — 监控标的、技术指标参数 (gitignored)
- `config/.env` — 邮箱密码、API Key (gitignored)
- `config/optimizer_constraints.yaml` — 保留历史路径，仅供显式回测的执行、Walk-Forward、基准和 profile 合同，不启动优化器
- `config/alerts.yaml` — 共享指标锚点配置（保留原路径）
- `config/fundamental_data_v2.json`、`config/company_summary_contract.json` — 提取与单公司摘要合同
- `config/research_report.json`、`config/prompts/` — 研究阶段、调查建模和写作约束
- `config/research_tariff_calendar.json` — 研究核价所需的已核验节假日日历
- `storage.authority_root` / `TRADE_EYES_AUTHORITY_ROOT` — 显式共享资料、请求策略及模型状态目录

## 文档

[完整文档索引](docs/README.md) · [架构图独立页](docs/architecture_overview.md)

| 文档 | 说明 |
|------|------|
| [设计决策](docs/llm/proj4llm.md) | 关键架构设计 + 技术路线 |
| [全项目架构总览](docs/architecture_overview.md) | 总图与飞书路由、公共层、报告触发和财报研究放大图，配置开关、本机/线上对照及源码导航 |
| [架构说明](docs/architecture.md) | 分层架构、数据流、模块职责 |
| [统一年报研究](docs/guide/research_pipeline.md) | 离线计划、授权执行、缓存、进度、恢复、调查建模与维修边界 |
| [共享平台与源码导出](docs/guide/reusable_platform.md) | 五个公共包、authority、资料与模型事实归属、跨项目复用 |
| [财报阶段入口](docs/guide/financial_pipeline.md) | 独立提取、整合、摘要及文件交接 |
| [正式研究](docs/guide/research_report.md) | 独立三轮写作入口及上下文、图表交接合同 |
| [部署指南](docs/deployment.md) | 服务器部署 + CI/CD |
| [配置参考](docs/configuration.md) | config.yaml 详细说明 |
| [快速开始](docs/guide/quickstart.md) | 5 分钟上手 |
| [开发日志](docs/development/devlog.md) | 版本演进 |
| [当前待办](docs/development/todo_backlog.md) | 当前工作项与验收欠项 |

## 测试

```bash
python -X utf8 -m pytest tests/validation/  # 提交前必跑验证
python -X utf8 -m pytest tests/            # 全量测试，沿用 pytest.ini 覆盖率门槛
python -X utf8 -m pytest tests/ --cov=src   # 覆盖率报告
python -X utf8 -m pytest tests/test_import_smoke.py
python -X utf8 -m pytest tests/interactive/test_security.py
```

只做定向功能回归时，可用 `-o addopts=` 清空全库覆盖率选项，并使用仓库内 `--basetemp=.tmp/<本次测试目录>`；该结果不代表全库覆盖率达标。Windows 上先创建 `.tmp` 父目录。真实来源、模型、Bot 和服务器验收与离线测试分别记录。

## 功能进展与验收边界

| 方向 | 状态 | 内容 |
|------|------|------|
| **发布安全门禁** | ✅ 已完成 | 部署前执行 Ruff `F/E/S110`、导入烟测、核心路径测试、远端严格配置校验与 systemd/本地心跳检查；不因无关的历史格式债务阻塞发布 |
| **报告与通知** | 已实现，按配置启用 | HTML、PDF 和多渠道报告；统一采用 Ke 收益展示，旧技术报告路径已删除，个人触发器独立管理 |
| **统一研究与共享底座** | 已提交本地实现 | 持久研究任务、跨批资料与产物复用、模型请求证据和费用、Pi 维修、五个公共包导出；[研究验收](docs/development/research_pipeline_acceptance_20261101.md)、[平台验收](docs/development/reusable_platform_acceptance_20261101.md) |
| **调查建模扩展** | 工作区实现，含未提交改动 | 调查补证、隔离 Python 计算、研究交接及一次审阅返工；[设计与使用边界](docs/design/research_investigation.md) |
| **可配置回测区间** | ✅ 已完成 | `/backtest 代码 起 止` 自动准备指标预热、raw/qfq、公司行为与基准，缺失时补数后执行严格校验 |
| **标的画像与审计** | 🔄 进行中 | 公司财务推导、ETF/REIT/商品/债券类型化画像，以及带发布日期和防前视约束的 point-in-time 数据回填 |
| **港股税后现金分红** | ✅ 已完成（P1） | `hk: 20%` 账户层保守预扣率进入 raw 成交/qfq 信号/显式现金流合同；策略、静态基准、外部基准与参考持仓均记录税前、税额与税后现金，缺少因果公告日的分红 fail closed |
| **跨股票实物分派** | v1.20.0 | 统一权益/交付/结算账本；显式港股通估算政策与实际凭据分开，支持名义持有人现金分摊及原始分段选举。三次腾讯事件对账、原配置三市场完整搜参与报告验收完成，未晋升策略；见[实施记录](docs/development/in_kind_ledger_20261031.md)与[发布记录](docs/development/in_kind_release_20261031.md) |
| **旧基本面研究与实验** | 已退役 / 终止 | 当前工作区 `fundamental_quant` 仅保留历史只读入口；旧 MOE、评分和 Ke 实验终止。新研究编排、仍有消费者的估值适配及新的 Ke 收益展示各有独立边界，见[研究退役说明](docs/development/research_retirement_20261024.md)与[实验终止记录](docs/development/failed_quant_experiments_retirement_20261030.md) |
| **周粒度降载** | ⏸ 暂缓（P5） | 默认日粒度；只在压测证明服务器超出负载阈值后评估周聚合、周高买入/周低卖出方案 |

研究性功能不自动修改参考持仓。已下线的优化、晋升与激活流程不再提供执行入口；历史运行只供读取。

旧 Fundamental Quant 研究、周通知及专用历史 CLI/Bot 入口已删除；历史 PDF、SQLite、tokenizer 和研究原文保留。公开历史报告可通过现有 `data_find/data_read` 读取，归属未确认的旧混合数据库不向机器人开放。

公共运行层旧重复运行链路退役见[记录](docs/development/shared_runtime_retirement_20261029.md)。带日期的发布与实机记录只证明对应版本和范围；本页不将旧发布快照作为当前线上版本，也不把任务完成等同于财务结论已独立核验。

## 许可

BSD-3-Clause. 详见 [LICENSE](LICENSE).
