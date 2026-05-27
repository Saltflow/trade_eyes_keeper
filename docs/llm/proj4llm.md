# 股票量化系统 - 关键设计决策文档

**文档版本**: v1.17 (简报增强 + 数据清理 + 调度调整)
**最后更新**: 2026-05-27
**压缩目标**: ~800行，保留关键设计决策

---

## 📋 执行摘要

本文档记录股票量化监控系统的关键设计决策，涵盖架构选择、问题解决方案和技术路线。系统核心能力：策略搜索优化器、信号扫描器、xelatex 日报 PDF 生成、邮件提醒、健康检查服务器。

### 核心原则
1. **真实数据优先** - 杜绝模拟数据，多源降级（新浪→腾讯→东方财富→Yahoo）
2. **配置驱动** - 技术指标/规则/简报通过 YAML 配置，避免硬编码
3. **文档驱动开发** - 所有设计决策存档至本文档
4. **防循环编码** - cycle_guard 自动检测重复错误模式
5. **Session 统一管理** - 全局 SessionContext 防止字段混淆
6. **全量验证** - 策略优化/信号扫描/回测分析基于 config 全量标的运行，禁止子集验证

---

## 🏗️ 架构现状 (v1.16)

```
┌─────────────────────────────────────────────────────────────────┐
│  配置层 (config/)                                               │
│  - config.yaml (股票/邮件/调度/简报)  - alerts.yaml (锚点)      │
│  - optimizer.yaml (策略模板/构建器)  - .env (API Key/密码)      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  缓存 + 数据源 (src/)                                           │
│  - data_source.py   (CSV 缓存 + 复权交叉验证 + 价格校验)        │
│  - web_crawler.py   (新浪/腾讯/东方财富 多源降级)               │
│  - data_fetcher.py  (协调获取 → 调用指标计算)                   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  指标计算 (src/)                                                │
│  - technical_indicators.py  (配置驱动 MA60/WMA/RSI/Bollinger)   │
│  - utils/etf_detector.py    (ETF 统一检测)                      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  策略 + 分析 (src/analysis/)                                    │
│  - strategy_optimizer.py   (贝叶斯优化 + 两阶段 + 收敛图)       │
│  - signal_scanner.py       (共识扫描 + 每日警报 + 回测)         │
│  - portfolio_strategy.py   (共享资金池 + 贪心搜索)              │
│  - rule_engine.py          (YAML 驱动 + 表达式沙箱)             │
│  - indicator_library.py    (RSI/MACD/ATR/布林/ADX/量比)         │
│  - backtest_config.py      (回测时间线约束)                     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  业务层 (src/)                                                  │
│  - condition_checker.py  - alert_engine/processor/state_manager │
│  - email_notifier.py     (告警/日报/简报 + xelatex PDF 生成)    │
│  - health_server/        (健康检查 + OTP + SSL + 报告链接)      │
│  - session_manager.py    (SessionContext Pydantic 模型)          │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  模板 + 输出                                                    │
│  - report_daily.tex         (xelatex LaTeX 日报模板)            │
│  - appendix_methodology.md  (13节公式附录, Markdown→LaTeX)      │
│  - email_template.html      (邮件正文, 港式财报卡片布局)        │
│  - optimizer_report.html    (交互收敛图, Plotly 暗色主题)       │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🏗️ 核心架构决策

### 1. Session 数据流 (v3.0)

**决策**: Pydantic `SessionContext` 替代裸 DataFrame/dict 传递
**背景**: `price`/`low_price` 字段名称不一致导致邮件显示价格为 0.00
**实现**: `fetch_to_session()` → `check_from_session()` → `send_from_session()`
**类型安全**: `StockPriceData` / `AlertStock` Pydantic 模型，自动验证数据完整性
**文件**: `src/session/session_manager.py` / `src/models/schemas.py`

### 2. DataSource 统一数据源 (v3.2 → v3.2.1)

**决策**: CSV 缓存 + meta 文件替代 JSON 缓存
**特性**: 7 天保留 / 15:55 当日过期 / 复权交叉验证（双源 3 日 close 比对）/ 价格关系校验
**影响**: `data_fetcher.py` 删 ~170 行缓存代码，`backtest_framework.py` 删内存缓存
**文件**: `src/data/data_source.py`

#### v3.2.1 修复：缓存 bypass + 复权检测回归 (2026-05-25)

**问题**: 重构 DataSource 时丢失了 `_should_bypass_cache`，导致 15:55 后非当日缓存不会被强制刷新，除权后的前复权历史数据持续错误。
**根因**: `fetch_stock_data()` 缓存命中路径直接 `return cached_df`，完全跳过了时间检查和复权检测。
**修复**:
  - 恢复 `_should_bypass_cache(cutoff="15:55", granularity=per-stock)`，按标的粒度生效
  - 缓存命中前增加 bypass 判断，触发后进入增量/全量拉取 → `_check_forward_adjustment` → 合并/覆盖
  - 统一 `requested_start_ts = pd.Timestamp(requested_start.date())`，消除时间分量导致的首行误删 bug
**测试**: `tests/test_data_source.py` 14 个用例覆盖 bypass 边界、复权修正检测、ETF 场景、fallback 路径

#### v3.2.2 数据清理：debt_ratio 删除 + ROE 计算修复 (2026-05-27)

**问题 1**: `debt_ratio`（资产负债率）从腾讯 API `items[52]` 获取，但该字段实为**动态 PE**；`items[53]` 实为**静态 PE**。原映射完全错误，且 debt_ratio 对投资决策无直接价值。
**决策**: 全链路删除 `debt_ratio`（schemas、data_fetcher、web_crawler、email_notifier、模板、LLM analyzers）。
**影响文件**: `src/models/schemas.py`, `src/core/data_fetcher.py`, `src/data/web_crawler.py`, `src/notification/email_notifier.py`, `src/templates/email_template.html`, `src/analysis/llm_analyzer/*`

**问题 2**: ROE 原从 `items[52]` 获取，映射错误后数据不可信。
**修复**: ROE 改为**推导计算**: `ROE = (PB / PE) × 100`，与财报披露值误差 <0.2%（以 600000 实测验证）。
**文件**: `src/core/data_fetcher.py`

#### v1.17 简报增强 (2026-05-27)

**排序**: `send_brief_report()` 按锚点偏离率**升序排列**，跌幅越大越靠前；无有效锚点的股票 `dev_pct=None` 用 `float("inf")` 兜底排最后。
**新增收盘简报**: `config.yaml` 新增 `afternoon_snapshot`（14:30），与 `morning_snapshot`（09:50）共用同一函数，仅标签不同。
**日报时间调整**: `scheduler.run_time` 从 `16:00` 改为 `19:00`，确保 A 股收盘后数据完整（港股 16:00 收盘，美股隔夜）。
**CI/CD 同步**: `ci_cd_deploy.py` 自动注册 3 条 cron：09:50 早盘 / 14:30 收盘 / 19:00 日报 / 02:00 优化。
**文件**: `src/notification/email_notifier.py`, `config/config.yaml`, `ci_cd_deploy.py`

### 3. 数据获取

**主源**: 新浪财经历史数据 API → 腾讯财经 → 东方财富（自动降级）
**股息**: LLM 提取缓存优先 → 网页爬虫备用
**缓存**: `cache/data/` CSV 格式，7 天保留期
**baostock**: 回测历史数据专用（前复权），日期格式 YYYY-MM-DD

---

## 🎯 策略搜索优化器 (v1.14)

### 架构

```
config/optimizer.yaml → StrategyOptimizer → PortfolioEvaluator
        5条规则定义         贝叶斯优化(skopt)      24月模拟回测
        6买+5卖构建器       13+N 维参数空间        BacktestConfig 约束
              │
        ┌─────┴──────┐
    Phase A (训练)   Phase B (测试)
    0-12月搜索        0-24月最终评估
                     按12-24月外样本排名
```

### 回测时间线 (`BacktestConfig`)

| 阶段 | 月 | 交易 | 资金注入 | 用途 |
|------|-----|------|---------|------|
| 观察 | 0-6 | 禁止 | 无 | 指标暖机 + pre-filter |
| 部署 | 6-12 | 自由 | A/非A各+20k/月 | 训练目标 |
| 延续 | 12-18 | 自由 | 无 | 外样本延续 |
| 持仓 | 18-24 | 禁止 | 无 | 最终排名依据 |

### 条件构建器池

| 买入 | 卖出 | 描述 |
|------|------|------|
| deviation_cross | deviation_cross | MA60 偏离穿越 |
| rsi_signal | rsi_signal | RSI 超卖/超买 |
| bollinger_signal | bollinger_signal | 布林下轨/上轨 |
| volume_spike | deviation_absolute | 放量异动 / MA 绝对偏离 |
| deviation_absolute | trend_follow | MA 绝对偏离 / ADX 反转 |
| trend_follow / none | none | ADX 趋势 / 规则禁用 |

### 超额收益

真实收益 − 现金基准收益（A股 rf=2%, 非A rf=4.5%, 日复利 r_f/252）。消除注资虚胖，真实反映交易 Alpha。

### 观测期预筛选

0-6 月无信号的构建器淘汰出搜索空间。实跑：银行/电信 ETF 卖出信号全 0 → 搜索空间从 33 维缩减。

### 全量运行结果 (2026-04-29)

- **A股 (18只, 150轮)**: Top-1 部署超额 -2.3%, 测试超额 +19.4%, 深跌抄底 + 永不卖出
- **非A股 (8只, 80轮)**: Top-1 部署超额 -3.9%, 测试超额 +49.6%, 布林信号 + 只选中海油
- 两个市场独立收敛到"买→持有→不卖"，主动禁用所有卖出规则

---

## 📡 信号扫描器 + 日报集成 (v1.15)

### 共识机制

加载最新优化结果 → 计算 Top-5 策略 → 对当日数据评估信号:
- **纳入监控**: ≥2/5 策略在 Top-5 中出现的构建器
- **纳入报警**: ≥3/5 策略在 Top-5 中出现的标的
- 每日 `--once` 运行时自动加载，产出共识信号 + 指标快照

### 回测嵌入

`run_backtest()` 用最新优化策略跑完整 24 月历史回测，3 阶段指标分拆，结果嵌入日报正文 + PDF。

### HTML 交互报告

自包含单文件 HTML (Plotly.js CDN)，暗色主题：
- 指标卡片行 / Plotly 交互收敛图 (可缩放悬停) / Top-10 策略表 (点击展开规则) / 最优策略详情
- 通过 health_server `/report/<token>` 路由提供，30 分钟时效链接
- Token 跨进程共享: `data/optimizer/.report_tokens.json` 文件

---

## 📧 xelatex PDF 日报 (v1.16)

### WeasyPrint → xelatex 替换

**原方案 (v3.6)**: WeasyPrint 将 `report_daily.html` 渲染为 PDF。
**问题**: 
- mathtext 公式是位图贴图，放大模糊
- 中文字体回退不稳定 (Noto Sans CJK SC 缺失字形)
- PDF 体积大 (644KB, 含 base64 图片)

**新方案**: Python → LaTeX 模板 → **xelatex 编译两次** → PDF 附件 (145KB)。
- 真 LaTeX 数学排版 (`amsmath`/`equation`/`split` 环境)
- 第一遍编译写入交叉引用 (.aux)，第二遍解析引用
- 附录: `appendix_methodology.md` → Python `markdown.markdown()` → `_esc()` 转义 → `$$...$$` 块直接注入 LaTeX

### LaTeX 模板: `report_daily.tex`

```
\documentclass[11pt,a4paper]{article}
\usepackage{xeCJK}
\setCJKmainfont{Noto Sans CJK SC}
```

- **ctexart → article+xeCJK**: 服务器未安装 ctex 宏包，改用 `xeCJK` + `article` 类
- **CRLF 修复**: Windows git 自动转 CRLF，xelatex 将 `\r` 视为 `^^M` 触发 Emergency stop。代码在编译前执行 `.replace("\r\n","\n")`
- **`_` 转义**: LaTeX 中 `_` 需转义为 `\_`，否则报 "Missing $ inserted"。`_esc()` 函数统一处理
- **颜色**: Navy 背景 / 白色表头 / 绿色正数 / 红色负数，港式财报风格
- **图表**: matplotlib 生成偏离度折线图 PNG (base64)，注入 LaTeX `\includegraphics`

### 编译流程

```
session → _build_tex_variables() → VAR 占位符替换
     → _esc() 转义 _ % & $ #
     → xelatex ×2 (-interaction=nonstopmode)
     → /tmp/report.pdf → read_bytes()
     → yagmail.send(attachments=[pdf])
```

编译耗时 ~2s，依赖: `texlive-xetex` + `texlive-lang-chinese` (约 500MB)

### 关键文件

| 文件 | 职责 |
|------|------|
| `src/templates/report_daily.tex` | LaTeX 日报模板 (101行) |
| `src/templates/appendix_methodology.md` | 13节公式附录 (193行) |
| `src/templates/email_template.html` | 邮件正文 (已大幅缩减) |
| `src/notification/email_notifier.py` | `_generate_daily_pdf()` (250行) |

---

## 🔒 安全加固 (v1.16)

### OTP 安全

- **随机源**: `random.randint(0, 9999)` → `secrets.randbelow(10000)` (密码学安全)
- **审计日志**: 不再记录明文 OTP，仅记录 `"ID:****"`
- **文件**: `src/health_server/core/global_instances.py`

### Health Server SSL/TLS

- **自签名证书**: `openssl req -x509 -newkey rsa:2048 -nodes`, 365 天有效期
- **监听**: `ssl.wrap_socket()` 包装 TCP socket，默认端口 1933
- **IP 配置**: `config.health_server.public_ip` 优先 → `ifconfig.me` fallback → 127.0.0.1
- **路径遍历防护**: `os.path.basename()` 消毒文件名参数

### 三层防御闸门

```
pre-commit hook  →  ruff lint + import smoke + safety tests
     ↕
CI/CD pre-deploy →  ruff lint + import smoke + core tests  (pre-push check)
     ↕
import smoke test →  24 key modules import integrity  (standalone)
```

**文件**: `hosts/pre-commit` / `ci_cd_deploy.py` / `tests/test_import_smoke.py`

### 安全测试

`tests/test_security.py` — 18 个测试覆盖:
- 表达式沙箱 (禁止 `__import__`/`eval`/`exec`/文件访问)
- 路径遍历防护 (`../` `/etc/passwd` 攻击)
- Token 格式验证 (必须是 base64url)
- OTP 随机性 (卡方检验)
- 速率限制 (单 IP 60 秒内 ≤5 请求)

---

## 🚀 CI/CD 增强 (v1.16)

- **环境变量**: `DEPLOY_HOST` / `DEPLOY_SSH_REMOTE` / `DEPLOY_REMOTE_DIR` 替代硬编码
- **简报 cron**: 自动注册 `09:50 daily` crontab 条目 (此前需手动添加)
- **texlive**: 自动安装 `texlive-xetex` + `poppler-utils` 系统依赖
- **pre-deploy checks**: ruff lint + import smoke + core tests (部署前自动运行)

---

## 🧪 测试覆盖 (v1.16)

| 测试集 | 数量 | 说明 |
|--------|------|------|
| backtest_config | 15 | 时间线约束 + 资金注入验证 |
| indicator_library | 18 | RSI/MACD/ATR/布林/ADX/量比 |
| signal_scanner | 14 | 共识计算 + 警报触发 |
| strategy_optimizer | 18 | 贝叶斯优化 + 构建器 + 预筛选 |
| security | 18 | OTP/沙箱/路径遍历/速率限制 |
| import_smoke | 24 | 关键模块导入完整性 |
| **合计** | **107** | 覆盖核心新功能 |

```bash
pytest tests/ -p no:capture -q           # 全量
pytest tests/test_security.py -v          # 安全专项
pytest tests/test_import_smoke.py         # 导入完整性
```

---

## 🔧 技术债务清理 (v1.16)

### 已完成清理
- ✅ `colorlog` 死依赖删除 (零 import)
- ✅ `camelot` PDF 表格解析死代码删除 (从未安装, 永远静默失败)
- ✅ `report_daily.html` 删除 (WeasyPrint 模板, 已被 .tex 替代)
- ✅ `alert_section.html` 删除 (不再独立渲染)
- ✅ 33 个 stale test import 修复
- ✅ Health server 5 处 `Path().parent.parent` 回归修复

### 待清理
| 文件 | 操作 | 原因 |
|------|------|------|
| `email_notifier.py` 旧 report_link 生成 | 删除 (~30行) | 时效链接由 HTML 报告保留 |
| `email_notifier.py` 旧监控表构建 | 删除 (~50行) | 已替换为 `_build_daily_table()` |
| `email_template.html` | 大幅缩减 | 正文已精简为摘要卡片 |

---

## 📊 开源准备 (v1.16)

| 文件 | 说明 |
|------|------|
| `LICENSE` | BSD-3-Clause + 投资免责声明 (软件仅供研究，作者不对投资损失负责) |
| `pyproject.toml` | 项目元数据 (name/version/authors/dependencies) |
| `CONTRIBUTING.md` | 贡献指南 (代码风格/测试/PR 流程) |
| `CHANGELOG.md` | v1.12 → v1.16 完整变更历史 |
| `README.md` | 项目概述 + 日报预览图 + CLI 命令 |

---

## 📝 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v1.0 | 2026-03-01 | 基础功能: 数据获取 + MA60 条件 + 邮件 |
| v1.8 | 2026-03-15 | 移除 akshare, 股息 LLM 缓存架构 |
| v2.0 | 2026-03-20 | 回测框架 |
| v2.8 | 2026-04-10 | baostock 历史数据缓存 |
| v3.0 | 2026-04-15 | Session-based 数据流 (Pydantic) |
| v3.1 | 2026-04-18 | 配置驱动指标计算，模块职能隔离 |
| v3.2 | 2026-04-26 | DataSource CSV 缓存 + 复权验证 |
| v3.3 | 2026-04-26 | 投资组合策略 + 规则引擎 (YAML 沙箱) |
| v3.4 | 2026-04-27 | 早盘简报 + 锚点择优算法 |
| v1.14 | 2026-04-29 | 策略搜索优化器 (贝叶斯+构建器池+超额收益) |
| v1.15 | 2026-05-01 | 信号扫描器 + 回测嵌入日报 + HTML 报告 |
| v1.17 | 2026-05-27 | 简报排序 + 收盘简报 14:30 + 日报 19:00 + debt_ratio 删除 + ROE PB/PE 推导 |
| **v1.16** | **2026-05-03** | **xelatex PDF 日报 + 安全加固 + 开源准备** |

---

## 🔗 相关文档

- [架构说明](../architecture.md) — 分层架构、数据流、模块职责
- [部署指南](../deployment.md) — 生产部署 + CI/CD
- [配置参考](../configuration.md) — config.yaml 详细说明
- [开发日志](../development/devlog.md) — 版本演进
- [贡献指南](../../CONTRIBUTING.md) — 代码风格 + 测试 + PR 流程

---

---

## 2026-05-17 邮件质量修复 + Pydantic 全量迁移

### 邮件渲染修复
1. **业绩增长列删除** — `email_notifier.py` 3 个 table section + 表头彻底移除死列
2. **告警行高亮 inline 化** — `<tr style="background:#fef9e7">` 替代 CSS class，兼容 Outlook/Gmail
3. **缺失值符号统一** — 43 处 `"-"` → `"—"` (em dash)
4. **邮件存档去嵌套** — `_save_email_copy` 不再包 `<html><body>`，仅 prepend HTML comment
5. **策略告警去重** — `signal_scanner.scan()` 按 `(code, rule_label)` dedup
6. **策略告警过滤** — 传统告警 table 中 `type=="strategy"` → `continue`
7. **港股分类修复** — 5 位代码不再误判为 A 股 (`len(code)==6`)
8. **行标区分** — 传统告警 `[MA60] 最低价 < MA60`，策略告警 `[策略]`
9. **Eastmoney 静默** — 删除假实现 WARNING 日志
10. **简报颜色 inline** — `style="color:..."` 替代 CSS class

### Pydantic 全量迁移
- `Rule` (rule_engine.py)
- `TradeRecord`, `StockMetrics`, `SubPeriodMetrics`, `PortfolioResult` (portfolio_strategy.py)
- `StrategyTrial`, `OptimizationReport` (strategy_optimizer.py)
- `SubPeriodMetrics` 去掉双重装饰器 (`@dataclass` + `BaseModel`)
- `__dataclass_fields__` → `hasattr(v, "label")` (strategy_optimizer.py:1037)
- `tests/test_rule_engine.py` 位置参数 → 关键字参数

### 数据清理
- `data_fetcher.py` 删除死字段 `earnings_growth`
- `email_notifier.py` 删除未使用 `earnings_growth` 局部变量
- `email_template.html` 删除僵尸 CSS (`.positive`/`.negative`/`.alert-row`)

**文档维护**: 本文档应在每次重大架构变更后更新  
**下次审查**: 2026-06-01
