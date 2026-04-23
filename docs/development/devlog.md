# 开发日志

记录项目主要版本迭代与功能演进。

---

## v1.0 - 基础功能

**日期**：2026-03-01

- 股票日线数据获取（新浪、腾讯、东方财富）
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

---

**相关文档**：
- [编码审计日志](audit_log.md)
- [开发规范](conventions.md)
- [LLM 设计决策](../llm/design_decisions.md)
