# 开发日志

记录项目主要版本迭代与功能演进。

---

## v1.0 - 基础功能

**日期**：2026-03-01

- 股票日线数据获取（新浪、腾讯）
- MA60 条件检查（最低价 < MA60）
- 邮件提醒（yeah.net SMTP + SSL）
- APScheduler 定时任务

## v1.1 - 邮件稳定性

**日期**：2026-03-02

- 修复 yeah.net SMTP 超时：改用端口 465 SSL 连接
- 支持邮箱授权码认证
- HTML 格式邮件表格

## v1.2 - 历史数据准确性

**日期**：2026-03-03

- 集成新浪财经真实历史数据 API（120 天）
- 前复权价格处理
- 换手率、振幅等字段补全

## v1.3 - LLM 基本面分析

**日期**：2026-03-05

- 接入 DeepSeek API
- 自动分析基本面、盈利、分红情况
- 结构化输出

## v1.4 - 缓存与可读性

**日期**：2026-03-07

- 每日缓存机制（避免重复请求）
- 缓存完整性验证
- 邮件表格拆分：价格技术指标 + 基本面指标
- 颜色区分正负值
- 安全改进：敏感信息移出配置文件，完全依赖环境变量
- 测试重组：unit / integration / api 三层结构

## v1.5 - 邮件存档与公告增强

**日期**：2026-03-07

- 邮件 HTML 副本自动存档到 `data/email_archive/`
- 公告获取错误处理优化
- 公告邮件显示修复

## v1.6 - 官方公告系统升级

**日期**：2026-03-07

- 切换到巨潮资讯网 (cninfo) 官方公告 API
- 支持 30+ 种公告类型（分红、停牌、年报等）
- 股息详情提取（送股、派息、除权日）
- 中文编码问题修复
- ETF 自动跳过

## v1.7 - 代码清理

**日期**：2026-03-08

- 删除 16 个未使用函数
- 更新 10 个测试文件
- 删除冗余文档（proj_short.md、proj_compressed.md）
- 保持核心功能完整

## v1.8 - 架构清理（akshare 移除）

**日期**：2026-03-15

- 移除 akshare 依赖（不稳定）
- 分红数据架构重构：LLM 提取缓存优先，网页爬虫备用
- 修复 scheduler_manager 与 main.py 循环导入
- 缓存管理器新增 `get_latest_llm_extraction_for_stock()`

## v2.0 - 回测框架

**日期**：2026-03-20

- 新增 backtest_framework.py
- 支持多策略回测

## v2.5 - 回测集成到邮件

**日期**：2026-03-22

- 提取 `get_backtest_results()` 纯数据接口
- 邮件模板扩展 `{backtest_section}`
- 避免重复邮件发送流程

## v2.7 - 回测邮件集成优化

**日期**：2026-04-08

- 明确回测数据接入邮件报表的集成方案
- 统一 HTML 表格构建风格

## v2.8 - 历史数据缓存

**日期**：2026-04-10

- 引入 baostock 作为稳定历史数据源
- 智能缓存：除权全量更新 / 日常增量更新
- 日期格式转换修复（YYYYMMDD -> YYYY-MM-DD）
- 三层数据源优先级（本地缓存 -> baostock -> web_crawler）

## v3.0 - Session 架构重构

**日期**：2026-04-15

- 引入 `SessionContext` 统一数据流（Pydantic 模型）
- 解决字段混淆问题（如 price -> low_price 映射错误导致价格为 0）
- 新链路：`fetch_to_session` -> `check_from_session` -> `send_from_session`
- 向后兼容：保留 `to_dataframe()` / `to_dict()` 适配方法

## v3.1 - 配置驱动的指标计算

**日期**：2026-04-18

- `technical_indicators.py` 独立为指标计算层
- `web_crawler.py` / `data_fetcher.py` 不再计算 MA60
- 所有技术指标通过 `alerts.yaml` 配置驱动
- `utils/etf_detector.py` 统一 ETF 检测逻辑

## v3.2 - DataSource 统一数据源

