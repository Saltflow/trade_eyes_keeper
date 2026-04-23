# 大模型相关设计决策

本文档记录与大模型（LLM）交互相关的所有架构决策、提示工程策略和额度管理方案。

---

## LLM 集成架构

### 数据流

```
公告抓取 (announcement_fetcher.py)
  └── 巨潮资讯网 API 获取公告列表
        └── 内容抓取 (content_fetcher.py)
              └── PDF/HTML 正文提取
                    └── LLM 提取 (llm_analyzer/)
                          ├── 关键信息结构化提取
                          ├── 分红数据提取（送股、派息、除权日）
                          └── 写入 LLM 提取缓存

财报分析 (financial_report_manager.py)
  └── 触发条件判断
        └── LLM 分析 (llm_analyzer/)
              ├── 基本面分析
              ├── 盈利能力评估
              └── 写入分析结果缓存

基本面分析 (data_fetcher.py)
  └── 读取 LLM 提取缓存
        └── cache_manager.get_latest_llm_extraction_for_stock()
```

### 调用额度保护

```yaml
# config.yaml
announcements:
  max_llm_calls_per_run: 30      # 公告提取 LLM 上限

financial_reports:
  max_llm_calls_per_run: 80      # 财报分析 LLM 上限（独立额度）
```

每个功能模块独立计数，防止单一功能耗尽全部额度。

---

## 关键设计决策

### 1. LLM 提取缓存优先

**决策时间**：2026-03-15 (v1.8)

**背景**：移除 akshare 后需要可靠的股息数据来源。

**方案**：
- 主要来源：LLM 从公告正文提取的结构化数据（缓存到 `cache/analysis/`）
- 备用来源：网页爬虫从新浪财经股息页面解析

**理由**：LLM 提取的数据更准确（区分中期/年度分红），且已缓存无需重复调用。

### 2. 公告内容抓取与 LLM 提取分离

**决策时间**：2026-03-07 (v1.6)

**背景**：公告列表只含标题和日期，无法获取具体分红数据。

**方案**：
1. `announcement_fetcher.py`：获取公告列表（轻量 API 调用）
2. `content_fetcher.py`：抓取公告正文（PDF/HTML）
3. `llm_analyzer/`：从正文提取结构化信息

**优势**：各步骤可独立缓存和重试，避免单点故障。

### 3. Session-based 数据模型

**决策时间**：2026-04-15 (v3.0)

**背景**：直接传递 dict 导致字段名混淆（price vs low_price），邮件显示价格为 0。

**方案**：使用 Pydantic `SessionContext` 统一所有数据流：

```python
class SessionContext(BaseModel):
    session_id: str
    stocks_data: Dict[str, StockPriceData]
    alerts: List[AlertStock]
    analysis_results: Dict[str, Dict]
    announcements: Dict[str, List]
    financial_analysis_results: Dict[str, List]
    backtest_results: Optional[List]
    errors: List[str]
```

### 4. 配置驱动的指标计算

**决策时间**：2026-04-18 (v3.1)

**背景**：MA60 硬编码在 web_crawler.py 和 data_fetcher.py 中，新增指标需修改多处。

**方案**：
- `alerts.yaml` 定义所有技术指标锚点
- `technical_indicators.py` 统一计算，从配置读取
- 新增指标只需修改 YAML，无需改代码

---

## 提示工程策略

### 公告信息提取

从公告正文中提取：
- 分红方案（每10股送股/派息/转增比例）
- 除权除息日
- 股权登记日
- 公告类型分类

### 财报分析

分析维度：
- 盈利能力（ROE、净利润增长率）
- 成长性（营收增长、利润增长）
- 分红可持续性（派息比率、自由现金流）
- 估值水平（PE、PB、股息率）

---

## 股息计算架构

**当前状态**：返回最新单次股息
**目标状态**：计算过去 365 天股息总和

| 计算方式 | 说明 | 当前状态 |
|---------|------|---------|
| 最新股息 | 仅返回最近一次派息 | 当前实现 |
| 12 个月总和 | 汇总过去 365 天所有派息 | 待修复 |

关键问题详见 [股息数据回归问题调查报告](../reports/dividend_regression_report.md)。

---

## 相关文档

- [项目关键设计决策 (proj4llm.md)](proj4llm.md) - 完整的架构决策记录
- [缓存设计](../design/cache_design.md)
- [编码审计日志](../development/audit_log.md)
