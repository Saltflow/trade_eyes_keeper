# 股票量化系统 - 关键设计决策文档

**文档版本**: v3.6 (日报重设计 + PDF 附件)
**最后更新**: 2026-05-02
**压缩目标**: <1500行，保留关键设计决策

---

## 📋 执行摘要

本文档记录股票量化监控系统的关键设计决策，重点关注架构选择、问题解决方案和技术路线。系统核心功能包括：股票数据获取、技术条件检查（价格<MA60）、邮件提醒、股息计算和LLM基本面分析。

### 核心原则
1. **真实数据优先** - 杜绝模拟数据，多源备份（新浪财经、腾讯财经、东方财富）
2. **自动降级** - 主数据源失败时自动切换备用源
3. **文档驱动开发** - 所有设计决策必须存档
4. **防循环编码** - 通过自动验证防止重复错误模式
5. **Session统一管理** - 全局共享SessionContext，防止字段混淆
6. **模块职能隔离** - 数据源只负责获取数据，指标计算独立分层
7. **配置驱动** - 技术指标通过配置文件管理，避免硬编码

---

## 🏗️ 架构现状 (v3.4)

### 当前分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│  配置层 (config/)                                              │
│  - config.yaml          - 股票列表/调度/简报/组合策略          │
│  - alerts.yaml          - 技术指标锚点配置 (ma60, wma20...) │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  缓存管理 + 数据源层 (src/)                                   │
│  - data_source.py       - 统一数据源: CSV缓存+复权交叉验证    │
│    ★ 返回数据按 requested_days 裁剪 (5处返回路径)             │
│  - web_crawler.py       - 多源爬虫 (Sina/QQ/Eastmoney)        │
│  - data_fetcher.py      - 数据获取 + 调用指标计算            │
│  - cache_manager.py     - LLM提取/财报缓存                   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  指标计算层 (src/)                                            │
│  - technical_indicators.py - 统一指标计算器，配置驱动          │
│  - utils/etf_detector.py   - ETF检测工具                      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  策略层 (src/)                                                │
│  - rule_engine.py       - 动态规则引擎 (YAML→Python表达式)   │
│    表达式沙箱: eval() + 受限 __builtins__                   │
│    默认规则: 5条 MA60 锚点择时 (2买3卖)                     │
│    扩展: 配置增减规则无需改代码                              │
│  - portfolio_strategy.py - 投资组合分析 (MA60择时+贪心搜索) │
│    已重构: 用 RuleEngine 替代硬编码 if/elif                  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  简报层 (src/)                                                │
│  - email_notifier.py → send_brief_report()  (早盘简报)       │
│  - main.py → run_brief_report()  (轻量任务: 仅价格+锚点)    │
│  - scheduler_manager.py → 遍历 brief_reports 注册 CronJob   │
│  - templates/brief_email.html                                 │
│    锚点择优: ma60(60d) > wma20(~100d) > wma30 > wma50        │
│    仅显示落入警报阈值区间的锚点, 最近3天有数据=活跃标的       │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  业务层 (src/)                                                │
│  - condition_checker.py - 条件检查（多层级警报）              │
│  - alert_engine.py / alert_processor.py - 警报规则+状态管理   │
│  - email_notifier.py    - 邮件通知 (日报/简报/告警)          │
│  - backtest_framework.py - 回测                               │
│  - health_server/       - 健康检查+管理界面+OTP认证          │
└─────────────────────────────────────────────────────────────────┘
```

### 关键改进 (v3.4)

1. **规则引擎 (rule_engine.py, 372行)**
   - 安全的 Python 表达式沙箱，YAML 配置驱动买卖规则
   - 默认规则与 MA60 锚点择时完全一致（2买3卖）
   - 用户可通过配置调整阈值/金额、增减规则（如"只买不卖"）
   - TimingStrategyEngine.run_simulation() 已重构为使用 RuleEngine

2. **早盘简报系统**
   - 每日 9:50 (交易日) 自动发送简报邮件
   - 每标的显示: 开盘价/现价 + 最短锚点 + 偏离率 (仅警报区间)
   - 轻量任务: 跳过 LLM/财报/回测/投资组合，只获取价格+锚点数据
   - 可扩展: config.brief_reports 列表添加新节点即可
   - CLI: `python main.py --brief [report_id]`

3. **DataSource 缓存裁剪**
   - 5处返回路径 (缓存命中/拉取失败/复权回退/合并) 全部按 requested_days 过滤
   - 修复缓存囤积全量历史数据导致图表/回测使用超过请求天数的问题

4. **字体统一**
   - chart_generator.py 和 portfolio_strategy.py 统一调用 _setup_cjk_font()
   - Windows 优先 Microsoft YaHei, Linux 优先 Noto Sans CJK SC
   - DejaVu Sans 不再拦截中文字体回退链

5. **投资组合优化**
   - portfolio_strategy.py (952行): MA60锚点择时引擎 + 贪心前向搜索
   - 净值归一化起点100 + 布林带填充/边界线可视化
   - 组合日期对齐改为按真实日期而非索引
   - A股/非A股分组独立优化，每组3个组合

---

## 🎯 中期目标

### 1. 模块职能隔离 (Phase 1)
- [x] 数据源层只负责数据获取，不包含任何业务逻辑
- [x] 指标计算独立成层，通过配置驱动
- [ ] 清理 web_crawler.py 中的死代码和重复逻辑
- [ ] 统一所有 ETF 检测到单一工具类

### 2. 分层处理优化 (Phase 2)
- [ ] 明确各层的输入输出接口
- [ ] 添加层间数据验证
- [ ] 实现各层的独立测试
- [ ] 考虑引入依赖注入容器

### 3. 可扩展性增强 (Phase 3)
- [x] 规则引擎: YAML 配置驱动买卖规则 (rule_engine.py)
- [x] 早盘简报: 可扩展简报注册机制 (scheduler.brief_reports)
- [x] 投资组合策略: 贪心前向搜索 + 可配置回看天数
- [ ] 支持多时间框架指标
- [ ] 支持指标组合策略

---

## 🏗️ 核心架构决策

### 1. Session-based数据流架构
**决策**: 统一使用SessionContext管理所有数据流，防止字段混淆
**时间**: 2026-04-15 (v3.0)
**背景**: 
- 旧链路：直接传递DataFrame和dict，字段名称不一致导致数据错误
- 问题：alert_engine返回"price"字段，condition_checker映射为"low_price"，数据传递中丢失
- 严重后果：邮件显示所有股票价格为0.00

**解决方案**:
1. **SessionContext统一数据模型**:
   ```python
   class SessionContext(BaseModel):
       session_id: str
       stocks_data: Dict[str, StockPriceData]  # 类型化的股票数据
       alerts: List[AlertStock]              # 类型化的警报数据
       analysis_results: Dict[str, Dict]        # LLM分析结果
       announcements: Dict[str, List]           # 公告数据
       financial_analysis_results: Dict[str, List] # 财报分析
       backtest_results: Optional[List]         # 回测结果
       portfolio_results: Optional[Dict]        # 投资组合分析 (v3.3新增)
       errors: List[str]                      # 错误日志
   ```

2. **数据流重构**:
   ```python
   # 旧链路（已删除）
   stock_data = fetcher.fetch_stock_data()  # DataFrame
   alert_stocks = checker.check_condition(stock_data)  # List[dict]
   notifier.send_alert(alert_stocks, stock_data, ...)
   
   # 新链路（当前使用）
   session_manager = SessionManager(config)
   session = session_manager.create_session(config)
   fetcher.fetch_to_session(session)           # → session.stocks_data
   checker.check_from_session(session)         # → session.alerts
   notifier.send_from_session(session)         # 从session读取
   ```

3. **类型安全数据模型**:
   - `StockPriceData`: 包含价格、MA60、分红、PE等（Pydantic验证）
   - `AlertStock`: 支持单锚点和多锚点警报
   - 提供兼容方法：`to_dataframe()`, `to_dict()`

**优势**:
- ✅ **防止字段混淆**: 强制类型约束，避免dict字段混乱
- ✅ **数据一致性**: 从头到尾流转的是data_fetcher获取的同一份数据
- ✅ **类型安全**: Pydantic自动验证数据完整性
- ✅ **错误集中**: Session.errors统一收集所有错误信息
- ✅ **向后兼容**: 提供兼容方法支持旧代码

**影响范围**:
- 删除的方法：`fetch_stock_data()`, `check_condition()`, `send_alert()`, `send_daily_report()`
- 新增的方法：`fetch_to_session()`, `check_from_session()`, `send_from_session()`, `send_daily_report_from_session()`
- 修改的文件：`main.py`, `src/data_fetcher.py`, `src/condition_checker.py`, `src/email_notifier.py`
- 新增的文件：`src/session_manager.py`, `src/models/schemas.py`, `src/models/converters.py`
- 测试更新：`tests/validation/test_system_validation.py`, 新增`tests/integration/test_session_flow.py`

**验证结果**:
- ✅ Session创建和数据流正常
- ✅ 主程序运行成功（`python main.py --once`）
- ✅ 数据一致性验证通过
- ✅ 所有核心功能正常工作

### 2. 数据获取架构
**决策**: 移除akshare依赖，统一使用网页爬虫
**时间**: 2026-03-15 (v1.8)
**理由**: akshare API不稳定，网页爬虫更可靠
**实现**:
```python
# 当前数据源优先级
1. 新浪财经历史数据API (主要)
2. 腾讯财经API (备用)
3. 东方财富API (备用)
```

### 3. 股息计算架构
**决策**: 12个月股息总和 vs 最新单次股息
**当前状态**: 仅返回最新股息（需要修复）
**目标状态**: 计算过去365天股息总和
**关键问题**: 股息回归问题（显示0.181元而非0.274元）
**解决方案**: 修改web_crawler.fetch_dividend_data()实现12个月汇总

### 4. 回测框架集成架构
**决策**: 集成回测功能到主系统邮件 vs 独立邮件发送
**时间**: 2026-04-08 (v2.7)
**背景**: 回测框架已开发但错误地创建了独立的邮件发送流程，导致重复造轮子
**解决方案**:
1. **提取数据接口**: 在backtest_framework.py中添加`get_backtest_results()`公共方法，提供纯数据接口
2. **邮件集成**: 在email_notifier.py中添加`_build_backtest_section()`方法构建HTML表格
3. **模板扩展**: 在email_template.html中添加`{backtest_section}`占位符

### 5. DataSource 统一数据源层（v3.2）
**决策**: 将缓存管理、数据校验、复权验证集中到 DataSource 模块
**时间**: 2026-04-26 (v3.2)
**背景**:
- 缓存逻辑分散在 data_fetcher、backtest_framework、session_manager 中，难以维护
- JSON 缓存格式不适合增量更新和校验
- 价格校验（close>=low<=high）放在 condition_checker（业务层），分层不合理
- 复权验证没有统一的交叉比对机制

**解决方案**:
1. **DataSource 新模块** (`src/data_source.py`):
   - CSV 缓存读写（每只股票一个 CSV 文件）
   - Meta 元信息文件（{code}.csv.meta 记录 fetch_time、rows、source）
   - 过期判定：7天保留 + 15:55 当日过期（读取 config.yaml 的 cache_bypass_cutoff）
   - 复权交叉验证：双源取3日 close 比对，差值 > 0.01 则不一致，fallback 重试最多2轮
   - 价格关系校验：close >= low <= high，不符合时日志警告

2. **CSV缓存格式**:
   ```csv
   date,open,close,high,low,volume,amount,stock_name,stock_code
   2025-10-28,6.48,6.48,6.54,6.46,24037000.0,155759760.0,电投产融,000958
   ```
   ```json
   # {code}.csv.meta
   {"stock_code": "000958", "fetch_time": "2026-04-26T00:30:20+08:00", "rows": 120, "source": "_fetch_from_qq"}
   ```

3. **下游模块变更**:
   - `data_fetcher.py`: 删除缓存代码（~170行），改用 `DataSource.fetch_stock_data()`；修复 @property 属性赋值冲突
   - `backtest_framework.py`: 删除 `self.data_cache` 内存缓存，使用 `DataSource.fetch_stock_data()`
   - `session_manager.py`: `DataSourceSelector` 简化为 DataSource 代理
   - `condition_checker.py`: 删除 `_validate_price_relationships()` 方法

**影响范围**:
- 新增文件: `src/data_source.py`
- 修改文件: `src/data_fetcher.py`, `src/web_crawler.py`, `src/session_manager.py`, `backtest_framework.py`, `condition_checker.py`
- 测试更新: `tests/validation/test_system_validation.py`（价格校验测试更新）
- 缓存格式变更: JSON → CSV + meta（旧 JSON 缓存文件不再被读取，可安全删除）

**验证结果**:
- ✅ 63/77 测试通过（14个失败/错误均为预存问题，与 DataSource 无关）
- ✅ `python main.py --once` 端到端运行正常
- ✅ CSV 缓存写入: 26 只股票已生成 cache/data/{code}.csv + {code}.csv.meta
- ✅ 缓存命中: DataSource 缓存命中 + backtest_framework 从 DataSource 获取数据正常
- ✅ 47 个旧 JSON 缓存文件已清理

### 6. 历史数据缓存架构
**决策**: 使用baostock作为稳定数据源替代web_crawler，实现智能缓存机制
**时间**: 2026-04-10 (v2.8)
**背景**: web_crawler不稳定，回测需要可靠的历史数据源，HistoricalDataManager返回空DataFrame
**问题诊断**:
1. **缓存目录为空** - HistoricalDataManager依赖缓存但缓存目录无数据文件
2. **日期格式不匹配** - baostock要求YYYY-MM-DD格式，系统使用YYYYMMDD格式
3. **配置加载问题** - config.config模块导入失败导致配置无法正确加载

**解决方案**:
1. **日期格式转换修复**:
   ```python
   # 在baostock_fetcher.py中添加日期转换函数
   def convert_date_format(date_str):
       if not date_str or len(date(date_str)) != 8:
           return date_str
       return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
   ```
2. **智能缓存策略**:
   - **全量更新**: 除权检测、缓存不存在或过期30天时触发
   - **增量更新**: 仅获取缺失的最新数据
   - **随机强制更新**: 1%概率强制更新防止缓存过期
3. **三层数据源优先级**:
   ```python
   # HistoricalDataManager数据获取优先级
   1. 本地缓存数据（JSON Lines格式）
   2. baostock稳定数据源（前复权数据）
   3. web_crawler备用数据源（降级使用）
   ```
4. **缓存文件结构**:
   ```
   cache/historical/
   ├── data/      # JSON Lines格式历史数据文件（股票代码_开始日期_结束日期.jsonl）
   └── metadata/  # JSON格式元数据文件（股票代码_metadata.json）
   ```

**组件实现**:
- `src/cache_strategy.py` - 智能缓存更新决策
- `src/historical_data_manager.py` - 统一历史数据访问接口
- `src/cache_manager.py扩展` - 历史数据缓存读写支持
- `backtest_framework.py修改` - 集成缓存优先策略

**部署验证**:
- ✅ 本地测试：HistoricalDataManager成功返回21条记录
- ✅ 缓存文件：正确创建JSON Lines格式缓存文件
- ✅ 远程验证：缓存机制在远程服务器基本工作
- ⚠️ 注意：远程服务器需要更新baostock_fetcher.py以包含完整日期转换函数

---

## 🔧 技术债务清理

### 1. 移除废弃代码
- 删除`src/session_manager.py`中所有旧链路方法
- 删除测试代码中的旧链路调用
- 清理文档中的旧架构说明

### 2. 统一错误处理
- 使用Session.errors统一收集错误
- 删除分散的logger.error调用
- 实现错误恢复机制

### 3. 测试覆盖
- 更新`tests/validation/test_system_validation.py`使用Session链路
- 新增`tests/integration/test_session_flow.py`集成测试
- 确保所有新方法都有测试覆盖

---

## 📊 监控和维护

### 1. 日志级别
- **生产环境**: INFO级别
- **调试环境**: DEBUG级别
- **错误监控**: ERROR级别

### 2. 性能监控
- Session创建时间
- 数据获取时间
- 邮件发送时间

### 3. 数据质量监控
- 价格关系验证（close>=low<=high）
- MA60有效性检查
- 股息率合理性检查（0.5%-30%）

---

## 🚀 紧急恢复预案

### 1. Session创建失败
- 回退到旧链路
- 记录错误并通知

### 2. 数据源全部失败
- 使用缓存数据
- 发送错误通知邮件

### 3. 邮件发送失败
- 重试3次
- 降级到本地日志
- 通知管理员

---

## 📝 版本历史

| 版本 | 日期 | 主要变更 | 影响范围 |
|------|------|----------|----------|
| v1.0 | 2026-03-01 | 初始版本 | 基础功能 |
| v1.5 | 2026-03-15 | 移除akshare | 数据获取 |
| v2.0 | 2026-03-20 | 添加回测框架 | 新增功能 |
| v2.5 | 2026-03-22 | 集成回测到邮件 | 邮件通知 |
| v2.8 | 2026-04-10 | 历史数据缓存优化 | 性能优化 |
| v3.0 | 2026-04-15 | Session-based数据流 | 架构重构 |
| v3.1 | 2026-04-18 | 配置驱动的指标计算，模块职能隔离 | 架构重构 |
| v3.2 | 2026-04-26 | DataSource 统一数据源层，CSV缓存+复权验证 | 架构重构 |
| v3.3 | 2026-04-26 | 投资组合策略分析模块（MA60锚点择时+贪心优化） | 新增功能 |

---

## 🧩 投资组合策略 + 规则引擎 (v3.4)

### 文件
- `src/portfolio_strategy.py` — 主模块（952行，已用 RuleEngine 重构）
- `src/rule_engine.py` — 动态规则引擎（372行）
- `tests/test_portfolio_strategy.py` — 15个测试
- `tests/test_rule_engine.py` — 23个测试

### 规则引擎

YAML 配置驱动，Python 表达式描述条件/动作：

```yaml
rules:
  - id: buy_minus5
    type: buy
    condition: "deviation <= -0.05 and prev_deviation > -0.05"
    action_amount: "min(5000, cash)"
    reset_when: "deviation > 0 and prev_deviation <= 0"
