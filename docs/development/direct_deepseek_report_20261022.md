# One-shot DeepSeek report, 2026-10-22

The user's new instruction explicitly requests fixing note boundaries, then one
direct DeepSeek report using Markdown/CSV and an unrelated-industry example.
This run does not reuse/reset the old authorization, budget ledger, state machine,
agent loop, review loop or publication path. One POST was attempted, without tools,
JSON output requirements, retries or follow-up model calls.

## Source-boundary repair

`fr-pack/25` recognizes `合并财务报表主要项目注释` and its report variant,
normalizes spaces for heading recognition, and merges repeated continued headers
without deleting their original text. Unknown peer major headings also terminate
the preceding known block, rather than letting a whitelist miss swallow the rest
of the notes. Classification uses normalized headings; selected source text is
not rewritten or summarized. Original frozen data and facts are not overwritten.

Observed CCC repairs:

- 2025 annual: old `税项(续)` block 103,176 characters/120 pages; corrected tax block
  1,633 characters, physical pages 223–225 (ending before the next heading).
- 2026 H1: old `税项` block 100,570 characters/113 pages; corrected tax block
  1,574 characters, physical pages 150–152 (ending before the next heading).
- The next major heading and tax pages were visually checked in the original PDFs.
  The heading at annual physical p225 is `五、合并财务报表主要项目注释`;
  the H1 counterpart is physical p152. Printed page labels differ.
- Eight recent disclosures, seven accounting topics each, 228 whole located policy
  and amount-note sections: every exported text byte matches its selected source
  lines. Contained overlapping sections are exported once. Location is not an
  accounting-quality judgment. Full receipt:
  `data/fundamental_quant/company_notes_materials_20261022/note_boundary_replay.json`.
- Regression plus existing pack/export tests and `tests/validation`: 94 passed;
  Ruff passed. Actual original-page excerpts are frozen in the new regression fixture.

## Frozen one-shot input

Run folder:
`data/fundamental_quant/direct_markdown_input_20261022-200725`.

The standard API message envelope contains Markdown/CSV reading content, not the
audit JSON. Input includes 4 company financial views (43 periods/2,904 financial
and derived cells), 4 observed-price snapshots, 28 whole annual MD&A chapters for
2019–2025, 4 latest H1 MD&A chapters, 228 recent full accounting-note sections, and
the existing liquor report as an unrelated-industry writing example. Seven older
interim MD&A files are explicitly listed as omitted, without deleting their
financial observations or original files. No old engineering research answer is
included. Source block headers expose physical page coordinates.

The actual message text measured 930,826 tokens; the service returned 930,856 input
tokens. The 30-token difference is chat framing. HTTP JSON escapes are not model
text tokens. Output maximum 64,000 plus a 4,096-token input-framing margin fits
the known one-million context. No character clipping or model summary was used.

## Actual API result

- Request: 2026-10-22 20:09:23 Asia/Shanghai; complete 20:12:39.
- One paid request; HTTP 200; stream `[DONE]`; finish reason `stop`.
- Service model alias: `deepseek-flash`; thinking enabled, effort high.
- Actual input 930,856; cache hit 0; cache miss 930,856.
- Output 40,203, including provider-reported reasoning 35,346.
- Public Markdown: 7,650 characters, nine main sections; exact provider public
  content retained. Windows newline translation was normalized back to LF only.
  Private reasoning remains in the private SSE receipt, not the report/reviewer input.
- Verified off-peak tariff arithmetic: CNY 1.091668 (provider usage times tariff,
  not invoice reconciliation). Pricing source and date are in the receipt.
- No independent review, human-authored financial replacement, charts, publication
  or email. Completing an API response does not certify its financial conclusions.

Outputs: `index.html`, `report.md`, `receipt.json`, `preflight.csv`, `selection.txt`,
`request.md`, immutable submitted payload and private SSE receipt. The single
request script uses an exclusive intent file to prevent accidental resubmission.
There is no automatic correction/retry or route back into the old workflow.
