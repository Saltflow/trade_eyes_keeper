# 快速开始

5 分钟内启动并运行股票量化系统。

---

## 1. 环境准备

```bash
# 验证 Python 版本
python --version   # 需要 3.10+（与 pyproject.toml 一致）

# 安装依赖
pip install -r requirements.txt
```

> **首次部署到服务器？** → [部署五步走](setup.md)

## 2. 配置系统

```bash
# 复制环境变量模板
cp config/.env.example config/.env
# 编辑 config/.env 填入邮箱和 API Key

# 复制主配置模板，再填写完整监控列表和各市场设置
cp config/config.yaml.example config/config.yaml
# config/config.yaml -> stocks / optimizer.markets
```

主配置请以 `config/config.yaml.example` 为模板，字段含义见[配置参考](../configuration.md)。股票代码写成字符串以保留前导零；执行日报、信号扫描或优化验收时使用配置的完整标的，不用局部示例替代。

```bash
# config/.env
EMAIL_SENDER=your_email@yeah.net
EMAIL_PASSWORD=your_authorization_code
EMAIL_RECEIVER=your_email@yeah.net
```

## 3. 运行与验证

```bash
# 单次收盘日报 (含 PDF 附件)
python main.py --once

# 单次早盘简报
python main.py --brief
python main.py --brief afternoon_snapshot  # 收盘简报

# 策略搜索优化（完整配置；耗时由预算、数据和机器决定）
python main.py --optimize

# 运行验证测试
pytest tests/validation/ -v
```

## 4. 启动定时任务

```bash
# Windows
scripts\run.bat

# Linux / Mac
chmod +x scripts/run.sh
./scripts/run.sh
```

日报默认 19:00，工作日简报默认 09:50 / 14:30（Asia/Shanghai）；以主配置及实际调度注册为准。`main.py --service` 装配唯一调度器、Bot 与公共执行器。

## 5. 查看结果

- **日志**：`logs/quant_system.log`
- **服务状态**：`python main.py --status`
- **管理命令**：飞书长连接 / Telegram Bot，见 [配置指南](feishu_telegram_setup.md)
- **邮件存档**：`data/email_archive/`

---

**下一步**：
- 深入了解配置选项：[配置说明](../configuration.md)
- 生产环境部署：[部署指南](../deployment.md)
- 架构图与其他指南：[文档索引](../README.md)
