# 官方行业历史补全

## 来源

中国上市公司协会公开了按股票代码排序的半年度行业分类 PDF：

- 2023 上半年、2023 下半年
- 2024 上半年、2024 下半年
- 2025 上半年、2025 下半年

下载地址和 SHA-256 保存在
`data/reference_universe/capco_industry_documents_2023h1_2025h2.json`；解析结果保存在
`data/reference_universe/capco_industry_history_2023h1_2025h2.json`。

每一行保留股票代码、一级行业字母、两位细分行业代码、分类期末日、页面发布日期、
原始 URL 和原始 PDF 的 SHA-256。行业代码使用 `一级字母 + 两位细分码`（例如
`J66`），与 2012 CSRC 分类的可比粒度一致。公司名称不参与连接，避免名称变更造成
错误合并。

## 防前视规则

`IndustryClassificationHistoryStore.labels_as_of(evaluation_date)` 只使用
`published_at <= evaluation_date` 的最新分类。分类期末日只用于审计，不能代替发布日期。
CSRC 2020Q4–2021Q3 和 CAPCO 2023H1–2025H2 已合并为
`industry_classification_history_official_2020q4_2025h2.json`；两者之间仍存在公开来源
断层，不用当前 Baostock 行业标签填充。

## 当前覆盖

CAPCO 六份文件解析出 32,104 条观察、5,797 个不同代码；合并官方历史共 49,026 条观察。
在 667 标的完整点时数据上，行业标签可用于 2021Q1–Q3、2021Q4–2022Q3，以及
2024Q1–2026Q1 的严格 OOS 日期；2023 年的部分日期因标签发布日期和 366 天账龄规则
暂不使用。
