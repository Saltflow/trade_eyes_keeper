# 配置参考

当前运行配置由 `ConfigStore` 统一读取。所有 Bot 修改先在跨进程锁内重读原始 YAML，再按字段更新并原子替换；运行态的环境密钥不会写回文件。

## 文件与职责

| 文件 | 用途 |
| --- | --- |
| `config/config.yaml` | 标的、数据采集、调度、通知、Bot、各市场策略与 profile 选择；本地文件，不进入 Git |
| `config/config.yaml.example` | 主配置模板 |
| `config/.env` / `.env.example` | SMTP、LLM、Bot 和部署凭证；只提交模板 |
| `config/alerts.yaml` | 指标锚点、阈值区间、重复告警规则 |
| `config/optimizer_constraints.yaml` | Solver 预算、执行费用、窗口、基准、Gate、稳健性和研究 profile |
| `config/promotion_policy.yaml` | 候选相对活动策略的晋升要求 |
| `pyproject.toml` / `pytest.ini` | Python 包、Ruff、测试设置，不属于交易运行配置 |

数据源按代码内的 provider 合同降级；旧 `data_source.primary/secondary`、顶层 `baostock`、`historical_cache`、`portfolio_strategy` 已退休。PIT 行情仍使用 `point_in_time_data.market_history`，其中 Baostock 超时配置继续有效。

## 主配置

股票代码必须写成字符串，避免 YAML 吃掉前导零：

```yaml
stocks: ["601728", "000001", "00883", "VOO"]
skip_search: []       # 保留监控，但不参加搜参
skip_signals: []      # 保留监控，但不显示策略信号
alerts:
  config_path: ./config/alerts.yaml
  enabled: true
```

指标计算器与告警检查器读取同一指定规则文件。自定义路径不存在或内容损坏时会报错，不会静默使用默认锚点；不再使用跨路径的全局规则单例。

| 配置段 | 有效配置与作用 |
| --- | --- |
| `report` | 报告展示模式、Ke 曲线读取与公告展示期限；见下节 |
| `announcements` | `enable`、`days`、`dividend_days`、正文/PDF提取、LLM 调用额度；详见模板 |
| `financial_reports` | `max_llm_calls_per_run` 仍供 LLM 额度使用；旧财报开关和股票数量限制已删除 |
| `sina_options` | `enabled`、`timeout_seconds`、`quote_batch_size`，供新浪期权数据源使用 |
| `point_in_time_data` | 原价/复权价、公司行动、披露日财报的目录、历史覆盖和 provider 设置 |
| `instrument_audit` / `instrument_catalog` | 标的画像输出及按代码指定的官方发行人/持仓资料 |
| `email` | SMTP 地址、端口和 SSL/TLS；敏感字段建议放 `.env` |
| `llm` | API 类型、URL、模型及基本面分析开关 |
| `storage` | 数据目录、缓存目录与保存设置；服务状态写入 `data_dir/runtime/` |
| `logging` | `level`、`file`、`format`；原 `LOG_LEVEL` 环境模板项已删除 |

### 报告展示

```yaml
report:
  technical_signals:
    enabled: false
  strategy_display: returns_only
  return_curve_artifact: data/strategy_returns/ke_24m.json
  return_curve_max_age_days: 7
  events:
    announcement_lookback_months: 2
```

`returns_only` 使用现有行情、基本面、事件和已完成的 Ke 历史模拟曲线；不回退旧技术扫描结果。日报与简报保留今开、昨收、现价、PE、PB、隐含 Ke 和 ROE，不展示股息率。ROE 优先使用已有财务值；缺失时仅在渲染层按有效 PB/PE×100 粗估并标记 `(估)`，无效输入显示 `—`，不写回财务数据。

`announcement_lookback_months` 接受整数 **1–24**，默认 **2**；布尔值、字符串和小数不接受。以报告的上海日历日期减去指定自然月数，包含截止日，按实际发布日期筛选；日期不明保留并标注“日期待确认”。待派息分红、未解禁事项独立保留。该设置只过滤报表展示，不删除原始记录，不改变 `announcements.days/dividend_days` 的采集窗口；实际可展示历史取决于已采集数据。

机器人可查询该字段，或提出“把日报公告展示期限改成3个自然月”，查看预览后发送 `确认 <编号>` 保存。沿用现有配置流程，下次报告读取生效，已开始的任务仍使用原快照。

### 统一调度

`python main.py` 与 `python main.py --service` 使用同一 `ScheduleManager`；任务调用现有 CLI 子进程。时间创建和在线修改都使用 `scheduler.timezone`。

2026-11-03 生产核验使用触发器发起报告，固定日报、启动日报和固定简报均关闭。以下示例保留兼容时间字段；关闭的任务不会注册：