**日期**: 2026-04-26

- CSV 缓存替代 JSON: 每只股票独立 `.csv` + `.csv.meta`
- 复权交叉验证: 双源 3 日 close 比对
- 价格关系校验: close >= low <= high
- 缓存裁剪: 5 处返回路径按 requested_days 过滤

## v3.3 - 投资组合策略 + 规则引擎

**日期**: 2026-04-26

- `portfolio_strategy.py` (952行): MA60 锚点择时 + 贪心前向搜索
- `rule_engine.py` (372行): YAML 驱动规则, 表达式沙箱, 规则锁
- 净值归一化起点 100, 布林带可视化, 日期对齐

## v3.4 - 早盘简报

**日期**: 2026-04-27

- 每日 09:50 自动发送, 周末自动跳过
- 锚点择优: MA60(60d) > WMA20(~100d) > WMA30 > WMA50
- 轻量任务: 仅价格 + 锚点, 跳过 LLM/财报/回测

## v1.14 - 策略搜索优化器

**日期**: 2026-04-29

- 贝叶斯优化 (skopt) 自动搜索最优策略
- 6买5卖条件构建器池: RSI/MACD/ATR/布林/ADX/量比
- 两阶段 (训练 0-12m / 测试 12-24m), 超额收益 + 现金基准
- 观测期预筛选, 股票汰换, 仓位比例制
- 收敛诊断图 + Plotly HTML 交互报告

## v1.15 - 信号扫描器 + 日报集成

**日期**: 2026-05-01

- SignalScanner: Top-5 策略共识, 每日信号评估
- 回测嵌入日报: `run_backtest()` 完整 24 月历史
- HTML 报告: health_server `/report/<token>` 时效链接
- 三层防御闸门: pre-commit + CI/CD + import smoke
- 安全测试: 18 个 (沙箱/路径遍历/token/OTP)
- CI/CD 路径环境变量化, 简报 cron 自动注册

## v1.16 - xelatex PDF 日报 + 安全加固 + 开源

**日期**: 2026-05-03

- WeasyPrint (644KB) → xelatex LaTeX (145KB): 真数学公式排版
- 公式方法论附录: 13节 amsmath 环境
- ctexart → article + xeCJK (服务器未安装 ctex)
- CRLF 修复: `\r\n` 导致 xelatex `^^M` Emergency stop
- OTP 安全: `secrets.randbelow()` + 审计日志脱敏
- Health server SSL/TLS 自签名证书
- BSD-3-Clause LICENSE + 投资免责声明
- 65 核心 + 18 安全 + 24 导入测试
- 邮件链接 IP/HTTPS 修复

## v1.17 - 简报增强 + 数据清理 + 调度调整

**日期**: 2026-05-27

- 收盘简报: 每日 14:30 `afternoon_snapshot` 自动发送
- 简报排序: 锚点偏离率升序，跌幅越大越靠前
- Cache bypass 回归修复: 恢复 `_should_bypass_cache()`
- debt_ratio 全链路删除: `items[52/53]` 映射错误
- ROE 推导计算: `PB/PE × 100`，误差 <0.2%
- 日报时间: 16:00 → 19:00
- 简报崩溃修复: `UnboundLocalError: dev_color`

## v1.17.1 - 数据源清理 + 优化器修复

**日期**: 2026-06-04

- QQ 实时行情接入: `fetch_realtime_quote()` 盘中简报数据刷新
- Eastmoney 全链路删除: 从 4 处降级路径彻底移除
- 数据源健康探针: 14 个 smoke 测试覆盖 A/港股/ETF
- 优化器 P0 修复: `best_params: dict` 类型放宽
- 布林带列名统一: `boll_pb` → `boll_pct_b`
- 简报锚点兜底: 无有效锚点时 fallback 到 ma60
- 非A股名称修复: NaN 时显示默认代码

---

**相关文档**：
- [编码审计日志](audit_log.md)
- [开发规范](conventions.md)
- [LLM 设计决策](../llm/design_decisions.md)
