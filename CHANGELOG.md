# Changelog

## v1.15 (2026-05-01)

### Added
- **策略信号扫描器** (`SignalScanner`): 加载最新优化结果,计算Top-5策略共识,每日对当日数据评估买入信号
- **回测分析**: `run_backtest()` 用最新优化策略跑完整24月回测,结果嵌入日报
- **观测期预筛选**: 用0-6月数据淘汰无信号构建器,减少搜索空间
- **现金基准计息**: 超额收益扣除无风险利率(A股2%/非A4.5%),准确衡量交易贡献
- **三层防御闸门**: pre-commit hook + CI/CD pre-deploy checks + import smoke test
- **HTML报告时效链接**: health_server `/report/<token>` 路由,30分钟有效
- **基准对比线**: 510300(沪深300)+510880(红利ETF)满仓持有策略

### Fixed
- 早盘简报调度器从未注册 (需要重启scheduler)
- health_server 5处路径回归 (f9a0f91重组后Path计算错误)
- 17个测试 stale import 崩溃
- HTML报告card值不渲染、Plotly charts加载失败

### Changed
- `SchedulerManager` 改为 `brief_function` 参数传入 (替代 `from main import`)
- `CI/CD` 路径环境变量化 (`DEPLOY_HOST`, `DEPLOY_REMOTE_DIR`)
- `CI/CD` 添加简报 cron (09:50 daily)
- `CI/CD` 部署前检查: ruff lint + import smoke + core tests

### Removed
- `colorlog` 死依赖 (零import)
- `camelot` PDF表格解析死代码 (从未安装,永远静默失败)

---

## v1.14 (2026-04-29)

### Added
- **策略搜索优化器** (`StrategyOptimizer`): 贝叶斯优化自动搜索最优策略参数
  - 条件构建器池 (6买5卖): RSI/布林/MACD/ADX/量比/趋势跟踪
  - 两阶段优化: 训练(0-12月) + 测试(12-24月)
  - 股票汰换: `include_{code}` 二进制维度
  - 仓位比例制: `cash * {frac}` 代替固定金额
- **回测约束** (`BacktestConfig`): 观察/部署/持仓三阶段时间线 + 资金注入计划
- **指标库** (`IndicatorLibrary`): RSI/MACD/ATR/Bollinger/ADX/量比纯pandas计算
- **收敛诊断图**: 贝叶斯收敛曲线 + 3阶段超额收益柱状图
- **超额收益**: 现金基准线隔离注资贡献,真实反映交易Alpha

### Fixed
- 下线简化回测框架 (`backtest_framework.py`)
- 投资组合策略解耦独立运行

---

## v1.13 (2026-04-27)

### Added
- **规则引擎** (`RuleEngine`): YAML驱动,表达式沙箱,23测试
- **早盘简报** (`--brief`): 轻量价格+锚点快照,09:50 cron调度
- **投资组合策略** (`PortfolioEvaluator`): 共享资金池模拟,贪心前向选择
- **跨平台字体** (`font_setup.py`): Windows Microsoft YaHei / Linux Noto Sans CJK SC

### Fixed
- 股息计算: 明确"最近12个月总和"语义
- 缓存裁剪: 5处返回加日期过滤
- 图表毛刺: 缺数据天用last_prices代替0
- 日期排序: sorted(date_map.items()) 消除折返线

---

## v1.12 及更早

- Phase1 src/ 目录重组 (按数据流分层)
- 多锚点报警系统 (MA60/WMA20/WMA30/WMA50)
- LLM基本面分析 + 财报分析
- 公告获取 + 股息提取
- 健康服务器 (OTP认证,管理页,监控列表在线编辑)