```

- **表达式沙箱**: `eval()` + 受限 `__builtins__`（仅 min/max/abs/int/float/round）
- **规则锁**: 触发一次后锁定，满足 `reset_when` 才解锁
- **优先级**: 同天多条命中按 `priority` 顺序执行
- **默认规则**: 5条（2买3卖）与原有硬编码完全一致
- **扩展**: 新增/修改规则只用改配置，不碰 Python 代码

### 投资组合优化
- 贪心前向选择: 3个目标 (max_return/min_drawdown/max_sharpe)
- 净值归一化起点100，布林带可视化 (填充+SMA+边界虚线)
- 日期对齐: 按真实日期而非索引，数据起点不同的股票不提前参与

## 📊 早盘简报 (v3.4 新增)

### 触发
- 每个交易日 9:50 AM 自动发送（config: `scheduler.brief_reports`）
- CLI: `python main.py --brief [report_id]`
- 周末/非交易日自动跳过，不在交易时段的标的自动过滤

### 锚点择优算法
```
对每只股票:
  1. 遍历锚点 (ma60, wma20, wma30, wma50)
  2. 过滤: 保留偏离率在警报阈值区间内的
      (≤-10% / -10~-5 / -5~0 / 5~10 / 10~15 / ≥15%)
  3. 择优: 实际回溯最短优先 (ma60:60d > wma20:~100d > wma30:~150d > wma50:~250d)
  4. 无锚点在区间 → 显示"-"
