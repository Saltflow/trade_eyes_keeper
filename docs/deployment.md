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

填写完整监控列表和各市场策略合同，启用需要的通知与管理渠道。Numba 是回测执行的必要依赖；启用飞书交互需安装清单中的 `lark-oapi`。PDF 日报另需 `xelatex`、CJK 字体和相应 TeX 包。内存与磁盘按全量标的、历史范围和搜索 worker 数量配置。

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

## 飞书研究助手部署

助手继续使用现有飞书长连接和 `trade-eyes.service`，无需新增监听端口。
新功能默认关闭，先准备专用 Linux Docker 运行镜像，再启用助手。
Docker 构建上下文只包含以下专用目录，不能把整个仓库或 `.env` 发送给构建服务：

```bash
docker build -t trade-eyes-research:local docker/feishu-research
python scripts/check_feishu_research.py
```

项目接口可先通过 `python scripts/check_feishu_research.py --project-only` 检查。
如果部署主机下载依赖不稳定，可使用构建辅助脚本的 `--wheelhouse` 选项：
依赖目录须带逐文件 SHA-256 清单，并使用官方 PyPI 分发文件。远端会再次核验，
然后在专用镜像中以 `--no-index` 安装；不会安装到宿主 Python 环境。

`check_feishu_research.py` 只读检查 Docker daemon、Linux 容器、专用镜像及资源上限。
默认任务使用 2 CPU、4096 MiB 内存；小服务器必须显式调整配置，例如：

```yaml
interactive:
  feishu:
    assistant:
      enabled: true
      model: deepseek-flash
      workspace_root: data/feishu_research
      docker_image: trade-eyes-research:local
      cpus: 1
      memory_mb: 768
      prepare_memory_mb: 768
      prepare_min_available_mb: 128
      timeout_seconds: 3600
      prepare_timeout_seconds: 3600
      max_output_mb: 100
```

内存上限不代表任务必然能够完成；超限或超时会保留失败证据并停止容器。
主机补数目前要求 Linux：每 100 毫秒监控本次进程组的 RSS 与交换内存总量，以及
主机 `MemAvailable`。超过 `prepare_memory_mb`（留空时使用 `memory_mb`）、
低于 `prepare_min_available_mb` 或无法读取监控数据时，停止所属进程组并保留资源
回执。此项是主机上的监控与主动中止；Docker 仍使用独立的 cgroup 硬限制。
内存较小的部署主机应串行执行验收和补数；非 Linux 主机准备阶段暂不开放。
默认单任务串行执行，主机负责通过现有供应商补齐真实数据，Docker 内禁网、非 root、
只读代码/配置/数据，只向本次任务输出目录写文件。Numba 和绘图库缓存使用容器临时目录。
生成 Python 在第二个离线容器运行，可信基准结果作为只读输入，附加研究输出单独归档。

本轮功能可使用 `scripts/deploy_feishu_assistant.py` 做按文件部署。
先用 `--check --file <路径>`（可重复）和 `--manifest <本地JSON路径>` 固定文件清单与
本地/远端哈希，再用 `--apply --manifest <同一路径>` 应用该清单。
如工作区包含其他尚未部署的修改，可用 `--source-root <工作区内候选目录>` 指定经过
审查的候选树。清单绑定该目录与文件哈希，应用时不允许换来源或回退到主工作区文件。
辅助脚本仅允许助手相关文件，核对远端身份、源码及私有配置未变化，逐文件备份并原子替换，
只合并助手配置节；不执行 Git 推送、reset、清理共享镜像或覆盖 `.env`。
镜像及 DeepSeek 模型预检不通过时不启用助手。该脚本不负责重启服务。

更新完成且相关测试通过后重启 `trade-eyes.service`，使用 `main.py --status`
核对新 MainPID 与新鲜心跳。助手启动即恢复任务记录、终止其遗留容器；中断任务不会自动重跑。
最后通过真实飞书私聊或 @ 消息验证问答、提案、确认、取消和结果附件。
文件上传需具有 `im:resource` 权限；只有单元测试或 Docker 预检通过，不能称为飞书账号链路已验收。

回退时先停止服务，将显式部署备份恢复到对应文件，并将助手 `enabled` 置为 `false`，
再启动服务验证原有命令。保留 `data/runtime/feishu_assistant.sqlite3` 和研究目录以便核对已确认操作。

