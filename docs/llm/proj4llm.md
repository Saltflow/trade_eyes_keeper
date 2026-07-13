# 股票量化系统 - 关键设计决策文档

**文档版本**: v1.19
**最后更新**: 2026-07-12
**压缩目标**: ~800行，保留关键设计决策

---

## 🔌 双搜参引擎架构 (v1.19 新增)

### 背景问题
分位评分引擎 (PercentileSignalFn) 之前只是"空壳"：`signal_fn` 仅用于往 YAML 写 `_engine` 标签，其 `evaluate()` 从未被遗传搜索调用。YAML 里实际是全局阈值格式参数 (`buy_1_signal: deviation_absolute`)，导致"信号名与指标对不上"。

### 解决方案：SignalFn 真正接入遗传搜索
- **SignalFn ABC** (`signal_functions.py`)：唯一替换点。新增方法：
  - `random_params/crossover/mutate` — genome 编解码（genome = `Params.values` 整数级别 dict）
  - `scan_signals(params, today, history)` — 引擎自身逻辑判断今日买卖信号（显示层用）
  - `describe_rules(params)` — 参数翻译成买卖规则名（报告用）
  - `engine_brief()` — 引擎买卖标准简介（/switch_optimizer 展示）
  - `execution_params(params)` — 解码执行阈值（买/卖分数阈值+仓位）
- **SignalFnSearchEngine** (`signal_fn_engine.py`)：把 SignalFn 包成遗传搜索器的 `StrategyEngine` 插件。encoding=Params，`evaluate_encoding` 调 `signal_fn.evaluate()` → 共享流水线 `simulate_portfolio`/`compute_metrics` → `WindowStats`。**engine 分支单线程（无 pickle 问题）**。
- **main.py**：`engine_type==percentile` → `engine=SignalFnSearchEngine(signal_fn)`；global → `engine=None`（100% 走旧向量化路径，criterion 1/2 逻辑零改动）。
- **strategy_optimizer_v2._build_report**：`hasattr(ss.encoding,'values')` 判定引擎模式。分位模式写真实分位参数 (`adx_pct_tau` 等, `_mode: signal_score`) + `_signal_fn_to_rules`（引擎自定义规则名 + `condition: __signal_fn__` 标记）。`_save_results` 序列化 `label` 字段。
- **SignalScanner.scan**：rules 含 `__signal_fn__` → 按 `_engine` 构造 SignalFn → `scan_signals()` 用引擎逻辑判断；否则走 legacy `condition` 评估（全局引擎/旧 YAML）。分位分派用 `_computed_cache`（compute_all 后的完整 DataFrame）算滚动分位。

### 关键不变量
- **全局引擎 = deprecated 但默认**，`engine=None`，走旧 FastEvaluator 向量化路径，日报版式与逻辑完全不变
- 旧 YAML（无 `_engine` 或 `_engine: global`, 真实 condition）永远走 legacy 路径
- 分位引擎信号名来自引擎自身：如 `分位评分买入 (score 0.29>0.19)` + detail `趋势强度分位(ADX)分位71%>46%`

### 评分引擎执行语义（`_score_sim_core`，主链路唯一决策仿真）
锁定于 `tests/test_score_engine.py`（18 项）：
1. **买入执行价 = 近 3 日收盘均价**（含当日；不足 3 日按现有天数）—— 源自 commit b01233f「3日确认+均价执行」，此前只在 Python 后备路径落地，numba/评分路径缺失，现已统一
2. **卖出执行价 = 单日收盘价**（触发日，不平滑）
3. **同日互斥**：同一标的同日既触发买又触发卖 → 双向跳过
4. **月度买入额度（分批注入，勿设 inf）**：日报回测 `_evaluate_signal_fn` 用 `MONTHLY_BUY_LIMIT=15000`，搜参 `SignalFnSearchEngine` 用 `100000`，与旧 global 完全一致。⚠️ **教训**：曾误将限额改为 `inf` 修「100%空仓」bug，导致首日满仓、收益虚高 5000BP+（实验证实 inf=+97% vs 15000=+10%）。空仓 bug 的正解是「买入额截断到剩余月度额度」，而非取消限额。
5. **允许回补**：卖出后可再买入（无 `shares==0` 永久壁垒）
6. 手数取整 / 手续费 / 现金约束 / 评分需严格 `> 阈值`
7. **季度持仓成本快照**：`_score_sim_core` 每季度边界快照 `q_cb`(成本基础)+`q_price`(价格)，显示层用时点值。⚠️ 勿用最终 `cost_basis[i] ÷ 季度时点 shares`（时点错配 → 假成本 0.00/295.87、假 pnl +16171%）

