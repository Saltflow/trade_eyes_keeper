# Changelog

## v1.17.1 (2026-06-04)

### Added
- **QQ 实时行情接入** (`fetch_realtime_quote`): 解决盘中简报数据不刷新，腾讯 API 全市场支持
- **数据源健康探针** (`tests/test_data_source_health.py`): 14 个 smoke 测试覆盖 A/港股/ETF 实时+历史+估值
- **集成测试** (`tests/integration/`): 3 个端到端测试验证优化器、信号扫描器、报告生成不崩溃
- **简报锚点兜底**: `_pick_best_anchor=None` 时 fallback 到 `ma60`，防止 `UnboundLocalError`

### Fixed
- **优化器 P0 Crash** (`strategy_optimizer.py`): `best_params: dict[str,float]` → `dict`，兼容空/字符串值
- **布林带列名统一**: `boll_pb` → `boll_pct_b`，全链路对齐（指标计算 → 优化器 → 扫描器 → 邮件）
- **Eastmoney 降级链删除**: 从全部 4 处降级路径移除（历史 K 线、实时行情、估值、基本面）
- **估值双源降级**: QQ(全市场 PE) → Yahoo(PB + 非A PE)，含重试 + `.SS`/`.SZ` 后缀修复
- **非A股名称显示**: NaN 时显示默认代码而非空字符串
- **Eastmoney 静默失败**: 删除假实现 WARNING 日志，减少噪音

### Changed
- **数据源降级链**: 新浪 → 腾讯 → Yahoo（东方财富完全下线）

---

## v1.17 (2026-05-27)

### Added
- **收盘简报** (`afternoon_snapshot`): 每日 14:30 自动发送，与早盘简报共用同一渲染逻辑
- **简报排序**: 按锚点偏离率升序排列，跌幅越大越靠前；无锚点股票排最后

### Fixed
- **Cache Bypass 回归**: 恢复 `_should_bypass_cache()`，15:55 后非当日缓存强制刷新（per-stock 粒度）
- **ROE 数据源错误**: 腾讯 API `items[52/53]` 实为动态/静态 PE，原 ROE/debt_ratio 映射完全错误
- **简报崩溃**: `UnboundLocalError: dev_color` 当股票无有效锚点时触发，导致 09:50 简报邮件中断

### Changed
- **日报时间**: `scheduler.run_time` 16:00 → 19:00（确保港股 16:00、美股隔夜数据完整）
- **ROE 计算**: 改为 `PB/PE × 100` 推导，与财报披露值误差 <0.2%
- **CI/CD cron**: 自动注册 09:50 早盘 / 14:30 收盘 / 19:00 日报 / 02:00 优化

### Removed
- **`debt_ratio` 全链路删除**: schemas、data_fetcher、web_crawler、email_notifier、模板、LLM analyzers

---

## v1.16 (2026-05-03)

### Added
- **xelatex PDF 日报**: WeasyPrint→xelatex 真 LaTeX 编译, 港式财报风格, PDF 145KB
- **公式方法论附录**: `appendix_methodology.md` 13节数学公式, xelatex 排版
- **投资免责声明**: LICENSE 追加金融免责章节 (非投资建议/不保证收益/自负风险)
- **OTP 安全加固**: `secrets.randbelow()` 替代 `random.randint` (密码学安全)
- **审计日志脱敏**: 不再记录明文 OTP, 仅显示 `"ID:****"`
- **Health Server SSL/TLS**: 自签名证书 + `ssl.wrap_socket()` + `public_ip` 配置
- **三层防御闸门**: pre-commit hook + CI/CD pre-deploy + import smoke test
- **安全测试**: `test_security.py` 18个测试 (沙箱/路径遍历/token/OTP/速率限制)
- **核心模块测试**: 65个 (backtest_config 15 + indicator_library 18 + signal_scanner 14 + optimizer 18)
- **CI/CD 简报 cron**: 自动注册 09:50 daily crontab
- **BSD-3-Clause LICENSE** + `pyproject.toml` + `CONTRIBUTING.md`

### Fixed
- **xelatex CRLF**: Windows git 自动 `\r\n` 导致 xelatex `^^M` Emergency stop → `.replace("\r\n","\n")`
- **ctexart 未安装**: 服务器缺少 ctex 宏包 → `article` + `xeCJK` 类替代
- **邮件链接 IP**: `hostname -I` 返回 Docker 网桥 IP → `public_ip` 配置 + `ifconfig.me` fallback
- **邮件链接 HTTPS**: `ssl.wrap_socket()` 支持自签名证书
- **Health server 路径**: 5处 `.parent.parent.parent` 路径回归修复
- **33 stale test import**: import 路径/方法名修复

### Changed
- **日报 PDF**: WeasyPrint (644KB) → xelatex (145KB), 真 LaTeX 数学排版
- **CI/CD**: 路径环境变量化, texlive 自动安装, pre-deploy 检查
- **版本号**: v1.15 → v1.16

### Removed
- `report_daily.html` (WeasyPrint 模板, 已被 `report_daily.tex` 替代)
- `alert_section.html` (不再独立渲染)

---

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
