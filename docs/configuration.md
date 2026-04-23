# 配置完整使用文档

本文档详解 `config/` 目录下所有配置文件及其字段含义。

---

## 文件总览

| 文件 | 用途 | 是否含敏感信息 |
|------|------|--------------|
| `config.yaml` | 主配置（股票、数据源、邮件、调度器） | 否 |
| `alerts.yaml` | 技术指标锚点与警报规则 | 否 |
| `.env` | 环境变量（API Key、邮箱密码） | **是** |
| `.env.example` | `.env` 模板，不含真实密钥 | 否 |

> `.env` 已加入 `.gitignore`，**切勿提交到仓库**。

---

## config.yaml

### stocks

监控的股票代码列表，支持 A 股、ETF、美股、港股、新加坡股。

```yaml
stocks:
  - 601728   # A股：中国电信（上海6开头，深圳0/3开头）
  - 512810   # ETF：华宝中证军工ETF
  - GOOG     # 美股
  - 00883    # 港股
  - C38U.SI  # 新加坡股
```

### data_source

```yaml
data_source:
  type: web_crawler           # 主数据源类型
  primary: web_crawler        # 主数据源
  secondary: baostock         # 备用数据源
  etf_force_web_crawler: true # ETF 强制使用 web_crawler（除权支持更好）
  fallback_to_web_crawler: true
```

### baostock

回测与历史数据专用数据源配置。

```yaml
baostock:
  enable: true
  adjustflag: "2"           # 复权类型：2=前复权，3=后复权
  login_retry_times: 3
  timeout_seconds: 30
```

### historical_cache

```yaml
historical_cache:
  enable: true
  historical_cache_days: 30
  force_update_interval_days: 30
  check_dividend_interval_days: 7
  historical_lookback_days: 730   # 默认回溯 2 年
  enable_historical_cache: true
```

### announcements

官方公告抓取配置。

```yaml
announcements:
  enable: true                    # 总开关
  include_in_email: true          # 邮件中显示公告
  days: 7                         # 获取最近 N 天公告
  dividend_days: 420              # 股息公告回溯天数
  enable_content_fetching: true   # 抓取公告正文
  enable_llm_extraction: true     # 使用 LLM 提取关键信息
  max_llm_calls_per_run: 30       # 每轮 LLM 调用上限
  max_pdf_size_mb: 10             # 公告 PDF 大小限制
```

### financial_reports

财报分析配置。

```yaml
financial_reports:
  enable: false                   # 总开关
  auto_enable: true               # 条件满足时自动启用
  conditional_enable: true
  force_analyze: false            # 强制分析所有股票（忽略触发条件）
  max_stocks_per_run: 2           # 常规模式每轮最多分析数
  max_force_stocks: 2             # 强制模式上限（0 或缺省则不限）
  max_llm_calls_per_run: 80       # 财报分析 LLM 调用上限（独立额度）
  reports_per_stock: 3
  conditional_days: 30
```

### backtest

```yaml
backtest:
  enable: true
  run_in_daily_task: true
  cache_days: 7
```

### email

SMTP 邮件发送配置。敏感字段（sender_email / sender_password / receiver_email）建议通过 `.env` 覆盖。

```yaml
email:
  smtp_server: smtp.yeah.net
  smtp_port: 465
  enable_ssl: true
  enable_tls: false
```

> yeah.net 使用 SSL 端口 465；QQ/163 可尝试 587 或 465，视服务商文档而定。

### llm

```yaml
llm:
  api_type: deepseek
  base_url: https://api.deepseek.com/v1
  model: deepseek-chat
  enable_fundamental_analysis: false   # 是否启用基本面分析
```

### scheduler

```yaml
scheduler:
  run_time: "16:00"             # 每日运行时间（24小时制）
  cache_bypass_cutoff: "15:55"  # 超过此时间且缓存非当日则强制刷新
  timezone: Asia/Shanghai
```

### health_server

```yaml
health_server:
  enabled: true
  host: 0.0.0.0
  port: 1933
```

### alerts（引用）

```yaml
alerts:
  config_path: "./config/alerts.yaml"
  enabled: true
```

### storage

```yaml
storage:
  data_dir: ./data
  cache_dir: ./cache
  cache_days: 7
  csv_format: true
```

### logging

```yaml
logging:
  level: DEBUG         # DEBUG / INFO / WARNING / ERROR
  file: ./logs/quant_system.log
  format: '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
```

---

## alerts.yaml

技术指标锚点与分层警报规则。

```yaml
version: 1.1
anchors:
  - name: ma60
    type: daily_ma
    window: 60
  - name: wma20
    type: weekly_ma
    window: 20
thresholds: [-10, -5, 0, 5, 10, 15]
boundary_rules:
  neg_right_closed: true
  pos_left_closed: true
  exclude_zero: true
  skip_zero_five: true
consecutive_days_threshold: 5   # 连续 N 天后不再重复发送相同区间警报
auto_reset: true                # 股票移动到新区间时自动重置旧状态
```

| 字段 | 说明 |
|------|------|
| `anchors` | 技术指标锚点列表。新增指标只需添加配置，`technical_indicators.py` 自动计算 |
| `type` | `daily_ma`（日线 MA）或 `weekly_ma`（周线 MA） |
| `window` | 计算窗口 |
| `thresholds` | 价格偏离锚点的百分比阈值，用于分层警报 |
| `boundary_rules` | 区间边界开闭规则 |
| `consecutive_days_threshold` | 抑制重复通知的连续天数 |

---

## .env

```bash
# 邮箱配置（必需）
EMAIL_SENDER=your_email@example.com
EMAIL_PASSWORD=your_email_password_or_app_specific_password
EMAIL_RECEIVER=receiver_email@example.com

# DeepSeek API 配置（可选）
DEEPSEEK_API_KEY=your_deepseek_api_key_here

# Tushare Token（可选）
TUSHARE_TOKEN=your_tushare_token_here

# 日志级别
LOG_LEVEL=INFO
```

### 配置优先级

1. 环境变量（最高）
2. `.env` 文件中的变量
3. `config.yaml` 中的默认值

---

**相关文档**：
- [部署指南](deployment.md)
- [架构说明](architecture.md)
- [快速开始](guide/quickstart.md)
