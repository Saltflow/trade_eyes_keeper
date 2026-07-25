# 硬编码默认值 / 魔法值 / 兜底逻辑 全面审计

> 审计日期: 2026-07-25
> 范围: 仓库全部 Python 文件
> 禁止: 新增任何默认值、兜底、魔法值

---

## 统计

| 类型 | 数量 | 最严重 |
|------|------|--------|
| **MAGIC_VALUE** | ~90 | 30-day buffer (5处), 252 trading days (4处), chart styling (全部) |
| **DEFAULT_PARAM** | ~35 | `days=120` (3处), SMTP port=465, 风险利率=2% |
| **FALLBACK** | ~30 | alt source map 引用已删 Eastmoney, signal label cascade, 日期解析→合成日期 |

---

## 一、跨文件重复值

### 1.1 `days + 30` buffer (5 处, 3 个文件)

| 文件 | 行号 | 代码 |
|------|------|------|
| `src/data/data_source.py` | 120 | `effective_days = days + 30` |
| `src/data/data_source.py` | 135 | `self._fetch_with_verify(stock_code, days + 30)` |
| `src/core/data_fetcher.py` | 129 | `start_dt).days + 30` |
| `src/data/web_crawler.py` | 347 | `delta(days=days + 30)` |
| `src/data/web_crawler.py` | 753 | `delta(days=days + 30)` |

### 1.2 风险利率 (5 处, 3 个文件)

| 文件 | 行号 | 代码 | 值 |
|------|------|------|-----|
| `src/analysis/helpers.py` | 10 | `RISK_FREE_A = 0.02` | 2% |
| `src/analysis/helpers.py` | 11 | `RISK_FREE_NON_A = 0.045` | 4.5% |
| `src/analysis/config.py` | 191 | `self.risk_free_rate: float = 0.02` | 2% |
| `src/analysis/config.py` | 197 | `rates.get(group, 0.02)` | 2% |
| `src/notification/email_notifier.py` | 1169 | `annual_rate=0.02` | 2% |

### 1.3 交易日参数 (4 处, 3 个文件)

| 文件 | 行号 | 代码 | 值 |
|------|------|------|-----|
| `src/analysis/backtester.py` | 616 | `np.sqrt(252)` | 夏普年化 |
| `src/analysis/backtester.py` | 747 | `np.sqrt(252)` | 同上(重复) |
| `src/analysis/backtester.py` | 211 | `t // 21` | 月预算重置 |
| `src/analysis/backtester.py` | 844-846 | `* 21` | walk-forward 窗口 |

### 1.4 初始资金 + 月度限额 (8 处, 3 个文件)

| 文件 | 行号 | 代码 |
|------|------|------|
| `src/analysis/config.py` | 41-42,206-209 | `monthly_buy_limit=15000`, `initial_capital=100000` |
| `src/analysis/backtester.py` | 427-428 | `initial_cash=100000`, `monthly_buy_limit=15000` |
| `src/analysis/config.py` | 355,370 | `injections[m] = 20000` |

### 1.5 手续费 (3 处, 2 个文件)

| 文件 | 行号 | 代码 |
|------|------|------|
| `src/analysis/config.py` | 43,208 | `commission_rate: float = 0.005` |
| `src/analysis/backtester.py` | 430 | `commission_rate: float = 0.005` |
| `src/analysis/config.py` | 360 | `commission_rate=0.002` (训练用) |

### 1.6 手数 (5 处, 4 个文件)

| 文件 | 行号 | 代码 |
|------|------|------|
| `src/analysis/helpers.py` | 41-44 | `return 100` / `return 100` / `return 1` |
| `src/analysis/config.py` | 46 | `{"a_share": 100, "hk": 100, "us": 1}` |
| `src/analysis/backtester.py` | 429,1076 | `lot_size: int = 100` |
| `src/analysis/optimizer.py` | 248 | `lot_sizes.get(group, 100)` |
| `src/main.py` | 378,386 | `.get("a_share", 100)` |

