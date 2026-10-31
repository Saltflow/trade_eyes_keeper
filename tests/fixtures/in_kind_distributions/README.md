# Official in-kind distribution source fixtures

The five UTF-8 text files were extracted from complete HKEX PDFs on 2026-10-31
using `HkexDividendEvidenceProvider._notice_text`, with the existing document,
page and extracted-text limits. `sources.json` binds each original URL, release
date, PDF SHA-256 and extracted-text SHA-256. Original downloaded PDFs and the
live ETNet replay are retained locally under
`data/acceptance/in_kind_sources_20261031/`.

- Tencent Music: 2018-12-13 declaration; the live ETNet ex-date is 2018-12-28.
  ETNet's earlier 2018-12-03 announcement reference is not the publication date
  of the definitive declaration. This event has a tiered ADS/cash election.
- JD.com: 2021-12-23 declaration and 2022-03-25 completion notice.
- Meituan: 2022-11-16 declaration and 2023-03-24 completion notice.

Declaration schedules are estimates. Completion notices prove issuer dispatch
and confirm that fractional-share disposals took place; they do not establish
the customer's custody receipt date, actual fractional net proceeds, applicable
account tax/fees, or cash credit date. Tests keep those facts unknown. Structured
receipts used in unit tests are explicitly synthetic and are not production
data-readiness evidence.