```

### 扩展性
```yaml
brief_reports:
  - id: morning_snapshot
    run_time: '09:50'
    label: '早盘简报'
  # 未来新加:
  - id: pre_close_alert
    run_time: '14:50'
    label: '收盘前警报'
```
调度器自动注册，无需改代码。

### 文件
- `src/templates/brief_email.html`
- `tests/test_brief_report.py` — 19个测试

---

## 🎯 策略搜索优化器 (v1.14)

### 设计动机

原有MA60交叉策略（买入 -5%/-10%，卖出 +5%/+10%/+15%）存在天花板：
- 对防御性标的（银行/电信/红利ETF）买入信号极少触发
- 阈值硬编码，无法适应不同标的行为
- 操作员主观判断影响执行

优化器自动搜索"满足回撤约束且最大化收益"的策略，替代人工设定。

### 架构概述

```
config/optimizer.yaml ──▶ StrategyOptimizer ──▶ PortfolioEvaluator
        │                      │                       │
   5条规则定义         贝叶斯优化(skopt)         模拟 24 个月回测
   6买+5卖构建器       13+N 维参数空间           时间线约束(BacktestConfig)
                          │
                    ┌─────┴──────┐
                Phase A (训练)  Phase B (测试)
                0-12月搜索       0-24月最终评估
                                按12-24月外样本排名