### 测试矩阵（量化引擎，共 ~80 项）
- `test_score_engine.py`（18）：决策仿真全部执行语义
- `test_percentile_engine.py`（15）：scan_signals/score_timeseries/execution_params/describe_rules/engine_brief/genome
- `test_signal_scanner.py`（20）：`__signal_fn__` 分派 + `_params_from_yaml` + `_make_signal_fn` + legacy 兼容
- `test_signal_fn_engine.py`（5）：适配器编码操作 + evaluate_encoding + 端到端分位搜索
- `test_portfolio_strategy.py`（+7）：`_evaluate_signal_fn` 真实交易回归（防 100% 空仓复发）
- **测试目录结构**：`tests/__init__.py` + `tests/integration/__init__.py` 消除同名模块（`test_signal_scanner.py`）收集冲突；`conftest.py` autouse fixture 清理绑定到已关闭捕获流的 StreamHandler

---

## 📋 执行摘要

本文档记录股票量化监控系统的关键设计决策，涵盖架构选择、问题解决方案和技术路线。系统核心能力：策略搜索优化器、信号扫描器、xelatex 日报 PDF 生成、邮件提醒、健康检查服务器。

### 核心原则
1. **真实数据优先** - 杜绝模拟数据，多源降级（新浪→腾讯→Yahoo）
2. **配置驱动** - 技术指标/规则/简报通过 YAML 配置，避免硬编码
3. **文档驱动开发** - 所有设计决策存档至本文档
4. **防循环编码** - cycle_guard 自动检测重复错误模式
5. **Session 统一管理** - 全局 SessionContext 防止字段混淆
6. **全量验证** - 策略优化/信号扫描/回测分析基于 config 全量标的运行，禁止子集验证

---

## 🏗️ 架构现状 (v1.18)

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
  │  - web_crawler.py   (新浪/腾讯/Yahoo 多源降级)                  │
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

**主源**: 新浪财经历史数据 API → 腾讯财经 → Yahoo Finance（自动降级）
**股息**: LLM 提取缓存优先 → 网页爬虫备用
**缓存**: `cache/data/` CSV 格式，7 天保留期
**baostock**: 回测历史数据专用（前复权），日期格式 YYYY-MM-DD

---

## 🎯 策略搜索优化器 (v1.14 → v1.18)

### 🔑 权威设计契约 (v1.18, 2026-07-07) — 冲突时以本节为准

> 本节记录搜参 + 日报/简报的**最终设计契约**。历史章节（V1/V2 早期描述）若与本节冲突，一律以本节为准。实现细节见 `config/optimizer_constraints.yaml`（唯一真源）。

#### 1. 搜参：多窗口验证 + 测试集排名 + 验证窗口留出

- **Walk-Forward 多窗口**（取代早期"9个月单窗口 / 4阶段"设计）：
  - 训练 12 月 + **测试 9 月**，滑动步长 3 月，**14 窗口**，数据 5 年
  - 配置：`config/optimizer_constraints.yaml` → `walk_forward`
- **验证窗口留出**（v1.18, 2026-07-07）：`validation_windows: 1`
  - **最后 1 个窗口（最近 9 月）不参与 `wf_score` 排序**，作纯样本外验证
  - 代码：`genetic_searcher._compute_wf_score` + 模块级 `compute_wf_score` 均排除 `all_stats[-N:]`
  - 理由：最近数据若参与排序，选出的策略已隐含偷看验证期 → 胜率不置信
- **排名依据**：`avg(排序窗口测试期超额收益)`（排除验证窗口后的均值）
  - 代码：`strategy_optimizer_v2.py` → `rank_ws = all_ws[:-v_win]`；`avg_test_ret = mean([ws.test_excess_return for ws in rank_ws])`
  - 稳定性惩罚：`stability_penalty × std(窗口收益)`，防单窗口运气
- **禁止**：用训练期收益排名、用全期收益排名、事后挑测试期最优（回看偏差）、最近9月参与排序

#### 2. 收益评估：约束下最高收益，超额 vs 基准

