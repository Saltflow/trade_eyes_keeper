# 股票量化系统

A股/港股/美股量化监控系统，自动获取交易数据、计算技术指标、检测条件、发送邮件提醒。

## 核心功能

- **数据获取**：多源实时日线数据（新浪、腾讯、东方财富）+ baostock 历史数据
- **条件检测**：价格低于 MA60 / WMA20 等技术指标锚点，多层阈值警报
- **邮件提醒**：满足条件时自动发送 HTML 格式邮件
- **公告分析**：巨潮资讯网官方公告抓取 + LLM 结构化提取
- **回测框架**：基于历史数据的策略回测
- **健康监控**：HTTP 健康检查服务器 + 管理后台

## 快速开始

```bash
pip install -r requirements.txt
cp config/.env.example config/.env   # 填入邮箱和 API Key
python main.py --once                # 单次运行验证
python main.py                       # 定时运行
```

详细步骤见 [快速开始指南](docs/guide/quickstart.md)。

## 文档

| 文档 | 说明 |
|------|------|
| [快速开始](docs/guide/quickstart.md) | 5 分钟上手 |
| [架构说明](docs/architecture.md) | 分层架构、数据流、模块职责 |
| [配置说明](docs/configuration.md) | config.yaml / alerts.yaml / .env 全字段 |
| [部署指南](docs/deployment.md) | 生产部署、CI/CD、运维 |
| [开发规范](docs/development/conventions.md) | 代码风格、项目约定 |
| [开发日志](docs/development/devlog.md) | 版本历史与功能演进 |
| [LLM 设计决策](docs/llm/design_decisions.md) | 大模型集成架构与额度管理 |
| [项目设计文档](docs/llm/proj4llm.md) | 完整的关键设计决策记录 |

## 项目结构

```
├── config/           配置文件（config.yaml, alerts.yaml, .env）
├── src/              源代码
│   ├── data_fetcher.py      数据获取协调
│   ├── web_crawler.py       网页爬虫
│   ├── technical_indicators.py  指标计算
│   ├── session_manager.py   Session 管理
│   ├── condition_checker.py 条件检查
│   ├── alert_engine.py      多层警报引擎
│   ├── email_notifier.py    邮件通知
│   ├── llm_analyzer/        LLM 分析
│   ├── health_server/       健康检查与管理
│   └── ...
├── docs/             项目文档
├── tests/            测试（unit/ integration/ validation/）
├── cache/            数据缓存
├── data/             历史数据与邮件存档
├── logs/             运行日志
├── main.py           主程序入口
├── AGENTS.md         AI Agent 工作流规范
└── requirements.txt  Python 依赖
```

## 许可证

本项目仅供学习和研究使用。