### 1.7 汇率 (3 处, 2 个文件)

| 文件 | 行号 | 代码 |
|------|------|------|
| `src/analysis/config.py` | 49 | `{"a_share": 1.0, "hk": 0.9, "us": 7.0}` |
| `src/main.py` | 272 | `{"a_share": 1.0, "hk": 0.9, "us": 7.0}` |
| `src/main.py` | 379,387,395 | 同上 (3 次重复 .get) |

### 1.8 `_eval_lookback_days` 重复实现 (2 个文件)

| 文件 | 行号 | 函数 |
|------|------|------|
| `src/analysis/helpers.py` | 51-61 | `_eval_lookback_days()` → `274` 回退 |
| `src/main.py` | 574-586 | `_eval_opt_lookback()` → `274` 回退 |

---


## 二、分析层硬编码: 逐文件

### `src/analysis/search_interface.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 55 | RANDOM | `r.randint(0, max(d.levels - 1, 0))` | `random_params()` 用于日报生成非确定性参数 |
| 78 | FALLBACK | `self.values.get(dim.name, 0)` | 缺键静默回 0（最小级别） |
| 105 | MAGIC_VALUE | `final_cash: float = 0.0` | 0.0 和"从未设置"无法区分 |
| 186 | MAGIC_VALUE | `MAX_LOTS = 5` | **最大5档仓位**—超限静默拒绝 |
| 211 | MAGIC_VALUE | `month = t // 21` | 每月21个交易日假设 |
| 222, 230 | MAGIC_VALUE | `r.random() < 0.5` / `rate=0.15` | GA 操作概率硬编码 |
| 226 | RANDOM | `r.randint(0, max(d.levels - 1, 0))` | mutation 用随机数 |

### `src/analysis/helpers.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 8 | MAGIC_VALUE | `MIN_EVAL_DAYS = 60` | 60天静默排除新股 |
| 9 | MAGIC_VALUE | `MIN_TRADING_DAYS = 400` | 未使用常量 |
| 10-11 | MAGIC_VALUE | `RISK_FREE_A = 0.02` / `RISK_FREE_NON_A = 0.045` | 跨文件重复 |
| 41-44 | MAGIC_VALUE | `return 100` / `100` / `1` | 手数硬编码 |
| 59-61 | MAGIC_VALUE | `30.4375` / `274` | 日历天近似 + 回退值 |

### `src/analysis/backtester.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 36-51 | MAGIC_VALUE | `IDX_CLOSE = 0` ... `IDX_MA200_DEV_PCT = 15` | 16个硬编码列索引,必须和 INDICATOR_NAMES 严格同步 |
| 176-178 | DEFAULT_PARAM | `min_holding_days=30`, `buy_price_mode=1`, `lot_mode=1` | JIT 数默认值不在 config 中 |
| 186 | MAGIC_VALUE | `MAX_LOTS = 5` | 最大5档,拒绝超限购买信号 |
| 254,317 | MAGIC_VALUE | `max(max_amt, 5000.0)` | 最低买卖金额5000元 |
| 427-433 | DEFAULT_PARAM | FastEvaluator 全部默认值 | 与 config.py 重复 (两处真源) |
| 482-483 | FALLBACK | `np.zeros((T, N), dtype=bool)` | score_signals 为 None 时静默全 False |
| 551 | FALLBACK | `buy_fracs if buy_fracs else [1.0]` | 缺少买比例时回退 **100% 全仓** |
| 668 | MAGIC_VALUE | `quarterly_interval: int = 63` | 季度 = 63 个交易日 |
| 696 | DEFAULT_PARAM | `min_holding_days=0, buy_price_mode=2, lot_mode=2` | 日报路径与优化器路径不同的默认值(优化器用 FIFO+30天,日报用3日最高价+无锁仓) |
| 1000-1002 | MAGIC_VALUE | `buy_threshold=0.5`, `position_frac=0.15` | **日报硬编码 15% 仓位比例** — 完全绕过 optimizer 参数 |
| 1060 | FALLBACK | `getattr(params, "_engine", "") or getattr(strategy, "name", "percentile")` | **三层回退**: params._engine → strategy.name → "percentile" |
| 1075-1076 | FALLBACK | `fx_map.get(group_name, 1.0)` / `lot_map.get(group_name, 100)` | 汇率和手数回退 |