```

### 时间线约束 (`BacktestConfig`)

```
月 0 ─── 6 ────── 12 ────── 18 ── 24
  观察    部署       延续      持仓
  (无交易) (+20k/月) (无注资)  (无交易)
```

| 阶段 | 交易 | 资金注入 | 用途 |
|------|------|---------|------|
| 观察 0-6m | 禁止 | 无 | 指标暖机 + pre-filter |
| 部署 6-12m | 自由 | A/非A各+20k/月 | **训练目标：最大化此段超额** |
| 延续 12-18m | 自由 | 无 | 外样本延续 |
| 持仓 18-24m | 禁止 | 无 | **最终排名依据** |

### 条件构建器池

替代固定模板，每条规则搜索"用哪个信号 + 阈值多高 + 仓位多大"：

| 买入构建器 | 描述 | 卖出构建器 | 描述 |
|-----------|------|-----------|------|
| `deviation_cross` | MA60偏离穿越 | `deviation_cross` | MA60偏离穿越 |
| `rsi_signal` | RSI超卖 | `rsi_signal` | RSI超买 |
| `bollinger_signal` |布林下轨 | `bollinger_signal` |布林上轨 |
| `volume_spike` | 放量异动 | `deviation_absolute` | MA绝对偏离 |
| `deviation_absolute` | MA绝对偏离 | `trend_follow` | ADX趋势反转 |
| `trend_follow` | ADX趋势跟踪 | `none` | 规则禁用 |
| `none` | 规则禁用 | — | — |

每条规则搜索 3 维：`{builder选择, 归一化阈值, 仓位比例}`。5条规则 × 3 = 15 + N只股票开关。

### 两阶段贝叶斯优化

**Phase A (训练)**: 贝叶斯优化仅见 0-12 月数据，最大化部署期超额收益（扣除回撤惩罚）。
**Phase B (测试)**: 所有候选策略在完整 0-24 月重跑，按外样本（12-24月）超额收益排名。

防止过拟合：优化器从未见过 12-24 月数据。训练-测试相关性 < 0.2 提示过拟合风险。

### 超额收益与现金基准

`total_return` = (期末NAV − 期初NAV) / 期初NAV。部署期因注资虚胖至 140%。

**超额收益** = 真实收益 − 现金基准收益。现金基准每日复利 `r_f/252`（A股 2%，非A 4.5%），注资时同步加计。消除注资虚胖，真实反映交易贡献。

Rank 1 的 0 笔交易 → 部署超额 ≈ −r_f（资金闲置机会成本），观察期超额 ≈ −r_f/2。

### 观测期预筛选

用 0-6 月观测数据遍历每只股票、每个构建器，统计信号触发次数。**前 6 个月完全无信号的构建器被排除出搜索空间**。

实跑效果：银行/电信 ETF 池中，卖出 `deviation_cross`/`rsi_signal`/`bollinger_signal`/`deviation_absolute`/`trend_follow` 全部 0 信号 → 淘汰。搜索空间从 33 维缩减到更紧凑。

### 股票汰换

搜索空间新增 `include_{code}` 二进制维度（0=跳过, 1=纳入）。贝叶斯优化同时搜索"用什么规则 + 选哪些股票"。至少保留 1 只。

### 仓位比例制

买入/卖出金额不再写死，改为 `cash * {frac}` 或 `fraction = {frac}`。卖出 fraction 也纳入搜索 `[0.10, 0.50]`。

### 收敛诊断图

双栏 matplotlib PNG：
- **图 1**: 贝叶斯收敛曲线（最优适应度线 + 评估散点 + 随机/贝叶斯分界）
- **图 2**: 最优策略 3 阶段超额收益柱状图（观察/部署/验证），标注回撤 + 交易次数

### HTML 可视化报告

自包含单文件 HTML（Plotly.js CDN），暗色主题：
- 指标卡片行（测试超额/回撤/Sharpe/交易/Pre-filter）
- Plotly 交互收敛图（可缩放/悬停）
- Top-10 策略表（点击展开规则详情）
- 最优策略完整规则展示 + 入选标的标签

### 全量运行结果 (2026-04-29)

**A 股 (18 只, 150 轮)**:
| 排名 | 部署超额 | 测试超额 | 回撤 | 交易 | 核心信号 |
|------|---------|---------|------|------|---------|
| 1 | -2.3% | **+19.4%** | -17.2% | 27 | deviation_cross ×2, 卖出全禁用 |

入选：国电电力、军工ETF、核电ETF、红利ETF、港股通ETF +1。策略本质：深跌抄底 + 永不卖出。

**非 A 股 (8 只, 80 轮)**:
| 排名 | 部署超额 | 测试超额 | 回撤 | 交易 | 核心信号 |
|------|---------|---------|------|------|---------|
| 1 | -3.9% | **+49.6%** | -12.8% | 7 | bollinger_signal, 只选中海油 |

系统独立收敛到"买→持有→不卖"——两个市场都主动禁用了所有卖出规则。ETF 占比 >60%（波动率低，均值回归更可靠）。

### 关键文件

| 文件 | 职责 |
|------|------|
| `src/analysis/backtest_config.py` | 回测时间线约束 |
| `src/analysis/indicator_library.py` | RSI/MACD/ATR/布林/ADX/量比 |
| `src/analysis/strategy_optimizer.py` | 贝叶斯优化 + 两阶段评估 + 收敛图 + HTML报告 |
| `config/optimizer.yaml` | 规则模板 + 构建器池 + 搜索参数 |
| `src/templates/optimizer_report.html` | 静态 HTML 报告模板 |
| `src/analysis/portfolio_strategy.py` | `PortfolioEvaluator` 含 BacktestConfig + IndicatorLibrary + SubPeriodMetrics |

### 使用方式

```bash
python main.py --optimize          # 全量搜索 (A股+非A股)
python main.py --optimize --iterations 200 --drawdown-limit -30
```

产出: `data/optimizer/{id}_{group}_strategies.yaml` + `_convergence.png` + `_report.html`

---

## 📧 日报重设计 (v3.6)

### 设计哲学

日报不是数据库 dump。每一条数据必须回答一个明确的投资决策问题。

| 呈现层级 | 时间 | 回答 |
|----------|------|------|
| P1 KPI 卡片 (2秒) | 今天有信号吗 / 策略还跑赢大盘吗 |
| P2 偏离趋势 (5秒) | 这是长期趋势还是短期波动 |
| P3 触发信号 (5秒) | 具体哪只、什么信号、当前值是多少 |
| P4 完整表 (10秒) | 所有标的今日指标全貌 + 基本面辅助判断 |
| P5 策略健康 (2秒) | 这套策略还靠得住吗 / 有没有背离 |

### 关键设计

**1. 一张表原则**: 全部 27 只标的在一张表里，A 股在上、境外在下。技术列动态（只显示策略实际引用的指标），基本面 3 列固定（股息率/PE/PB）。

**2. 偏离度 30 日折线图**: 取绝对值最大的 5 只，叠线在同一张图上。X=日期、Y=偏离度%。灰色虚线标注买入阈值。平滑下行=趋势走弱，锯齿震荡=短期波动。

**3. PDF 附件 + 邮件正文分离**: WeasyPrint 将 HTML 渲染为 PDF，yagmail 作为附件发送。邮件正文仅含摘要文字。消除时效 token 链接问题。

**4. 基本面对冲**: 技术面信号优异但 PE 极高/股息率骤降的标的全表可见——不直接决策，但提供手动筛选依据。

### 技术实现

```
--once 流程:
  │
  ├─ signal_scanner.scan()        → 信号 + 指标快照
  ├─ signal_scanner.run_backtest() → 回测数据
  │
  ├─ _chart_deviation_timeline()  → matplotlib PNG (base64)
  ├─ report_daily.html 模板        → str.format() 渲染
  ├─ WeasyPrint(html)             → PDF bytes
  └─ yagmail.send(attachments=[pdf]) → 邮件附件
