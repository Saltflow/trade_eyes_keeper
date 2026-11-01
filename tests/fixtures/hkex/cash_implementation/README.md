# Historical short cash implementation notices

The text files contain verbatim issuer headers and dividend implementation
sections from eight public HKEX PDFs. `sources.json` records the official title
search identity and publication date, source URL, full PDF SHA-256, page count,
excerpt page numbers, excerpt SHA-256, and the independently fetched ETNet event.
Numbers are fixture expectations, never production overrides.

Full downloaded PDFs and the complete-PDF parser replay are retained in the
ignored `.tmp/hk_cash_evidence_20261101/` research directory. Production continues
to parse every page of accepted documents and reject documents above 32 pages,
16 MiB, or the extracted-text limit. The text excerpts are matcher fixtures;
they do not authorize accepting truncated production documents.

The notices exercise `on or about`, `distributed`, and tax-inclusive cash amounts
quoted for every ten H shares. Their RMB/HKD distinction and the per-ten divisor
are retained. Neither an earnings date nor a later adjusted-price factor is
used to invent an HKD amount or publication date.
