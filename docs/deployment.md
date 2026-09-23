# 部署与运行

服务入口为 `python main.py --service`（省略参数等价）。它管理唯一调度器、Telegram 轮询和飞书应用长连接，服务状态写入本地文件。

HTTP/HTTPS health server、Web 管理页、OTP 和报告临时链接已整体下线。使用飞书/TG Bot 更灵活、更安全，无需开放管理端口；原来的 1933 端口不再使用。[Bot 配置](guide/feishu_telegram_setup.md)与[配置参考](configuration.md)分别说明凭证和开关。

## 安装和检查

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config/config.yaml.example config/config.yaml
cp config/.env.example config/.env
```

填写完整监控列表和各市场策略合同，启用需要的通知与管理渠道。回测依赖沿用现有环境；启用飞书交互需安装清单中的 `lark-oapi`。PDF 日报另需 `xelatex`、CJK 字体和相应 TeX 包。内存与磁盘按全量标的、历史范围和搜索 worker 数量配置。

```bash
python main.py --service       # 调度 + 已启用的管理 Bot
python main.py --interactive   # 仅运行已启用的 Bot
python main.py --status        # JSON 状态；运行且心跳新鲜返回 0，否则返回 1
```

单次日报和简报仍使用 `--once` / `--brief [id]`。验证业务数据时可设置 `SKIP_NOTIFICATIONS=true`，这样仍生成报告但不外发。策略绩效验收必须使用主配置全部标的，运行完整 `--optimize`，不能用少量标的代替。

## Linux 常驻服务

项目的部署脚本维护 `/etc/systemd/system/trade-eyes.service`。手工部署示例（路径按实际目录修改）：

```ini
[Unit]
Description=Trade Eyes Keeper scheduler and outbound Bots
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/trade_eyes_keeper
ExecStart=/opt/trade_eyes_keeper/.venv/bin/python /opt/trade_eyes_keeper/main.py --service
Environment=PYTHONUTF8=1
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trade-eyes.service
sudo systemctl status trade-eyes.service
sudo journalctl -u trade-eyes.service -f
```

同一数据目录只允许一个服务实例。调度任务仍以独立子进程执行；systemd 停止服务时会清理进程组。心跳证明服务进程/接收线程存活，不证明行情、SMTP 或 Bot 账号的端到端可用性。

## 现有服务器迁移

部署脚本会停止并禁用旧 `trade-eyes-health.service`、清理旧保活进程，安装并启动 `trade-eyes.service`。不会再启动 HTTP 探针或自签名证书逻辑。

旧 cron 只迁移属于当前项目的 `main.py --once`、`--brief`、`--optimize` 行，保留其他项目任务。日报和简报以 YAML 为准。若 YAML 没有声明 `optimize_enabled`，但存在每天固定时间的旧优化 cron，会将其时间与启用状态写入 YAML 后移除该 cron；显式的 `false` 会被尊重。无法表达为单个每日时间的旧优化 cron 会使迁移报错，需先在 YAML 明确调度意图。

新配置模板将定时优化设为 `false`，避免服务启动隐式执行全量搜索。需要例行优化时明确设置 `optimize_enabled: true` 与 `optimize_time`。不要再为这些任务额外创建 cron。

## CI/CD

部署凭证使用 `.env.example` 中的 `DEPLOY_HOST`、`DEPLOY_SSH_USER`、`DEPLOY_SSH_KEY`、`DEPLOY_SSH_REMOTE`、`DEPLOY_REMOTE_DIR`。

```bash
python ci_cd_deploy.py --dry-run
python ci_cd_deploy.py
python ci_cd_deploy.py --mode investigate
```

实际部署会推送代码、保留远端私有配置、安装依赖、校验完整优化配置、运行禁止外发的系统检查、迁移调度并启动服务。验收使用 systemd 状态及当前 PID 的新鲜本地心跳，成功后按统一通知开关发送部署通知。

`--sync-config` 明确用本地主配置替换远端配置；`--sync-env` 明确同步凭证文件。默认两项均关闭。`--dry-run` 只预览，不构成远端验收。

## 运维入口

| 目标 | 入口 |
| --- | --- |
| 服务进程及退出原因 | `systemctl status trade-eyes.service` / `journalctl -u trade-eyes.service` |
| 本地运行证据 | `python main.py --status`、`data/runtime/service_status.json`（随 `storage.data_dir` 改变） |
| 日报与搜索日志 | `logs/quant_system.log` |
| 已生成邮件 | `data/email_archive/` |
| 标的与调度管理 | Bot `/help`、`/list`、`/add`、`/remove`、`/schedule` |

Bot 开关、凭证或白名单变更后重启服务。`/schedule` 会原子保存并立即重排当前服务任务。已下线的页面和公网报告地址不再作为检查依据。

## 机器人服务发布验收

普通服务启动不主动发送上线通知；需要时显式使用 `--notify-start`。
迁移会 mask 旧 `trade-eyes-health.service`，阻止旧单元再次启动。
发布验收必须同时确认旧进程已退出、旧管理端口不再监听、新 systemd MainPID 与
新鲜心跳一致，并验证已启用机器人各自的实际平台连接。心跳不能代替账号连接证据。
同机其他项目的 Nginx/HTTP 服务不属于本项目退休范围。