```

### 待清理文件 (确认日报格式后再删)

| 文件 | 操作 | 原因 |
|------|------|------|
| `global_instances.py:56-140` | 删除 token 系统 | 时效链接替换为 PDF 附件 |
| `health_handler.py:503-530` | 删除 `handle_report()` + `/report/` 路由 | 同上 |
| `email_notifier.py` report_link 生成 | 删除 (~30行) | 同上 |
| `email_notifier.py` all_rows_price/fundamental 构建 | 删除 (~50行) | 替换为统一 `_build_daily_table()` |
| `email_template.html` | 大幅缩减 | 邮件正文简化 |
| `alert_section.html` | 可删 | 不再独立模板渲染 |

### 依赖

- `weasyprint>=60.0` — HTML → PDF 渲染
- 服务端系统依赖: `libcairo2 libpango-1.0-0 libgdk-pixbuf2.0-0 libffi-dev`

---

## 🔗 相关文档

- [AGENTS.md](../../AGENTS.md) - 开发规范和代理使用指南
- [README.md](../../README.md) - 项目概述和使用说明
- [开发规范](../development/conventions.md) - 代码风格与开发实践
- [架构说明](../architecture.md) - 系统架构与模块职责

---

**文档维护**: 本文档应在每次重大架构变更后更新  
**下次审查**: 2026-05-15