### `src/analysis/optimizer.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 46-65 | RANDOM | `random.randint(...)` / `random.random()` | 未播种全局随机数 |
| 61 | DEFAULT_PARAM | `rate: float = 0.15` | 与 search_interface.py 重复 |
| 94 | MAGIC_VALUE | `1 + constraints.risk_free_rate / 252` | 年化因子 |
| 230 | DEFAULT_PARAM | `group: str = "a_share"` | 默认只优化A股 |
| 248 | FALLBACK | `lot_sizes.get(group, 100)` | 手数回退 |

### `src/analysis/config.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 59-66 | FALLBACK | WalkForwardConfig 全部 `.get()` | 12/9/3 月默认窗口 |
| 78 | MAGIC_VALUE | `max(self.test_months * 30, 274)` | 30天/月 × 月数, 硬下限274 |
| 86-95 | FALLBACK | GeneticSearchConfig 全部 `.get()` | 10000样本/1000优势/1000人口/5000后代 |
| 191,197 | DEFAULT_PARAM | `risk_free_rate = 0.02` | 跨文件重复 |
| 314-321 | DEFAULT_PARAM | BacktestConfig 全部默认值 | observe_end_month=6, capital=100000 |
| 347 | MAGIC_VALUE | `(d.day - ref.day) / 30.0` | 30.0天/月 |
| 354-355 | MAGIC_VALUE | `range(6, 13)` / `20000.0` | 注入计划硬编码 |
| 360 | MAGIC_VALUE | `commission_rate=0.002` | 训练与正常手续费不同 |
| 384 | FALLBACK | `return StrategyConstraints()` | YAML缺失时返回全默认约束 |

### `src/main.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 219 | FALLBACK | `config.get("dashboard", {}).get("strategy", "percentile")` | 三层回退策略名 |
| 222 | FALLBACK | `get_strategy("percentile")` | strategy 为 None 时硬编码回退 |
| 223 | RANDOM | `strategy.random_params()` | **日报用随机参数** (不搜参的直接后果) |
| 231 | MAGIC_VALUE | `len(df) >= 60` | 60天最低数据要求 |
| 272,378-395 | MAGIC_VALUE | FX rates `{a_share: 1.0, hk: 0.9, us: 7.0}` | 跨文件重复6次 |
| 544-547 | MAGIC_VALUE | `"510300": 730, "510880": 730, "VOO": 730, "BRK.B": 730` | 硬编码基准ETF代码和回看天数 |
| 567 | BUG | `_scan_group()` 空壳 | 函数体无 return,调用方遍历 None → TypeError |
| 665 | MAGIC_VALUE | `日报 19:00 / 简报 09:50 14:30 / 搜参 02:00` | 通知文本中硬编码调度时间 |

---

## 三、数据层硬编码

### `src/data/technical_indicators.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 60 | FALLBACK | `[{"name": "ma60", "type": "daily_ma", "window": 60}]` | 配置加载失败时只提供 MA60 但缺少 WMA20/30/50 等 |
| 79,141 | MAGIC_VALUE | `min_periods=1` | 所有 MA 第一个数据点直接取本身 |
| 335 | MAGIC_VALUE | `period: int = 14` | RSI 周期 14 |
| 349-352 | MAGIC_VALUE | `fast=12, slow=26, signal=9` | MACD 12/26/9 |
| 366 | MAGIC_VALUE | `period: int = 14` | ATR 周期 14 |
| 384 | MAGIC_VALUE | `window=20, num_std=2.0` | 布林带 20/2 |
| 405 | MAGIC_VALUE | `period: int = 14` | ADX 周期 14 |
| 440 | MAGIC_VALUE | `window: int = 20` | 量比窗口 20 |
| 453,456 | MAGIC_VALUE | `rolling(60)` / `rolling(200)` | MA周期硬编码 |
| 460 | MAGIC_VALUE | `window: int = 252` | 分位窗口 252 |
| 477 | MAGIC_VALUE | `len(win) >= 20` | 最少 20 个有效观测 |
| 482-492 | DEFAULT_PARAM | compute_all() 全部 8 个参数 | 无一来自 config |

