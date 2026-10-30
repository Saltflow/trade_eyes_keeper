# Company/period reading files (offline, 2026-10-22)

2026-10-23: Historical delivery record below. The old
`scripts/export_research_materials.py` entry and its unused export orchestration
were removed. CSV formatting is reused in `src/fundamental_data/history.py`;
current data boundaries are documented in [the three data modules](fundamental_data_20261023.md).
The commands below describe the prior run, not a current supported entry point.

The model-facing reading format is Markdown with embedded CSV. Internal JSON,
SQLite, immutable source records and historical paid receipts are unchanged.
Neither a peer group nor its review state is required to export a company-period
record. Existing FR packs already store individual disclosures; the old research
entry point incorrectly made a group-wide packet the reading boundary.

## Layout and use

```text
companies/a_share/600039/
  financials/2025-12-31_FY.md
  financial_history.md
  market_snapshot.md
  reports/2025-12-31_<source-version>/
    management_01.md
    source.csv
    chapters.csv
audit/companies/a_share/600039/...
selection.txt
metrics.csv
```

The financial period file is the independent data unit. A company history is a
derived reading view: metrics in rows, actual fiscal periods and FY/H1 labels in
columns. Unit/currency/scope appear once per compatible CSV block. Empty cells
mean absent observations; `NA` means an explicitly null observation. Values,
negative results and original numeric strings are not rounded, rescaled or
silently filled. Multiple report versions remain separate directories.

`selection.txt` references files and defaults to company histories, market
snapshots and original management sections, without repeating the period views.
Users may replace a history with selected period files, or add/remove companies.
Omitted companies' cached files are retained; unchanged files are not rewritten.
Detailed lineage stays in `audit/` and original documents, available for lookup
instead of being repeated beside every model-visible number.

```powershell
python -X utf8 scripts/export_research_materials.py `
  --input data/fundamental_quant/construction_final_review_20261021-163028/construction/packet.json `
  --output data/fundamental_quant/company_reading_materials_20261022
```

`--issuer a_share:600039 --period 2025-12-31` selects one company-period.
The legacy packet is only a migration source here. The exporter also accepts a
standalone row with issuer, period, kind, values and metric bases, without group
configuration. It does not download data, start an agent, authorize a campaign,
publish or send email.

## Observed acceptance

- Real export: 43 independent period files, 4 company history views, 4 dated
  market snapshots and 39 complete management sections.
- Independent CSV reconstruction matches all 2,904 financial/derived cells and
  their unit/currency/scope; management text bytes match the frozen originals.
- Financial history views: 24,362 tokens. Raw management sections: 787,441.
  Selected reading body with file labels: 817,339.
- With the unrelated liquor writing sample and short instructions, the prepared
  standard API message envelope measures 842,255 tokens. Adding a 5% input margin
  and 65,536 output space fits the known one-million context. This is preparation
  only, not a model result or authorization to spend.
- `tests/test_research_material_export.py`, existing projection regressions and
  `tests/validation`: **52 passed**; Ruff passed. Tests cover a single company,
  different currencies/scopes, negatives/large values/null/zero/absent cells,
  actual fiscal periods, adding/removing peers without rebuilding other files,
  exact original text, multiple disclosure versions, observed market dates and
  cache reuse. Replay receipt is in the export directory.

Accounting-note section boundaries remain unresolved. The oversized tax blocks
are not included as supposedly verified notes. This delivery accepts the reading
format and company/period granularity only. It does not accept a full financial
research data contract, autonomous research, or the previous paid state machine.
No paid request, publication or email was made.
