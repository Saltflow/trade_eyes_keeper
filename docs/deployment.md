# 部署指南

本文档涵盖股票量化系统的生产环境部署、CI/CD 流程及日常运维。

---

## 环境要求

| 项目 | 最低要求 | 推荐 |
|------|---------|------|
| Python | 3.8+ | 3.11+ |
| 内存 | 512 MB | 1 GB |
| 磁盘 | 100 MB | 1 GB（含缓存、历史数据与 texlive） |
| 网络 | 公网访问 | 稳定公网（用于抓取与邮件） |
| 操作系统 | Windows / Linux / macOS | Linux (Ubuntu 22.04 LTS) |
| 系统依赖 | — | `texlive-xetex` `poppler-utils` (PDF 日报) |

---

## 快速部署

### 1. 克隆与安装

```bash
git clone <repo-url> stock-quant
cd stock-quant
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp config/.env.example config/.env
# 编辑 config/.env，填入邮箱、API Key 等敏感信息
```

必需变量：
- `EMAIL_SENDER` / `EMAIL_PASSWORD` / `EMAIL_RECEIVER`
- `DEEPSEEK_API_KEY`（如需 LLM 分析）
- `TUSHARE_TOKEN`（如使用 tushare）

### 3. 验证配置

```bash
python main.py --once
```

检查 `logs/quant_system.log` 确认无报错，邮件正常收到即可。

---

## 生产部署模式

### 方式一：systemd 服务（Linux 推荐）

创建 `/etc/systemd/system/stock-quant.service`：

```ini
[Unit]
Description=Stock Quantitative System
After=network.target

[Service]
Type=simple
User=quant
WorkingDirectory=/opt/stock-quant
Environment="PYTHONUTF8=1"
Environment="PYTHONIOENCODING=utf-8"
ExecStart=/opt/stock-quant/.venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启用：
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stock-quant
sudo journalctl -u stock-quant -f
```

### 方式二：Cron + 单次模式

```bash
# 每天 19:05 执行（A股/港股收盘后数据完整）
5 19 * * * cd /opt/stock-quant && .venv/bin/python main.py --once >> logs/cron.log 2>&1
```

---

## CI/CD 自动化部署

系统内置 `ci_cd_deploy.py`，支持 SSH 密钥或密码方式推送至远程服务器。

### 配置

```bash
export DEPLOY_SSH_KEY_PATH="/path/to/private/key"
# 或
export DEPLOY_PASSWORD="your_server_password"
```

### 部署命令

```bash
#  dry-run 预览变更
python ci_cd_deploy.py --dry-run

# 正式部署
python ci_cd_deploy.py

# 自定义 SSH 端口
python ci_cd_deploy.py --ssh-port 2222

# 排查远程健康状态
python ci_cd_deploy.py --investigate
```

部署脚本会自动完成：代码更新、依赖安装、systemd/crontab 配置、健康检查验证，并发送部署通知邮件。

---

## 健康检查服务器

系统内置 HTTP 健康服务器（默认端口 1933），随调度器自动启动。

| 端点 | 说明 |
|------|------|
| `GET /` | HTML 状态面板 |
| `GET /status` | JSON 格式系统状态 |
| `GET /health` | 健康探针（200 OK） |
| `GET /metrics` | Prometheus 格式指标 |
| `GET /test-email?force=true` | 触发真实邮件测试 |
| `GET /report/<token>` | 策略优化 HTML 报告 (30 分钟时效) |

**SSL/TLS**: 系统自动生成自签名证书 (365 天有效期), 端口 1933 HTTPS 监听。
设置 `config.health_server.ssl: true` 和 `public_ip` 启用。

单独启动：
```bash
python main.py --health-server
```

---

## 日志与监控

### 日志文件

| 路径 | 用途 |
|------|------|
| `logs/quant_system.log` | 主程序运行日志 |
| `logs/management_audit.log` | 健康服务器管理审计 |
| `docs/development/audit_log.md` | 编码审计与循环检测记录 |

### 日常巡检清单

1. 数据源可用性：访问 `/health` 确认状态正常
2. 邮件送达率：检查收件箱或邮件存档目录 `data/email_archive/`
3. 缓存健康：确认 `cache/data/` 与 `cache/analysis/` 无异常膨胀
4. 敏感信息泄漏：定期 `git log --all --full-history -S 'password'` 排查历史

---

## 安全建议

1. **密钥隔离**：所有密码、API Key 仅保存在 `config/.env`，已加入 `.gitignore`
2. **邮件存档**：`data/email_archive/` 包含真实邮箱地址，注意本地磁盘加密
3. **SSH 优先**：CI/CD 优先使用 SSH 密钥，避免密码硬编码
4. **端口防火墙**：仅开放必要端口（如 1933 健康检查端口）
5. **定期轮换**：API 密钥与邮箱授权码建议每 90 天轮换

---

## 故障排查

| 现象 | 排查步骤 |
|------|---------|
| 数据获取失败 | 检查网络 → 删除 `cache/` 强制刷新 → 查看 `web_crawler` 降级日志 |
| 邮件发送失败 | 确认 SMTP 服务器/端口/授权码 → 检查防火墙 → 开启 DEBUG 日志 |
| 编码错误 | 确保 `PYTHONUTF8=1` 已设置，参考 `docs/reports/encoding_issues.md` |
| Session 创建失败 | 查看 `logs/quant_system.log` 中 Pydantic 验证错误，检查 config.yaml 格式 |

---

**相关文档**：
- [快速开始](guide/quickstart.md)
- [配置说明](configuration.md)
- [架构说明](architecture.md)
