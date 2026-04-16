# 股票量化系统 - 关键设计决策文档

**文档版本**: v3.0 (Session-based数据流)
**最后更新**: 2026-04-15
**压缩目标**: <1000行，保留关键设计决策

---

## 📋 执行摘要

本文档记录股票量化监控系统的关键设计决策，重点关注架构选择、问题解决方案和技术路线。系统核心功能包括：股票数据获取、技术条件检查（价格<MA60）、邮件提醒、股息计算和LLM基本面分析。

### 核心原则
1. **真实数据优先** - 杜绝模拟数据，多源备份（新浪财经、腾讯财经、东方财富）
2. **自动降级** - 主数据源失败时自动切换备用源
3. **文档驱动开发** - 所有设计决策必须存档
4. **防循环编码** - 通过自动验证防止重复错误模式
5. **Session统一管理** - 全局共享SessionContext，防止字段混淆

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

### 5. 历史数据缓存架构
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

---

## 🔗 相关文档

- [AGENTS.md](./AGENTS.md) - 开发规范和代理使用指南
- [README.md](./README.md) - 项目概述和使用说明
- [pytest.ini](./pytest.ini) - 测试配置

---

**文档维护**: 本文档应在每次重大架构变更后更新  
**下次审查**: 2026-05-15