### 项目文档与会话记录升级

助手从部署主机本身读取 `knowledge.py` 白名单内的文档。更新助手源码时，应核对
文档对应的业务代码版本；不要将工作区尚未发布的其他设计文档一起覆盖上线。
`docs/guide/announcements.md` 记录公告筛选与时效合同及核对版本。

升级前检查没有正在确认或运行的任务，并通过 SQLite backup API 备份运行中的
`data/runtime/feishu_assistant.sqlite3`，不能只复制可能仍有 WAL 写入的主文件。
新版本在启动时新增 `conversation_turns` 和 `conversation_events`，保留原提案、
任务及收据。问答留档默认30天，管理员可配置 `conversation_retention_days`（1–365）；
服务启动和新消息进入时清理到期会话。正文、工具参数/结果先脱敏再入库，通用日志
仅记录排查编号和状态。应限制数据库及备份的读取权限。

验证要检查一次真实模型问答的文档引用与持久记录，再核对飞书客户端实际收件。
API重放或mock发送成功不等同于客户端送达。排查时以服务日志 `trace` 对应
`conversation_turns.id`，联查 `conversation_events.turn_id`；旧版本漏记的正文
不会因升级自动补回。详见[使用与排查说明](guide/feishu_telegram_setup.md#项目资料与问题排查)。

### Pi Agent 核心依赖

Pi 是可选后端，需要 Node >=22.19。依赖固定在 `tools/feishu-pi/package-lock.json`，
与 Python 服务环境分离；不安装全局 CLI，不加载用户的 pi 配置或插件。

```bash
npm ci --prefix tools/feishu-pi --ignore-scripts --no-audit --no-fund
node tools/feishu-pi/bridge.mjs --check
node --test tools/feishu-pi/bridge.test.mjs
```

预检应返回 `type:ready`、`backend:pi`、`version:0.87.1`、`builtin_tools:[]`。
然后只调整 `interactive.feishu.assistant.harness_backend: pi`；仓库调用链查询建议
将 `max_tool_rounds` 设为12，`max_tool_calls` 保持32。确认模型、密钥、白名单和
其它配置未变后重启服务。必须实测 Python→Pi→模型→仓库工具→回答的完整往返，
不能只凭npm安装成功认定接入完成。

桥接环境不继承密钥、HOME、NODE_OPTIONS 或插件配置。模型 HTTP 留在 Python，
仍使用原 DeepSeek API 与非思考模式；Node 只通过固定 JSONL 协议请求已注册能力。
停止或超时清理该轮子进程。回退可将后端显式设为 `native` 并重启；不会自动重跑
失败或结果不确定的问答。dsh 未安装到 Python 环境，避免其 Pydantic 2 依赖与本项目
Pydantic 1 冲突。接入决策与验收见 `docs/development/feishu_repository_harness_20260928.md`。
# 业务数据快照与文档目录

本地业务数据可使用 `scripts/push_dataset_snapshot.py` 的清单与校验流程传入服务器独立 `data/dataset_imports/` 快照；不覆盖原数据或运行配置。代码发布后运行 `python scripts/build_dataset_catalog.py`，核对扫描完整性及导入清单缺口，再启用机器人跨数据集查询。原文补取使用 `scripts/backfill_dataset_documents.py` 的预览与有界 `--execute`，结果及失败原因保存在目录中。操作方式与数据格式见 [统一数据集目录](guide/dataset_catalog.md)。

SSH 链路较慢时，可使用已安装的 Paramiko 运行 `scripts/push_dataset_snapshot_sftp.py --snapshot-dir <冻结快照目录>`，默认 4 路、最多 `--concurrency 8`。只在远端已初始化同一 manifest 时加 `--resume-existing`；须先退出旧上传进程，不并行启动两个上传器。该入口保留原主机密钥校验、协调锁、partial 前缀、完整块及物化文件校验，不需要开放新端口。

索引构建后运行 `python scripts/export_dataset_catalog.py`，将同一只读 SQLite 快照完整导出到 `data/dataset_catalog/README.md` 及配套 JSON/JSONL；数据集数超过 100 也不会截断。文件有共同的导出时间和索引版本；源数据变化后应重新构建并导出。
