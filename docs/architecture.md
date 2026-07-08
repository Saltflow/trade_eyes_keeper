# 架构说明

本文档描述股票量化系统的整体架构、数据流与各模块职责。

---

## 系统分层

```
┌─────────────────────────────────────────────────────────────────┐
│  配置层 (config/)                                               │
│  - config.yaml          股票列表、数据源、邮件、调度器配置      │
│  - alerts.yaml          技术指标锚点配置（ma60, wma20...）      │
│  - .env                 敏感信息（API Key、邮箱密码）           │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  数据源层 (src/)                                                │
  │  - web_crawler.py      网页爬虫（新浪、腾讯、Yahoo）            │
│  - data_fetcher.py     数据获取协调、缓存管理、调用指标计算     │
│  - announcement_fetcher.py  巨潮资讯网官方公告抓取              │
│  - financial_report_fetcher.py / manager.py  财报分析          │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  指标计算层 (src/)                                              │
│  - technical_indicators.py   统一指标计算器，从 alerts.yaml 读取 │
│  - utils/etf_detector.py     ETF 检测工具（消除硬编码）         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  分析策略层 (src/analysis/)                                      │
│  - strategy_optimizer_v2.py  Walk-Forward 14窗口 + 遗传搜索（02:00 cron）│
│  - signal_scanner.py         每日共识信号扫描（读 YAML，不重搜参）      │
│  - portfolio_strategy.py     共享资金池模拟 + PortfolioEvaluator        │
│  - rule_engine.py             YAML 驱动规则引擎 + 表达式沙箱    │
│  - indicator_library.py      RSI/MACD/ATR/布林/ADX/量比计算     │
│  - backtest_config.py         回测时间线约束 (观察/部署/持仓)    │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  业务层 (src/)                                                  │
│  - session_manager.py    Session 统一管理（Pydantic 数据模型）  │
│  - condition_checker.py  条件检查（价格 < MA60 等）             │
│  - alert_engine.py / processor / state_manager  多层警报系统    │
│  - email_notifier.py     邮件通知与 HTML 报表生成               │
│  - llm_analyzer/         LLM 基本面分析与公告提取               │
│  - health_server/        HTTP 健康检查与管理后台                │
│  - backtest_framework.py 回测框架                               │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  基础设施层                                                     │
│  - cache/                数据缓存（cache/data/, cache/analysis/）│
│  - cache/historical/     回测历史数据缓存（JSON Lines）         │
│  - data/                 股票历史数据 CSV、邮件存档             │
│  - logs/                 运行日志与审计日志                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 核心数据流

### 每日任务主链路（Session-based）

```
main.py --once
  └── session_manager.create_session()
        ├── data_fetcher.fetch_to_session()       → session.stocks_data
        │      ├── web_crawler 获取原始价格数据
        │      ├── technical_indicators 计算 MA60 / WMA20
        │      └── 公告/财报/股息数据填充
        ├── condition_checker.check_from_session() → session.alerts
        │      └── alert_engine / alert_processor
        ├── signal_scanner.scan()                  → 共识信号（今日触发）
        │      ├── 加载最新优化结果 (Top-5 策略, 02:00 cron 产出)
        │      ├── 用 YAML rules 条件评估当日数据
        │      └── 日报/简报直读 YAML 预估收益，不重新搜参
        └── email_notifier.send_from_session()
               ├── _generate_daily_pdf()
               │      ├── report_daily.tex 模板注入数据
               │      ├── xelatex 编译两次 (LaTeX 交叉引用)
               │      └── appendix_methodology.md → LaTeX 公式
               ├── yagmail.send(html + pdf 附件)
               └── 保存副本至 data/email_archive/
```

> **关键原则**：v3.0 起不再直接传递 DataFrame 和 dict，全部通过 `SessionContext` 流转，防止字段名称不一致导致的数据错误（如邮件显示所有股票价格为 0.00）。

---

## 模块职责

| 模块 | 职责边界 | 不负责的领域 |
|------|---------|------------|
| `web_crawler.py` | 从公开网站获取原始日线数据、股息历史 | **不计算** MA60 等任何技术指标 |
| `data_fetcher.py` | 协调多数据源、缓存命中判断、调用指标计算层 | **不直接解析** HTML/JSON |
| `technical_indicators.py` | 根据 `alerts.yaml` 配置计算所有技术指标 | **不读取**网络数据 |
| `session_manager.py` | 维护 Session 生命周期、类型安全数据模型 | **不包含**业务规则 |
| `condition_checker.py` | 基于 Session 数据判断警报条件 | **不修改** Session 中的原始数据 |
| `email_notifier.py` | 构建并发送邮件、xelatex PDF 生成、图表生成 | **不抓取**外部数据 |
| `signal_scanner.py` | 加载优化 YAML、用 rules 评估今日共识信号 | **不重新搜参**、**不修改**原始数据 |
| `strategy_optimizer_v2.py` | Walk-Forward 14窗口搜参（02:00 cron）、写 YAML | **不直接**调用邮件、**日报不调用它** |
| `cache_manager.py` | 读写本地缓存、过期清理、完整性校验 | **不发起**网络请求 |

---

## 数据源优先级

### 价格数据

1. **网页爬虫**（新浪财经历史数据 API，主要）
2. **腾讯财经 API**（备用）
3. **Yahoo Finance API**（备用）
4. **baostock**（回测历史数据专用，前复权）

### 公司公告

1. **巨潮资讯网 (cninfo)** 官方公告 API（主要）
2. 上海 / 深圳证券交易所 API（备用）
3. 新浪财经公告页面（降级）

### 股息数据

1. **LLM 提取缓存**（从公告解析，优先）
2. **网页爬虫**（新浪财经股息历史页面，备用）

---

## 缓存策略

| 缓存类型 | 路径 | 保留天数 | 说明 |
|---------|------|---------|------|
| 日线数据缓存 | `cache/data/` | 7 天 | 按股票代码分文件 |
| 分析结果缓存 | `cache/analysis/` | 7 天 | LLM 分析、公告提取结果 |
| 历史数据缓存 | `cache/historical/` | 30 天 | 回测用 JSON Lines |
| 邮件存档 | `data/email_archive/` | 手动清理 | HTML 邮件副本 |

**特殊规则**：交易日 15:55 后若缓存数据非当日，自动绕过缓存强制刷新。

---

## 扩展点

- **新增技术指标**：在 `alerts.yaml` 的 `anchors` 中添加配置，`technical_indicators.py` 自动识别
- **新增数据源**：实现与 `web_crawler.py` 同接口的模块，在 `data_fetcher.py` 中注册降级链
- **自定义警报规则**：修改 `alerts.yaml` 的 `thresholds` 与 `boundary_rules`
- **新增策略构建器**：在 `indicator_library.py` 添加信号函数，`optimizer.yaml` 注册 builder
- **自定义日报模板**：修改 `report_daily.tex` (LaTeX) 和 `email_template.html` (邮件正文)
- **新增公式**：编辑 `appendix_methodology.md`，Markdown → LaTeX 编译链自动处理

---

**相关文档**：
- [部署指南](deployment.md)
- [配置说明](configuration.md)
- [LLM 设计决策](llm/design_decisions.md)