### `src/data/data_source.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 31-37 | FALLBACK | `ALT_SOURCE_MAP` | 引用已删的 `_fetch_from_eastmoney` |
| 63,161 | DEFAULT_PARAM | `days: int = 120` | 默认 120 天(与其他文件 730 不一致) |
| 112-113 | MAGIC_VALUE | `+ 5` / `max(tail_days, 5)` | 增量拉取缓冲 |
| 120,135 | MAGIC_VALUE | `days + 30` | 全量拉取缓冲 |
| 254-256 | DEFAULT_PARAM | `"cache_bypass_cutoff", "15:55"` | 缓存绕过时间 |
| 264 | FALLBACK | `datetime(..., 15, 55)` | 解析失败回退 |
| 285 | MAGIC_VALUE | `MAX_RETRIES = 2` | 重试次数 |
| 359,389,401 | MAGIC_VALUE | `< 3` | 最小3数据点 |
| 374 | MAGIC_VALUE | `> 0.05` | 5%复权修正阈值 |
| 410 | MAGIC_VALUE | `diff > 0.01` | 0.01元交叉验证阈值 |

### `src/core/data_fetcher.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 32 | FALLBACK | `"web_crawler"` | 数据源类型缺失时静默回退 |
| 44 | DEFAULT_PARAM | `"Asia/Shanghai"` | 时区硬编码 |
| 84 | DEFAULT_PARAM | `"./data"` | 保存目录回退 |
| 214 | MAGIC_VALUE | `days=365` | LLM 分红缓存回看 |
| 299 | MAGIC_VALUE | `days=730` | session 历史数据回看 |
| 386-387 | MAGIC_VALUE | `dividend_yield > 30` | 股息率上限 + 自相矛盾 |

### `src/data/web_crawler.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 37-40 | MAGIC_VALUE | User-Agent / timeout=30 / retries=3 / delay=2 | 全部硬编码 |
| 179-201 | MAGIC_VALUE | ETF 识别列表 | 新 ETF 需改代码 |
| 250-270 | FALLBACK | 市场数据源退阶链 | SG 只有 2 个源, US/HK 部分源返回实时价(仅 1 数据点) |
| 340 | MAGIC_VALUE | `["106", "107", "108"]` | 美股交易所前缀 |
| 366 | MAGIC_VALUE | `"fqt": "1"` | 前复权硬编码 |
| 1371-1372 | MAGIC_VALUE | `0 < abs(pe) <= 1000` / `0 < abs(pb) <= 50` | PE/PB 静默过滤阈值 |
| 1552 | MAGIC_VALUE | `delta(days=365)` | 分红汇总窗口 |
| 1765 | MAGIC_VALUE | `int(years * 365)` | 年→天转换(忽略闰年) |

---

## 四、通知层硬编码

### `src/notification/email_notifier.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 673,681,697 | FALLBACK | SMTP port=465 / archive dir / token timeout=30 | 全部回退值 |
| 729,826 | DEFAULT_SUBJECT | `股票提醒 - {datetime}` / `股票日报 - {datetime}` | 主题格式硬编码 |
| 1485-1606 | MAGIC_VALUE | HTML 策略说明中的数字: 15000/5000/10000/10万/2%/4.5% | 与 config 不一致 |
| 2606-2637 | MAGIC_VALUE | 锚点优先级 dict / 偏离阈值列表 -10,-5,...+15 | 与 alerts.yaml 重复 |
| 3146 | MAGIC_VALUE | `axvline(x=-0.5)` | 图表买线-0.5% 硬编码 |
| 3268 | FALLBACK | `["deviation", "rsi"]` | consensus 为 None 时默认指标 |
| 3471 | MAGIC_VALUE | `timeout=60` | xelatex 编译超时 |