- **目标函数**：在硬约束满足前提下，最大化测试期收益
- **硬约束**（`hard_constraints`）：最大回撤 ≥ -40%、平均仓位 ≥ 5%、收益标准差 ≤ 15%、月交易 1-100 次；不满足直接丢弃
- **收益基准**（超额收益 = 策略收益 − 基准收益）：
  | 分组 | 基准 |
  |------|------|
  | A股 | 无风险(2%) / 510300(沪深300) / 510880(红利ETF) |
  | 非A股 | 无风险(3.8%) / VOO(标普500) / BRK.B |
  - 配置：`config/optimizer_constraints.yaml` → `benchmarks`
  - **冲突时以此配置为准**（多处有基准实现，此处为唯一真源）
- **交易模型**：position_target（sigmoid 动态仓位），非固定分数

#### 3. 日报/简报：直接读 YAML 预估收益，不重新搜参

- **独立搜参任务（02:00 cron）继续更新 YAML** — 这是唯一的搜参入口
- **日报/简报只读取 YAML** Top1 策略的预估收益（近 9 个月测试期），**不再自己重新搜参/贪心选股**
- **废弃**：日报中 `PortfolioOptimizer.run()` 每日重新贪心搜索（导致结果天天大幅波动、"抽奖"效应）
- **展示数据**：Top1 策略的 `test_return`（测试期超额）、`test_drawdown`、`sharpe`、`_stocks`（选股）、`quarterly_holdings`（季末持仓）、平均现金仓位
- **今日信号**：`SignalScanner` 用 YAML 的 `rules` 条件字符串评估当日数据，`strategy_rank==1` 与 Top1 展示对齐
- **固定选股评估**：`PortfolioOptimizer.run_fixed(stock_selection)` — 对 YAML `_stocks` 跑一次确定性 `evaluate()`，产出 NAV 曲线 + 季末持仓，不搜索

#### 4. 三组独立搜参 + 独立资金池 + 验证期胜率 (v1.18, 2026-07-11)

- **三组独立搜参**（v1.18 2026-07-11 起）：优化器 V2 按 A股/港股/美股**分别搜参**，产出三份 YAML：
  - `*_a_share_strategies.yaml` / `*_hk_strategies.yaml` / `*_us_strategies.yaml`
  - 动机：港美股走势差异大，混搜时港股趋势性主导搜索空间，美股被迫用不适配的规则（如全是 trend_follow）
  - `run_optimization_v2` 用 `_detect_fine_group` 三分；`optimizer_constraints.yaml` benchmarks 加 hk/us（=VOO/BRK.B）
  - 回退：hk/us YAML 未生成时用 non_a_share YAML
- **三组独立资金池**（`_detect_fine_group`，日报展示层）：
  | 细分组 | 判定 | 资金池 | 主基准 | 规则来源 |
  |--------|------|--------|--------|----------|
  | a_share | 6 位纯数字 | 独立 100k | 510880 红利ETF | a_share YAML |
  | hk | 5 位纯数字 | 独立 100k | VOO | hk YAML |
  | us | 含字母 | 独立 100k | VOO | us YAML |
  - **`_detect_stock_group`（二分）保持不变** — risk_free/回测仍用 a_share/non_a_share
  - **标的池来自 config**（非 YAML `_stocks`）：`run_fixed(groups=)` 遍历 config stocks 按细分组分池，删/加标的立刻生效
  - `SignalScanner.scan(group)`：group∈a_share/hk/us，按细分组过滤标的 + 读对应 YAML
- **信号名按组翻译**：`_readable_signal(code, ..., map_a, map_hk, map_us)` 按标的细分组选对应 YAML 的信号名映射（A股 buy_1≠港股 buy_1≠美股 buy_1）
- **验证期胜率**（现场算，不信搜出来的窗口）：
  - `email_notifier._calc_validation_winrate()`：用 `run_fixed` 产出的 `nav_series` vs 主基准价格
  - 最近 9 月逐日算 forward return，"任意一天买入持有到期跑赢主基准的概率"
  - 完全离线，不依赖 YAML 搜出的数字
- **净值图**：每组一张（chart002=A股/chart003=港股/chart004=美股），Top1 绿线 + 30日布林带 + 红色基准线

#### 5. 未解禁定增展示 (v1.18, 2026-07-09)