```yaml
scheduler:
  timezone: Asia/Shanghai
  daily_enabled: false
  run_time: '19:00'
  run_on_startup: false
  optimize_enabled: false
  optimize_time: '02:00'
  daily_report_frequency: daily   # daily / weekly / off
  daily_report_weekday: 4         # 0=周一，4=周五
  cache_bypass_cutoff: '15:55'
  daily_misfire_grace_seconds: 3600
  brief_misfire_grace_seconds: 900
  optimize_misfire_grace_seconds: 7200
  brief_reports:
    - id: morning_snapshot
      run_time: '09:50'
      enabled: false
      skip_weekends: true
      label: 早盘简报
    - id: afternoon_snapshot
      run_time: '14:30'
      enabled: false
      skip_weekends: true
      label: 午后简报
```

`daily_enabled` 控制固定日报任务是否注册，`brief_reports[].enabled` 控制各固定简报；`daily_report_frequency: off` 不能代替关闭任务注册。显式开启后仍支持上述固定时间，不能把示例时间当作生产正在执行的计划。`daily_report_frequency` 控制普通日报的发送频次，告警和手动 `/daily` 的语义保持独立。定时优化缺省关闭，手动 `--optimize` 不受该开关影响。已有服务器的旧优化 cron 会在部署时迁移，见[部署指南](deployment.md)。

### 受管触发器

触发器判断何时运行报告，复用既有 `reports.daily` / `reports.brief` 任务及正文构建链路，仍按报告配置处理完整标的池。动作类型只有 `daily`、`brief` 和预留的 `optimize`；前两种可执行，`optimize` 当前拒绝执行，没有 `reminder` 报告类型。个人持仓提醒是独立业务。

触发后的报告按授权路由、渠道开关和报告参数投递，邮件与 Bot 分别记录结果；条件命中不等于各渠道已发送成功。实现见[触发动作合同](../src/triggers/models.py)和[报告任务入口](../src/core/report_execution.py)。

### 通知与管理 Bot

```yaml
notification:
  email: {enabled: true}
  feishu: {enabled: false, msg_type: interactive}
  telegram: {enabled: false, parse_mode: HTML}
interactive:
  feishu:
    enabled: false
    allowed_chat_ids: []
    rate_limit_per_minute: 10
  telegram:
    enabled: false
    allowed_chat_ids: []
    rate_limit_per_minute: 10
    polling_interval: 2
```

`notification` 管理报告外发，`interactive` 管理命令接收，两者分别启用。飞书使用应用长连接，Telegram 使用轮询。飞书可显式设置 `["*"]` 沿用当前群成员范围；Telegram 需要明确聊天 ID。启用 Bot 时，空白名单或缺失凭证会阻止启动。

HTTP/HTTPS health server、管理页面、OTP 和报告临时链接已整体下线。使用飞书/TG Bot 更灵活、更安全，无需开放管理端口。配置方法见[Bot 指南](guide/feishu_telegram_setup.md)。

### 策略与执行合同

每个 `optimizer.markets.<market>` 必须显式声明 `strategy`、`solver_id`、`gate_profile`、`walk_forward_profile`、`execution_profile` 和 `benchmark_profile`。默认值和 profile 定义统一放在 `optimizer_constraints.yaml`，不再支持全局策略兜底。

搜索排名、逆序验证、留一验证均保留 raw OHLC、现金分红（含税前/税额）、拆股和基准执行数据。前复权价格用于信号；缺少执行数据或 Numba 依赖时明确失败，不能生成零资产伪结果。

主搜参资源配置使用 `search.workers`（可由 `SEARCH_WORKERS` 覆盖）和 `search.evaluation_backend`；`genetic_search.evaluation_workers` 保留给研究接口。无消费者的两项旧 mutation 参数和随机敏感性采样参数已删除，敏感性验证枚举确定性邻域。

## 环境变量和优先级

- 进程环境优先于同目录 `.env`；加载 `.env` 使用 `override=False`。
- `EMAIL_SENDER`、`EMAIL_PASSWORD`、`EMAIL_RECEIVER` 和 `DEEPSEEK_API_KEY` 的非空环境值覆盖相应 YAML 字段。无数据消费者的 `TUSHARE_TOKEN` 已从加载器和模板移除。
- Bot 和推送凭证采用非空 YAML 值优先、对应环境变量兜底。建议只在 `.env` 保存凭证。
- `SKIP_NOTIFICATIONS` 禁止所有通知及 Bot 回复；`SKIP_EMAIL`、`SKIP_FEISHU`、`SKIP_TELEGRAM` 禁止对应渠道外发。`true/1/yes/on` 均有效且忽略大小写。
- 外发跳过开关不禁止已授权管理命令执行；停用管理入口应设置 `interactive.<channel>.enabled: false` 并重启服务。

`config/.env.example` 列出凭证名称。日常修改用 Bot 命令或编辑 YAML；Bot `/schedule` 修改会立即更新当前调度器，其余服务级开关、凭证和白名单需要重启服务。