### `src/notification/chart_generator.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 42-48 | MAGIC_VALUE | 锚点-窗口映射 `{wma50:50,...}` | 新锚点得 window=0 |
| 84 | DEFAULT_PARAM | `trading_days: int = 60` | 默认60交易日 |
| 133-138 | MAGIC_VALUE | `cols=4`, `fig_width=12`, `rows*2.2` | 图表布局全部硬编码 |
| 174-229 | MAGIC_VALUE | 全部颜色/大小/字体/alpha/线宽 | 样式硬编码 |
| 234 | MAGIC_VALUE | `dpi=130` | 第一张图 DPI |
| 365 | MAGIC_VALUE | `dpi=100` | 第二张图 DPI (不同) |
| 490 | MAGIC_VALUE | `dpi=150` | 第三张图 DPI (又不同) — **3 张图 3 个 DPI** |
| 393-398 | MAGIC_VALUE | 组标签 dict | 新组无标签 |
| 401 | MAGIC_VALUE | `("a_share", "hk", "us")` | 跳过 non_a_share / sg |
| 407 | MAGIC_VALUE | `len(navs) < 20` | 20 点最低 NAV |
| 411 | MAGIC_VALUE | `round(len(navs) / 21)` | 月份估算用 21 交易日 |
| 422 | MAGIC_VALUE | `base = nav_arr[0] if nav_arr[0] > 0 else 1.0` | 净值 ≤0 时设为 1.0 |
| 452-456 | MAGIC_VALUE | 基准 ETF 映射 `{a_share: [510300,510880], hk/us: [VOO,BRK.B]}` | 新组无基准 |
| 482 | MAGIC_VALUE | `YearLocator()` | 年度刻度 |

---

## 五、P0 级别的 BUG

### 5.1 `_scan_group()` 空壳
- 文件: `src/main.py` 行 567-571
- 函数体只有 `strategy = get_strategy("percentile")` 然后**没有 return**
- 调用方 (行 189-191 和 354-356) 遍历返回值 → `TypeError: 'NoneType' object is not iterable`
- 被 try/except 吞掉 → `signal_scan = None` → 邮件信号段永远为空

### 5.2 `ALT_SOURCE_MAP` 引用已删代码
- 文件: `src/data/data_source.py` 行 31-37
- `_fetch_from_eastmoney` 按 AGENTS.md 已删除, 但 ALT_SOURCE_MAP 仍引用它
- 静默创建无效回退路径

### 5.3 `mail.py:223` 用 RANDOM 参数跑日报
- `strategy.random_params()` 为 24 维简化策略生产垃圾参数
- 没有 optimizer 产出文件 → 日报永远用随机数

### 5.4 FastEvaluator 与 simulate_portfolio 默认值冲突
- `backtester.py:427-433` (FastEvaluator) 用 `min_holding_days=30, lot_mode=1`
- `backtester.py:696` (simulate_portfolio) 用 `min_holding_days=0, lot_mode=2`
- 优化器路径和日报路径用不同的交易规则

### 5.5 PE/PB 静默过滤
- `web_crawler.py:1371-1372` PE > 1000 或 PB > 50 时静默丢弃估值数据
- 无日志, 无警告, 贵价股/困境反转股基本面无缘

---

## 六、禁止列表

以下模式禁止出现在数据管线中:

| 禁止代码 | 替代 |
|----------|------|
| `config.get("key", HARDCODED_DEFAULT)` | 键缺失即报错,或从单头来源读取 |
| `x or FALLBACK` 用作回退 | 显式 None 检查 + 错误处理 |
| `random_params()` 在非 GA 上下文中 | 日报必须读 optimizer 输出文件 |
| `strategy.random_params()` | 同上 |

