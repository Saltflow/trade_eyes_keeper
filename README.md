# 股票量化系统 (Stock Quant)

[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://python.org)
[![Status](https://img.shields.io/badge/status-active-blue)]()

> **English**: A cross-market quantitative monitoring system for A-shares, US stocks, and HK stocks. Features solver-neutral strategy optimization, a single unified backtester, intrinsic-value and instrument audits, daily xelatex PDF reports, and multi-channel notifications.

A股 / 美股 / 港股量化监控系统。策略搜索优化器自动发现最优交易信号，每日 xelatex LaTeX PDF 日报含信号扫描 + 回测分析 + 公式方法论附录，支持 HTML 交互报告链接与 Telegram/飞书多渠道通知。

## 核心功能

| 功能 | 说明 |
|------|------|
| **策略搜索优化器** | 按市场独立执行 22 窗 Walk-Forward，16 窗排名、2 窗隔离、4 窗留出 |
| **信号扫描器** | 加载单一活动策略参数，直接读取统一 TradePlan 最后有效日事件 |
| **回测分析** | 统一 Backtester、成交价策略与无风险/510300/同池等权三重基准比较 |
| **标的画像审计** | 公司财务推导、利润增长、ETF 前十大穿透及 REIT/商品/债券类型化画像 |
| **条件检测** | 多锚点阈值报警 (MA60/WMA20/WMA30/WMA50) + 优化策略信号报警 |
| **早盘/收盘简报** | 轻量价格+锚点快照，每日 09:50 / 14:30 自动发送，按偏离率升序排列 |
| **邮件提醒** | 日报含信号扫描+回测+公式附录，xelatex LaTeX PDF 附件 (港式财报风格) |
| **健康监控** | HTTP 健康检查服务器 (OTP 认证 + 管理后台 + 在线编辑监控列表) |
| **投资组合策略** | 共享资金池模拟，贪心前向选择，月度限额约束 |
| **规则引擎** | YAML 驱动，Python 表达式沙箱，23 个单元测试 |
| **公式附录** | LaTeX 排版 13 节指标方法论（RSI/布林/MACD/ADX/回测约束），xelatex 编译 |

## 运行与发布边界

部署环境从外部活动指针按市场加载已验证策略和参数；public README 只记录
公开的运行合同，不记录生产运行号、当前活动策略、服务器地址或收件人。
所有通知、今日扫描和参考持仓都从同一个活动指针读取，不在通知层重新计算收益或胜率。

晋升比较使用统一的无风险、配置基准和同池等权基准；框架允许额外的
`universe_equal_weight` 基准，因此门槛配置的是“至少 3 个基准”，而不是“只能有 3 个”。
候选仍需通过完整性、留出和安全门槛后才可激活；每个市场独立比较和切换，使用
`python main.py --activate-run RUN_ID --group MARKET`。

## 日报预览

![日报布局预览（2026-08-20 版式，脱敏示意）](docs/images/daily_report_preview_2026-08-20.png)

> 每日自动生成 HTML 报告和 LaTeX PDF 附件，邮件、飞书和 Telegram 共享同一
> `EvaluationReport`。报告包含监控标的、回测结果、参考基准、期末持仓、胜率、
> 周 NAV 图和季末模拟持仓；周 NAV 以图形展示，不再堆成长表格。仓库中的预览图
> 仅展示版式，不包含真实标的、价格、收益、收件人或网络地址。

## 快速开始

**首次部署？** → [部署五步走](docs/guide/setup.md)

```bash
pip install -r requirements.txt
cp config/.env.example config/.env   # 填入邮箱和 API Key (DeepSeek)
python main.py --once                # 单次收盘日报
python main.py --brief               # 单次早盘简报
python main.py --optimize            # 对配置活动策略执行统一搜参
python main.py --activate-run RUN_ID --group MARKET # 按市场人工激活留出通过的候选
python main.py --audit-instruments   # 全量标的画像 JSON + HTML 审计
python scripts/benchmark_technical_strategies.py --solver random --depth 1000 --market-workers 12 --evaluation-workers 1  # 五策略统一基准
python scripts/benchmark_search_throughput.py --candidates 1000  # 标量/批量吞吐验收
python scripts/analyze_search_depth.py  # 1000→10000 搜索边际效应
python main.py                       # 定时运行 (cron/APScheduler)
```

## 项目结构

```
src/
├── strategy/          # TradingStrategy API、自动注册表和具体投资策略插件
│   ├── api.py         # StrategyMarketData / TradePlan / TradingStrategy
│   ├── registry.py    # 自动发现 plugins/，无需中央策略字典
│   └── plugins/       # builder / percentile / regime_pullback / ...
├── search/            # Solver API、搜索编排、Gate、验证和产物
│   ├── api.py         # Candidate / SearchProblem / Solver 稳定公共合同
│   ├── controller.py  # Solver 无关的 ask/tell 编排
│   ├── registry.py    # 自动发现 solvers/
│   └── solvers/       # Genetic / Random / 单线 Simulated Annealing
├── backtest/          # 唯一成交、资金仿真和评价引擎
├── experiments/       # 跨策略 benchmark 与搜索深度实验，不进入生产激活链
├── markets.py         # 市场分组、手数和跳过配置
├── core/              # 数据拉取、条件检查、调度管理
├── data/              # 多源行情、公告与 LLM 数据提取
├── alerting/          # 多层报警引擎 + 状态管理
├── session/           # 当前日报运行上下文（待后续收敛命名）
├── models/            # 当前日报 Pydantic 模型（待后续收敛命名）
├── instruments/       # 类型化标的、财务推导、基金穿透和审计报告
├── notification/      # 邮件通知 + 图表生成
├── health_server/     # HTTP 健康检查 + 管理
├── utils/             # CJK 字体, ETF 检测
└── templates/         # HTML/CSS 模板
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `python main.py` | 启动定时调度器 (cron) |
| `python main.py --once` | 单次收盘日报 |
| `python main.py --brief [id]` | 早盘/收盘简报 (`morning_snapshot` / `afternoon_snapshot`) |
| `python main.py --optimize` | 按市场独立执行 Walk-Forward 搜参 |
| `python main.py --activate-run RUN_ID --group MARKET` | 原子激活指定市场中留出与稳健性均通过的候选 |
| `python main.py --audit-instruments` | 对全量配置标的生成类型化 JSON 与详细 HTML 审计 |
| `python scripts/benchmark_technical_strategies.py --solver random --depth 1000 --market-workers 12 --evaluation-workers 1` | 冻结各市场行情快照，并行比较五个注册技术策略；可切换 GA/随机/退火，只写诊断产物 |
| `python scripts/benchmark_search_throughput.py --candidates 1000` | 用完整 A 股冻结输入比较同一候选的标量与 CPU 批量评价吞吐、RSS 和一致性 |
| `python scripts/analyze_search_depth.py` | 用同一 RandomSolver 候选流测量 1,000→10,000 搜索边际效果并绘图 |
| `python main.py --health-server` | 仅启动健康服务器 |

`--optimize-v2` 不再是有效入口；增加 Solver 时只需实现并注册统一的
`Solver`（`ask/tell/checkpoint`），`SearchController`、策略和通知层无需增加算法分支。

## 配置

- `config/config.yaml` — 监控标的、技术指标参数 (gitignored)
- `config/.env` — 邮箱密码、API Key (gitignored)
- `config/optimizer_constraints.yaml` — Solver、Gate、窗口、成交和资源合同
- `config/alerts.yaml` — 多锚点报警配置

## 文档

| 文档 | 说明 |
|------|------|
| [设计决策](docs/llm/proj4llm.md) | 关键架构设计 + 技术路线 |
| [架构说明](docs/architecture.md) | 分层架构、数据流、模块职责 |
| [部署指南](docs/deployment.md) | 服务器部署 + CI/CD |
| [配置参考](docs/configuration.md) | config.yaml 详细说明 |
| [快速开始](docs/guide/quickstart.md) | 5 分钟上手 |
| [开发日志](docs/development/devlog.md) | 版本演进 |

## 测试

```bash
pytest tests/ -p no:capture -q          # 全量测试
pytest tests/ --cov=src                 # 覆盖率报告
pytest tests/test_import_smoke.py       # 模块导入完整性
pytest tests/test_security.py           # 安全测试
```

## Roadmap

| 方向 | 状态 | 内容 |
|------|------|------|
| **统一策略评估** | ✅ 已完成 | 单一 `--optimize` 入口；14 个 12/9/3 自然月窗口；统一 TradePlan、Backtester 与 EvaluationReport |
| **Solver 解耦** | ✅ 已完成 | `SearchController` 与 Genetic / Random / Simulated Annealing / Local Genetic 插件分离；新增算法不改策略和评价层 |
| **报告与通知** | ✅ 已完成 | HTML、PDF、邮件、飞书和 Telegram 共享同一报告对象；周 NAV 以图形展示，季末持仓和期末持仓统一来源 |
| **标的画像与审计** | 🔄 进行中 | 公司财务推导、ETF/REIT/商品/债券类型化画像，以及带发布日期和防前视约束的 point-in-time 数据回填 |
| **基本面 embedding / DCF** | 🧪 研究中 | 分离公司画像与市场定价逻辑；CAPM-DCF 已具备 A/HK/US 独立点时数据合同，但须各市场完成广域训练、留出验证与统一 benchmark 后才可人工激活 |
| **股票池稳健性** | 📋 下一步 | 将标的池扰动、删减变体和横截面排序纳入统一 benchmark，结果需人工确认后才可激活 |
| **可配置回测区间** | 📋 下一步 | 支持自定义起止日期、基准对比和训练/测试分离，同时复用统一成交与评价合同 |

研究性功能默认不改变活动策略；只有通过统一门槛、完整性和留出验证的候选，才允许人工激活。

## 许可

BSD-3-Clause. 详见 [LICENSE](LICENSE).
