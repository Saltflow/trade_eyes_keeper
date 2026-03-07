# 股票量化系统 - 单页摘要

## 项目概述
A股量化监控系统，定时获取股票数据，检查技术条件（最低价<MA60），发送邮件提醒，可选LLM基本面分析。

**核心原则**：接口找不到就用爬虫，绝不使用模拟数据进行投资决策。

## ✅ 核心功能
1. **数据获取**：沪深A股日线数据（开盘、收盘、最高、最低、成交量）
2. **条件检查**：当天最低价 < MA60（前复权）
3. **邮件提醒**：HTML格式，包含详细股票状态
4. **LLM分析**：DeepSeek API基本面分析（可选）
5. **定时任务**：每天15:30自动运行

## 📊 当前配置
- **监控股票**：601728（中国电信）、600938（中国海油）
- **数据源**：新浪财经历史API（真实120天数据）
- **邮件设置**：
  - 发件：your_email@example.com（授权码：your_email_password_or_app_specific_password）
  - 收件：receiver_email@example.com
  - SMTP：smtp.yeah.net:465（SSL）
- **运行时间**：每天15:30（A股收盘后）

## 🏗️ 系统架构
```
股票量化系统/
├── config/config.yaml    # 配置文件
├── src/                 # 源代码
│   ├── data_fetcher.py  # 数据获取（akshare+爬虫）
│   ├── web_crawler.py   # 网页爬虫（新浪/腾讯/东方财富）
│   ├── condition_checker.py # 条件检查
│   ├── email_notifier.py    # 邮件通知
│   ├── llm_analyzer.py  # LLM分析
│   └── scheduler_manager.py # 定时任务
├── data/               # 历史数据存储
├── logs/              # 系统日志
└── main.py           # 主程序入口
```

## ⚙️ 关键技术
- **多数据源备份**：akshare → 新浪财经 → 腾讯财经 → 东方财富
- **真实数据优先**：拒绝模拟数据，确保投资决策准确性
- **SSL邮件连接**：解决yeah.net SMTP超时问题
- **前复权处理**：数据源提供前复权价格
- **MA60计算**：pandas rolling函数，60日移动平均

## 🚀 快速启动
```bash
# Windows
run.bat

# Linux/Mac
./run.sh

# 单次测试
python main.py --once

# 功能测试
python test_basic.py
```

## 📈 输出示例
**邮件内容**：
- 满足条件股票：代码、最低价、MA60、价差、跌幅%
- 所有监控股票：开盘、收盘、最高、最低、MA60、状态
- HTML表格格式，美观易读

**日志系统**：
- 系统日志：`logs/quant_system.log`
- 测试日志：`logs/test.log`

## 🔧 配置说明
```yaml
stocks: [601728, 600938]
email:
  smtp_server: smtp.yeah.net
  smtp_port: 465
  sender_email: your_email@example.com
  sender_password: "your_email_password_or_app_specific_password"
llm:
  api_key: "DeepSeek API密钥（可选）"
scheduler:
  run_time: "15:30"
```

## ✅ 已验证功能
1. ✅ 真实历史数据获取（新浪财经API）
2. ✅ 条件检查准确（最低价<MA60）
3. ✅ 邮件发送正常（SSL连接+授权码）
4. ✅ 定时任务稳定（APScheduler）
5. ✅ LLM分析可选（DeepSeek API）

## 🎯 设计特点
1. **模块化设计**：各功能独立，易于维护扩展
2. **容错机制**：数据源失败自动切换
3. **配置驱动**：YAML+环境变量，灵活安全
4. **跨平台**：Windows/Linux支持
5. **生产就绪**：日志、错误处理、定时任务

**状态**：稳定运行，使用真实历史数据，邮件通知正常。

**最后更新**：2026-03-05