---

## 六、策略引擎 / session / core 层硬编码

### `src/session/session_manager.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 85 | MAGIC_VALUE | `uuid4())[:8]` | UUID 截断为 8 字符 |
| 161 | MAGIC_VALUE | `max_retries = 2` | 重试次数硬编码 |
| 234 | DEFAULT_PARAM | `days: int = 730` | 默认回看天数 |

### `src/models/schemas.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 134 | MAGIC_VALUE | `v < 0.1` | 价格 < 0.10 元视为无效 |
| 142 | MAGIC_VALUE | `v < 0.1` | MA60 同样 0.1 阈值 (不同语义) |
| 180 | DEFAULT_PARAM | `data_source: str = "sina"` | 数据源无信息时静默默认 |

### `src/core/ref_portfolio.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 23 | MAGIC_VALUE | `AL_CAPITAL = 100000.0` | 初始资本硬编码 |
| 24 | MAGIC_VALUE | `COMMISSION_RATE = 0.005` | 手续费硬编码 |
| 28 | MAGIC_VALUE | `MONTHLY_LIMIT = 50000.0` | 月限额与代码中 15000 **冲突** (两个值并存) |
| 242 | DEFAULT_PARAM | `lot_size: int = 100` | 手数默认A股 |
| 244 | DEFAULT_PARAM | `monthly_buy_limit: float = 15000.0` | 另一月限额默认值 |

### `src/interactive/commands/handlers.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 326 | MAGIC_VALUE | `+ 365, 1000)` | 回测回看天数缓冲 |
| 343-347 | MAGIC_VALUE | 13 维参数字典 | **硬编码 percentile 默认参数**—与 engine 重复 |
| 453-457 | MAGIC_VALUE | `"SAMPLES": "2000"` / `"20000"` | 优化器预设"fast"/"deep"超参 |
| 683-715 | MAGIC_VALUE | config reset 全部默认值 | 又一整套硬编码配置 |

### `src/analysis/strategies/percentile/engine.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 30 | MAGIC_VALUE | `WINDOW = 252` | A股实际~244交易日 |
| 31 | MAGIC_VALUE | `_LEVELS = 10` | 离散化级别 |
| 35 | MAGIC_VALUE | `0.1 + (...) * 0.8` | tau 值范围 [0.1, 0.9] |
| 39 | MAGIC_VALUE | `[0.1, 0.3, 0.5, 0.7, 0.9]` | 5 个权重级别 |
| 70-71 | DEFAULT_PARAM | `_tau", 5))` / `_w", 2))` | 缺少参数键时静默默认 |
| 150 | MAGIC_VALUE | `[0.05, 0.15, 0.25, 0.35, 0.45]` | 仓位比例级别(与 ParamDim 重复) |

### `src/analysis/strategies/builder/engine.py` (最严重: **45+ 魔法浮点值**)

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 27 | MAGIC_VALUE | `-0.30 + 0.005` | 偏离穿越阈值范围 |
| 29 | MAGIC_VALUE | `< 0.01` | ±1% 重置带 (出现 **4 次**: L29,58,100,121) |
| 34 | MAGIC_VALUE | `80.0 - th_norm * 50.0` | RSI 阈值 [80,30] |
| 42 | MAGIC_VALUE | `0.05 + th_norm * 0.45` | Bollinger %B 阈值 |
| 49 | MAGIC_VALUE | `1.2 + th_norm * 2.8` | 量比阈值 [1.2,4.0] |
| 64 | MAGIC_VALUE | `20.0 + th_norm * 40.0` | ADX 阈值 [20,60] |
| 75 | MAGIC_VALUE | `-0.20 - th_norm * 0.30` | 绝对折价阈值 |
| 89 | MAGIC_VALUE | `ma200 * 0.8` / `slope > -0.05` / `> 0.95` | 深度价值 3 个阈值 |
| 160-161 | MAGIC_VALUE | `BUILDER_COUNT = 8` / `SELL_BUILDER_COUNT = 6` | 依赖 dict 插入顺序 |
| 162 | MAGIC_VALUE | `RESHOLD_LEVELS_BUILDER = 10` | 与 percentile 重复 |
| 253,272 | MAGIC_VALUE | `_confirmation(..., 3)` / `_confirmation(..., 1)` | 确认天数 (3买/1卖) |
| 180,185,219,228 | MAGIC_VALUE | `range(5)` / `range(3)` | 规则槽位数(出现 4 次) |