- **数据源**：东方财富 `RPT_SEO_DETAIL` API（`web_crawler.fetch_placement_data`）
- **仅 A 股**（6 位纯数字），19:00 全量任务抓取（简报不触发）
- **只展示未解禁**：`is_locked=True`（当前 < 解禁日）；解禁日 = 上市日 + 锁定期
- **展示字段**：代码 / 名称 / 未解禁定增数额(亿股) / 占发行后总股本% / 定增价格 / 解禁时间
- **取最近一次**：多条按上市日期降序取第一条
- 代码：`_parse_placement_row`（占比+解禁日）、`_parse_lockin_years`（"3年"→3, "18个月"→1.5）、`email_notifier._build_placement_section`

---

### V2 优化器 (v1.17-beta, 2026-06-03)

**设计哲学**:
1. 模糊的正确好过精确的错误 — 不用贝叶斯精确拟合历史噪音
2. 永远正确的判断是废话 — 单维度信号（如纯量比）不区分方向，无信息增量
3. 好的机会不稍纵即逝 — 状态型信号替代穿越型，给足够判断时间

**V1 vs V2 架构对比**:

| 维度 | V1 (贝叶斯) | V2 (遗传搜索 + Walk-Forward) |
|------|------------|-------------------------------|
| 搜索方式 | 贝叶斯连续优化 | 离散网格 + 遗传算法 |
| 评估方式 | 单测试期排名 | Walk-Forward 14窗口评分 |
| 回测引擎 | PortfolioEvaluator 逐日Python | FastEvaluator numpy+numba 向量化 |
| 规则模板 | 2买3卖, 独立OR | 5买0卖, 离散选择 |
| 约束 | 仅回撤软惩罚 | 硬约束过滤：仓位/回撤/交易密度/一致性 |
| 性能 | 150次 × 2.9s = 7min | 25000次 × 10ms = 4min |
| 速度提升 | — | **约 290x** |

**V2 新增文件**:

| 文件 | 职责 |
|------|------|
| `config/optimizer_constraints.yaml` | 可配置约束：仓位/回撤/交易密度/一致性 |
| `src/analysis/optimizer_constraints.py` | 约束加载器 + WalkForward/Genetic/Discrete 配置模型 |
| `src/analysis/walk_forward.py` | 数据切片 → 统一日期轴 → 14窗口矩阵 |
| `src/analysis/fast_evaluator.py` | numba JIT 信号生成 + 组合模拟 + 指标计算 |
| `src/analysis/genetic_searcher.py` | 三阶段：随机粗筛(10000) → 遗传3代 → 精确验证 |
| `src/analysis/strategy_optimizer_v2.py` | V2 顶层入口，兼容 V1 OptimizationReport |

**V2 三阶段搜索流程**:
```
Phase 1 (粗筛): 30000 随机策略 → 向量化评估14窗口 → 过滤约束 → Top 10000
Phase 2 (遗传): 5000种 × 5代 × 交叉/变异 → 25000 新策略 → Top N
Phase 3 (验证): 精确 PortfolioEvaluator → Top 10 → YAML + HTML
```

**Walk-Forward 窗口设计** (实际值见 `config/optimizer_constraints.yaml`):
```
数据: 5年 (60个月)
训练 12月 + 测试 9月, 滑动步长 3月, 共 14 窗口
W1: [Train 0-12][Test 12-21]
W2:    [Train 3-15][Test 15-24]
... (每次滑动 3 月, 共 14 窗口)
评分 = mean(14窗口测试超额) - stability_penalty(0.5) × std(14窗口测试超额)
```

**约束系统** (`optimizer_constraints.yaml` — 唯一真源):
- **仓位下限**: 测试期平均持仓 ≥ 5%（`min_avg_position_pct`）
- **回撤上限**: 最大回撤 ≥ -40%（`max_drawdown_pct`）
- **一致性**: 收益标准差 ≤ 15%（`max_return_std_pct`，防单窗口运气）
- **交易密度**: 月交易 1-100 次（`min/max_trades_per_month`）
- 全部硬性过滤，不满足直接丢弃

### V1 优化器 (v1.14, 保留在 `--optimize`)

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

### V1 回测时间线 (`BacktestConfig`)

| 阶段 | 月 | 交易 | 资金注入 | 用途 |
|------|-----|------|---------|------|
| 观察 | 0-6 | 禁止 | 无 | 指标暖机 + pre-filter |
| 部署 | 6-12 | 自由 | A/非A各+20k/月 | 训练目标 |
| 延续 | 12-18 | 自由 | 无 | 外样本延续 |
| 持仓 | 18-24 | 禁止 | 无 | 最终排名依据 |

