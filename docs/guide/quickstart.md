# 快速开始

5 分钟内启动并运行股票量化系统。

---

## 1. 环境准备

```bash
# 验证 Python 版本
python --version   # 需要 3.8+

# 安装依赖
pip install -r requirements.txt
```

> **首次部署到服务器？** → [部署五步走](setup.md)

## 2. 配置系统

```bash
# 复制环境变量模板
cp config/.env.example config/.env
# 编辑 config/.env 填入邮箱和 API Key

# （可选）编辑股票列表
# config/config.yaml -> stocks
```

最小可运行配置示例：

```yaml
# config/config.yaml
stocks:
  - 601728
email:
  smtp_server: smtp.yeah.net
  smtp_port: 465
  enable_ssl: true
scheduler:
  run_time: "19:00"
  timezone: Asia/Shanghai
```

```bash
# config/.env
EMAIL_SENDER=your_email@yeah.net
EMAIL_PASSWORD=your_authorization_code
EMAIL_RECEIVER=your_email@yeah.net
```

## 3. 运行测试

```bash
# 单次收盘日报 (含 PDF 附件)
python main.py --once

# 单次早盘简报
python main.py --brief
python main.py --brief afternoon_snapshot  # 收盘简报

# 策略搜索优化 (全量搜索, 15-30 min)
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

系统将在每天 19:00（Asia/Shanghai）自动执行日报。

## 5. 查看结果

- **日志**：`logs/quant_system.log`
- **健康面板**：浏览器访问 `http://localhost:1933`
- **邮件存档**：`data/email_archive/`

---

**下一步**：
- 深入了解配置选项：[配置说明](../configuration.md)
- 生产环境部署：[部署指南](../deployment.md)