### `src/analysis/strategies/simplified/engine.py`

| 行号 | 类型 | 代码 | 问题 |
|------|------|------|------|
| 9-10 | MAGIC_VALUE | `BUY_LIMIT_LEVELS` / `SELL_LIMIT_LEVELS` | [5k,10k,20k,30k,50k] — 相同列表定义两次 |
| 11 | MAGIC_VALUE | `VELS_SIMP = 10` | 阈值离散化级别(与 percentile/builder 重复) |
| 12-13 | MAGIC_VALUE | `NUM_BUY_RULES = 5` / `NUM_SELL_RULES = 3` | 与 builder 重复 |
| 14-20 | MAGIC_VALUE | 买卖构建器列表 | 依赖 CONDITION_BUILDERS_FAST 键名 |
| 115,132 | MAGIC_VALUE | `_confirmation(..., 3)` / `_confirmation(..., 1)` | 确认天数(与 builder 重复) |

---

## 七、跨策略确认天数重复

| 策略 | 确认天数(买) | 确认天数(卖) | 文件 | 行号 |
|------|----------|----------|------|------|
| percentile | N/A (用连续评分) | N/A | — | — |
| builder | 3 | 1 | builder/engine.py | 253, 272 |
| simplified | 3 | 1 | simplified/engine.py | 115, 132 |
| FastEvaluator | 3 | 1 | backtester.py | 431-432 |
| **总计 4 处重复,2 种值** |

## 八、跨策略规则槽位数重复

| 值 | 出现文件 | 出现次数 |
|-----|---------|---------|
| NUM_BUY_RULES = 5 | builder(4次), simplified(1次), handlers.py(1次) | 6 |
| NUM_SELL_RULES = 3 | builder(3次), simplified(1次), handlers.py(1次) | 5 |
| threshold_levels = 10 | percentile, builder, simplified | 3 |
| 确认天数 = 3/1 | builder, simplified, FastEvaluator | 3 |

## 九、月限额双值冲突

| 值 | 位置 | 上下文 |
|-----|------|--------|
| **50,000** | `ref_portfolio.py:28` | `REF_MONTHLY_LIMIT` 模块常量 |
| **15,000** | `ref_portfolio.py:244` | `rebalance()` 函数默认参数 |
| **15,000** | `backtester.py:428`, `config.py:41` | FastEvaluator / ExecutionConfig |
| 这两个值同时存在于 ref_portfolio.py 中,**未解释关系** |


## 十、总计量

| 类型 | 第一批(分析层) | 第二批(数据+通知层) | 第三批(session+策略引擎) | **总计** |
|------|-------------|---------------|----------------------|--------|
| MAGIC_VALUE | ~50 | ~85 | ~65 | **~200** |
| DEFAULT_PARAM | ~20 | ~30 | ~25 | **~75** |
| FALLBACK | ~20 | ~25 | ~8 | **~53** |
| RANDOM | 8 | 0 | 0 | **8** |
| BUG | 3 | 2 | 1 | **6** |
| **合计** | | | | **~342** |

---

## 审计完成

- 审计文件数: 22 个 Python 文件
- 发现硬编码总数: ~342 处
- 假跨文件重复: 30+ 处 (多文件同值但无单头来源)
- BUG: 6 个 (含 _scan_group 空壳, ALT_SOURCE_MAP 引用已删代码, 日报用 random_params, FastEvaluator 路径冲突, PE/PB 静默过滤, 月限额双值)