### V1 条件构建器池

| 买入 | 卖出 | 描述 |
|------|------|------|
| deviation_cross | deviation_cross | MA60 偏离穿越 |
| rsi_signal | rsi_signal | RSI 超卖/超买 |
| bollinger_signal | bollinger_signal | 布林下轨/上轨 |
| volume_spike | deviation_absolute | 放量异动 / MA 绝对偏离 |
| deviation_absolute | trend_follow | MA 绝对偏离 / ADX 反转 |
| trend_follow / none | none | ADX 趋势 / 规则禁用 |

### V1 已知问题
- 回看偏差：150个策略事后挑测试期最好的，类"开卷考试"
- 过拟合：训练期-6.78%，测试期+18.25%，过度依赖回溯
- OR 逻辑：buy_1 和 buy_2 独立触发，不放量也能买
- 卖出无效：优化器频繁选出"只买不卖"

### V1 超额收益

真实收益 − 现金基准收益（A股 rf=2%, 非A rf=4.5%, 日复利 r_f/252）。消除注资虚胖，真实反映交易 Alpha。

### V1 全量运行结果 (2026-04-29)

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

> ⚠️ **v1.18 变更**：日报不再每日重新贪心搜索/回测。改为直接读取最新 YAML（02:00 搜参任务产出）的 Top1 策略预估收益（近 9 个月测试期）。详见本文档「权威设计契约」章节。

### 日报邮件版式 (daily_mode, v1.18)

`_build_email_body(daily_mode=True)` 精简日报，去掉过时/冗余段：
- **删除**：MA60 偏离报警段（`alert_section`）、回测分阶段表（`backtest_section`）、旧组合分析段（`portfolio_section`）、价格表的 MA60/偏离/状态列
- **保留 + 重排**：走势图(收盘价) → 净值图(每组一张) → **搜参策略结果**（Top1规则+今日信号+组合卡片+季末持仓，一段讲完）→ 基本面表 → 公告 → **未解禁定增**
- **今日信号合并**：`SignalScanner.alerts`（A股+港股+美股 merge）并入搜参段，信号名 `buy_1`→`偏离穿越`（`_build_signal_label_map` 读 YAML params 翻译）
- **简报统一**：早/晚简报也走 `SignalScanner`（非 `build_strategy_suggestions` 旧逻辑），与日报信号一致
- 邮件从 ~89k 字符精简到 ~28k

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
| `CHANGELOG.md` | v1.12 → v1.17.1 完整变更历史 |
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
| **v1.16** | **2026-05-03** | **xelatex PDF 日报 + 安全加固 + 开源准备** |
| v1.17 | 2026-05-27 | 简报排序 + 收盘简报 14:30 + 日报 19:00 + debt_ratio 删除 + ROE PB/PE 推导 |
| v1.17.1 | 2026-06-04 | QQ 实时行情 + Eastmoney 删除 + 数据源健康探针 + 优化器 P0 修复 + 布林带列名统一 |
| **v1.18** | **2026-07-09** | **搜参多窗口验证契约 (14窗口/测试9月/验证窗口留出) + 日报直读YAML不重搜参 + 港股/美股独立资金池 + 验证期胜率 + 未解禁定增展示 + daily_mode版式精简 + 简报统一SignalScanner** |

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
**下次审查**: 2026-08-01

---

## 🎯 仓位目标模型 (Position Target Model) — v1.18-beta

### 动机
原有优化器使用 **Fixed-Frac（固定分数）** 交易执行：每次信号触发时，买入 `cash × buy_frac`、卖出 `position × sell_frac`。问题是 `buy_frac`/`sell_frac` 是静态的，无法根据市场信号强弱自适应调整仓位。

新模型将"买多少/卖多少"从固定分数升级为**动态仓位目标**：
1. 聚合全体股票的买卖信号 → **bullish_score** ∈ [0, 1]
2. sigmoid 映射 → **target_position**（目标仓位占 NAV 比例）
3. 每日渐进调仓（最多 ±10%/天），朝目标收敛

### 核心算法
```
每天:
  bullish = aggregate(buy_signals, sell_signals)
           = n_buy_active / (n_buy_active + n_sell_active)
  target  = sigmoid(slope × (bullish - 0.5) × 2 + bias)
  delta   = clamp(target - current_pct, -0.10, 0.10)
  
  delta > 0: 买入（候选=有买入信号的股票，等额分配，均价执行）
  delta < 0: 卖出（所有持仓按比例减持，均价执行）
```

