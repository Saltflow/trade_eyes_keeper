# HKEX dividend evidence fixture

- `2026092901567.pdf`: PICC (01339), official cash dividend update,
  published 29 September 2026, downloaded 16 October 2026 UTC.
- Source: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0929/2026092901567.pdf
- Complete original two-page PDF, not a truncated/sanitized extraction.
- Regression purpose: labels and values are separate positioned PDF objects.
  Plain column-order text extraction loses date/amount associations; visual-row
  reconstruction must preserve the existing strict cash-evidence match.
- This document is **not** identified as the historical oversized failure.

- `ltn201804191437.pdf`: PICC (01339), poll results, published 19 April 2018;
  complete original PDF downloaded 18 October 2026 UTC.
- Source: https://www1.hkexnews.hk/listedco/listconews/sehk/2018/0419/ltn201804191437.pdf
- SHA256: c65a043563b606e679e9ddf6bd0d460c661a5e99fc19afd0dfdabd3f5edb73ba
- Regression purpose: resolve the 24 April 2018 RMB-only event using the
  register date, declared RMB cash and explicit HKD/RMB exchange rate. A
  transient download failure must remain unverified until complete evidence
  is obtained on the single permitted retry. This is valid evidence, not an
  identified production download failure or oversized culprit.
