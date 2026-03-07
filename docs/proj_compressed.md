# 股票量化系统 - 单页摘要

## 项目目标
A股量化监控：获取日线数据→检查最低价<MA60→邮件提醒→可选LLM基本面分析。

**核心原则**：真实数据优先，拒绝模拟数据用于投资决策。

## ✅ 功能概览
1. **数据获取**：沪深A股日线（开盘/收盘/最高/最低/成交量）
2. **条件检查**：当天最低价 < MA60（前复权）
3. **邮件提醒**：HTML格式，含详细股票状态
4. **LLM分析**：DeepSeek API基本面分析（可选）
5. **定时运行**：每天15:30自动执行

## 📊 当前配置
- **监控股票**：601728（中国电信）、600938（中国海油）
- **数据源**：新浪财经历史API（真实120天数据）
- **邮件**：your_email@example.com → receiver_email@example.com
- **SMTP**：smtp.yeah.net:465（SSL，授权码认证）
- **运行时间**：每天15:30（A股收盘后）

## 🏗️ 系统结构
```
股票量化系统/
├── config/config.yaml    # 配置
├── src/                 # 源码
│   ├── data_fetcher.py  # 数据获取
│   ├── condition_checker.py # 条件检查
│   ├── email_notifier.py    # 邮件通知
│   ├── llm_analyzer.py  # LLM分析
│   └── scheduler_manager.py # 定时任务
├── data/               # 数据存储
├── logs/              # 系统日志
└── main.py           # 主入口
```

## ⚙️ 技术特点
- **多数据源备份**：akshare→新浪财经→腾讯财经→东方财富
- **真实数据**：拒绝模拟，确保投资决策准确性
- **SSL邮件**：解决yeah.net SMTP超时问题
- **前复权处理**：数据源提供前复权价格
- **MA60计算**：pandas rolling函数，60日移动平均

## 🚀 快速使用
```bash
# 启动（Windows/Linux）
run.bat 或 ./run.sh

# 单次测试
python main.py --once

# 功能测试
python test_basic.py
```

## 🔧 核心配置
```yaml
stocks: [601728, 600938]
email:
  smtp_server: smtp.yeah.net
  smtp_port: 465
  sender_email: your_email@example.com
  sender_password: "your_email_password_or_app_specific_password"
llm:
  api_key: "your_deepseek_api_key_here"  # 可选
scheduler:
  run_time: "15:30"
```

## 📈 输出示例
**邮件内容**：
- 满足条件股票：代码、最低价、MA60、价差、跌幅%
- 所有监控股票：开盘、收盘、最高、最低、MA60、状态
- HTML表格格式

**日志系统**：
- 系统日志：`logs/quant_system.log`
- 测试日志：`logs/test.log`

## ✅ 已验证
- ✅ 真实历史数据获取
- ✅ 条件检查准确
- ✅ 邮件发送正常
- ✅ 定时任务稳定
- ✅ LLM分析可选

**状态**：稳定运行，真实数据，邮件正常。

**更新**：2026-03-05