# HKEX dividend evidence fixture

- `2026092901567.pdf`: PICC (01339), official cash dividend update,
  published 29 September 2026, downloaded 16 October 2026 UTC.
- Source: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0929/2026092901567.pdf
- Complete original two-page PDF, not a truncated/sanitized extraction.
- Regression purpose: labels and values are separate positioned PDF objects.
  Plain column-order text extraction loses date/amount associations; visual-row
  reconstruction must preserve the existing strict cash-evidence match.
- This document is **not** identified as the historical oversized failure.
