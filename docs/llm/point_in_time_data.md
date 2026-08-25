# 逐时点财务、原始行情与除权数据合同

本合同补充 `proj4llm.md` 的标的画像约束。当前阶段只补数据，不改变活动策略、
搜索参数或日报决策。

## 唯一价格语义

- `raw_*` 是不复权 OHLC，只能用于历史市值、PE、PB、每股指标和真实成交价核对。
- `qfq_*` 是前复权 OHLC，只能用于技术指标和跨除权日的连续收益序列。
- 恒等式固定为 `qfq_price = raw_price × qfq_factor`。持久化前必须逐行验证。
- 禁止用今天锚定的前复权价格回溯历史 PE/PB；这会把未来分红、拆股和送转影响
  注入早期估值。

## 财务可用时间

- 财务快照按 `(报告期, 实际披露日, 来源)` 保留版本；不覆盖旧修订。
- 历史日期只读取 `published_at <= evaluation_date` 的最新已知版本。
- 没有实际披露日的 Yahoo 财务值只能用于当天画像，不能进入逐时点存储或搜参。
- A 股以 Baostock 的 `pubDate/statDate` 作为季度股本和基础利润历史；沪市再爬取
  上交所免费 XBRL 业绩概览与完整报表，深市再爬取巨潮完整年度报告 PDF。
- 上交所/巨潮披露的归母净利润、扣非归母净利润、经营现金流和归母净资产属于更
  明确的公司口径，在其真实披露日生成新修订并覆盖 Baostock 同期歧义字段；更早
  日期仍保留当时可获得的旧快照，不能把后披露值提前。
- 自由现金流只按真实报表字段计算：
  `FCF = 经营活动现金流量净额 - 购建固定资产、无形资产及其他长期资产支付的现金`。
  任一项缺失、单位不明确或 PDF 标签解析失败时，FCF 必须保持缺失。
- 美股使用 SEC Company Facts 的 `filed/end`，仅在标准 OCF 与资本开支标签同时
  存在时计算 FCF。SEC 不标准化的扣非利润保持缺失。
  SEC User-Agent 优先读取显式的 `SEC_USER_AGENT`，否则使用已有
  `EMAIL_SENDER` 组成可联系标识，不新增付费凭据。
- 港股在可靠适配器完成前明确记缺失，不用推测披露日填充。
- 生产回填禁止付费源、模拟值、默认值和 mock 数据；免费官方源失效时记录 URL 与
  失败原因。可通过 `official_statement_crawlers: false` 临时停用网络爬虫。

## 公司行动

- 公司行动至少保存除权日、已知公告日、现金分红、股份倍数、来源和原始复权因子。


## Cumulative filing normalization

- SEC Company Facts is grouped by original filing date. Later filings' comparative columns no longer replace the original publication date of an earlier period.
- SEC 10-Q flow values are stored as fiscal-year-to-date values. Revenue, net income, operating cash flow, capital expenditures, and FCF are converted to standalone quarters by the shared cumulative-statement normalizer; 10-K values supply the standalone fourth quarter.
- EPS and diluted weighted-average shares select the standalone-quarter duration, while flow fields select the longest fiscal-YTD duration in the filing.
- A-share Baostock, SSE, and CNINFO flow statements use the same cumulative contract. Missing quarterly capital expenditures remain missing; an annual FCF is never presented as the latest quarter.
- Company profiles expose both latest standalone-quarter FCF and four-quarter TTM FCF. When quarterly cash-flow history is incomplete, the annual fallback is explicitly labeled latest_fiscal_year.
- 拆股、送转等明确改变股数的事件可以滚动调整财报股本分母，但不能重写利润和
  净资产总额。
- 只有复权因子变化、但无法拆分现金/送股/配股组成时，保存
  `adjustment_factor_change`，禁止猜测股数变化。
- 配股会同时改变股本和净资产；缺少配股比例、价格或认购结果时，相关估值应保持
  缺失或过期，不能只按价格跳空反推。

## 回填与策略边界

运行：

```bash
python scripts/backfill_point_in_time.py
```

默认处理 `config/config.yaml` 全部标的，输出到 `data/point_in_time/`。其中市场文件
同时含 raw/qfq OHLC、因子、可交易状态和公司行动；财务文件保存全部带披露日版本；
`latest_backfill.json` 明确列出每只标的的成功、缺失和失败原因。
`statement_field_coverage` 以全部公司标的（包括明确缺失的市场）为分母，
逐列输出填充数、总数和填充率，不能用“不适配/抓取失败”缩小分母。

`FundamentalFeaturePanelBuilder` 只接受上述原始价和逐时点财务。它生成独立的数值
矩阵与 availability mask；目前不接入 `technical_ensemble`，也不进入任何历史优化
窗口，直到全量覆盖审计和防前视验收通过。


## Requested-window coverage

- A successful response is not sufficient for market-history completeness. The backfill report records requested/actual date boundaries, calendar coverage ratio, and full/partial status separately.
- For A-share instruments, Baostock remains the primary source. If its first observation is more than the configured tolerance after the requested start, Yahoo is queried once and the bundle with the earlier start (then more rows) is selected.
- The default tolerance is 31 calendar days. Newly listed instruments remain explicitly partial instead of being represented as six-year-complete.
- Baostock SDK calls run inside a serialized socket session with a configurable 20-second default timeout. A stalled TCP read becomes an explicit source failure and the router falls back instead of blocking the entire instrument batch.
- SSE companies use current SSE performance summaries plus recent official annual-report PDFs indexed by CNINFO. The legacy SSE full-statement endpoint remains historical evidence but is not treated as current because its balance/cash-flow series stops early.
- official_pdf_recent_years limits the expensive PDF supplement to the recent years needed to refresh balance-sheet and cash-flow fields. The default is three years.
- Parsed CNINFO PDF values are cached by source URL with a content hash and parser contract, so repeated backfills reuse audited values without downloading and parsing hundreds of pages again.