### 参数
| 参数 | 范围 | 含义 |
|------|------|------|
| `position_slope` | 0.5 ~ 10.0 (20 档) | 仓位对信号的敏感度 |
| `position_bias` | -3.0 ~ 3.0 (20 档) | 基准仓位偏移（负数偏保守） |
| `max_daily_adjust` | 0.10 (固定) | 每日最大调仓幅度 |

### 与旧模式对比
| | Fixed-Frac (旧) | Position-Target (新) |
|---|---|---|
| 买入量 | `cash × buy_frac` 固定 | `delta × NAV` 动态 |
| 卖出量 | `position × sell_frac` 固定 | `delta × NAV` 动态 |
| 仓位控制 | 无全局概念 | 有全局目标仓位 |
| 月度上限 | 有 (15000) | **无**（删除了） |
| 每标每日操盘 | 不限制 | **最多 1 次** |
| 执行价格 | 3日6价均值 (buy) / 当日均价 (sell) | 同左 |

### 新增文件/改动
- `src/analysis/fast_evaluator.py`: 
  - `_aggregate_bullish()` — 信号聚合
  - `_sigmoid()` / `_compute_position_target()` — sigmoid 映射
  - `_simulate_position_target_python()` — 每日渐进调仓模拟
  - `FastEvaluator.evaluate_position_target()` — 新评估入口
- `src/analysis/genetic_searcher.py`: `StrategyEncoding` 新增 `position_slope`/`position_bias`（向后兼容）
- `config/optimizer_constraints.yaml`: 新增 `position_model` 段
- `tests/test_fast_evaluator.py`: 10 个新测试（信号聚合、sigmoid 映射、仿真场景）
- `scripts/preview_position_target.py`: 对比预览脚本

### 当前状态
- ✅ 核心算法实现 + 22 个测试全绿
- ✅ 旧 evaluate() 路径不受影响（增量添加，未替换）
- 🔄 预览脚本已部署到服务器（需 `python main.py --once` 先跑一遍数据）
- 📋 TODO: 集成到优化器 V2 搜索流程（`evaluate_position_target` 替代 `evaluate`）

---

## 📋 TODO / Roadmap (v1.18-beta)

| 项目 | 状态 | 说明 |
|------|------|------|
| 多渠道通知统一配置 | 🔄 开发中 | Telegram + 飞书群机器人 Webhook + 邮件，YAML 驱动，NotifierManager 统一入口 |
| 策略优化器 V2 | ✅ 已上线 | Walk-Forward 14窗口 + 遗传搜索 + numba 向量化；02:00 cron 更新 YAML，日报/简报直读 |
| **给定时间段回测工具** | 📋 TODO | 支持自定义起止日期 + 基准对比（如 510300）+ 训练/测试严格分离。可用于审计历史表现、验证策略在特定市场环境下的有效性 |
| 飞书日报原生卡片 | ✅ 已实现 | 飞书日报/告警不再复用邮件 HTML 转 Markdown，改为从 Session/DataFrame 直接生成多张飞书卡片（价格/基本面/技术指标）；飞书不渲染管道表格或 fenced code block，因此表格统一用纯文本等宽显示，避免单卡截断 |
| 飞书核心链路测试 | ✅ 已实现 | `tests/test_feishu_notifier.py` 扩展到 29 个测试，覆盖初始化、传输、日报/告警/简报、多卡分段、DataFrame 采集、告警码、技术字段、纯文本表格、中文宽度、格式化 helper；测试允许 Mock Session 接口但不 Mock 数据对象 |
| 飞书真实数据链路测试 | ✅ 已实现 | `tests/integration/test_feishu_real_data.py` 用最小 config 只放 `601728`，通过 `StockDataFetcher` 真实取数写入 Session，只启 Feishu；默认 patch `_send` 只验证一张价格卡片，设置 `FEISHU_E2E_SEND=1` 时才真实发送 |
| Telegram 交互 Bot | ✅ 已实现 | `main.py --interactive` 启动 Telegram 轮询 Bot，支持 `/help` `/list` `/add` `/remove` `/backtest`；白名单 + 限流安全层；纯 requests 轮询，不添加第三方依赖 |
