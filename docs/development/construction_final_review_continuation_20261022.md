# Engineering final-review continuation (local delivery only)

The user explicitly authorized 40 model calls, 30,000,000 input-token
reservations, 1,500,000 output-token reservations and 300 minutes. Passing ends
the run immediately. Publication and email remain disabled.

The continuation reuses the actual research result, both Pi histories, all
registered calculations and estimates, frozen source packet and frozen Skill
snapshots. It does not rerun preparation or create a new research answer.

- Source: `data/fundamental_quant/deepseek_repaired_construction_20261021`.
- Current run is recorded by
  `data/fundamental_quant/construction_final_review_latest.json`.
- Latest starting result hash:
  `32df6a08e3a6ecfb5b7d4bcb5daaf7fa4563ac1a62441b28738480eda82e7ea7`.
- Source ledger and expired authorization remain unchanged. The old final
  request has unknown usage; it is archived and not retried under the same ID.
- The new run starts at sequence 28 with its own explicitly authorized ledger.

## Complete context instead of incremental trial reviews

The independent reviewer receives the latest **complete** revised report and
all eight complete recent annual/interim management-discussion chapters for
the four companies. Previous independent reviews and their evidence remain in
the review history. Researcher private reasoning is not transferred.

Keeping the old context and appending these chapters without a capacity change
would require approximately 1.11 million tokens including safety margin and
output reservation, above the model's one-million-token capacity. Therefore a
single explicit context projection moves the bulky historical monthly valuation
records from the active panel to the frozen, tool-readable evidence store. The
original prefix and complete starting checkpoint are archived verbatim. Annual
financial records, current price/share observations, estimates, calculations,
source pages and review findings remain active. No financial value or research
judgment is changed by this projection.

The prepared request measures approximately 817,548 input tokens, reserves
approximately 858,426 input tokens and 65,536 output tokens, within capacity.
Pi framing splits the logical input into three bounded frames. This projection
may cold-miss the provider cache once; actual provider usage, rather than an
assumed cache percentage, controls costs. Subsequent turns preserve context.

## Acceptance boundary

The source draft passed deterministic result checks, and the preceding offline
protocol/control regression had 107 passing tests. Neither proves financial
review acceptance. Only a complete, valid independent review of the current
result can grant local accepted-report status. Review corrections, tool
continuations and any necessary context compaction share the new hard limits.
Do not allocate separate budgets per role, reset counters, or hide unknown use.

Consult the current run's `runtime.json`, `terminal.json`,
`construction/checkpoint.json` and `cost_receipt.json` for actual progress and
outcome. This preparation note does not assert a successful paid review.

## Observed continuation repairs

The first complete independent review returned actionable accounting/evidence
issues. A subsequent reviewer response exposed numeric-wire failures, not a
successful research result. The following repairs preserve those objections:

- Numeric prose correction stays in the independent Pi history, shares the
  original ledger/deadline, allows at most two retries per result hash, disables
  tools for this correction, and pins verdict, check outcomes, issue identities,
  severities, claim IDs and references. It cannot turn rejection into approval.
- A single paragraph in `limitations` is losslessly wrapped in a list before
  validation. Raw provider content remains immutable.
- The numeric binder can render an exact registered estimate ID as its complete
  labelled low/high range. It cannot choose the base, infer an endpoint, invent
  a calculation or use a midpoint. Unknown/quarantined references and invalid
  bounds remain rejected. Bindings explicitly distinguish estimate ranges from
  computed scalar values.
- Evidence-delta reconstruction consults complete archived reviewer histories
  after compaction, preventing unchanged panel fields from being mistaken for
  undelivered data and repeatedly appended. New archives carry content hashes.
- Resuming the same already-authorized five-hour deadline retains that deadline
  and all usage counters. This does not authorize extending a deadline; the
  existing bound still rejects a longer renewed window.

The real response from call `7a01adea4b8a-0032-review` was consumed offline after
these adapter repairs. Its `revise` verdict and five substantive issues were
retained. This consumption made zero model calls and did not grant acceptance.
The raw response, before-checkpoint, adapter changes and same-ledger proof are in
`wire_repair_2_offline_receipt_consumption` within the continuation directory.

Call `7a01adea4b8a-0034-research` also returned a complete public revision inside
a native `revise` control envelope, with two redundant final object closers and
known revision fields beside `patch`. The response was already fully received
and accounted. The local parser now accepts only these bounded, unambiguous
structural variants: recognized submit/revise/review control envelopes are
converted to final structured output, never executed as arbitrary tools;
disjoint known revision siblings are moved into the patch without changing
their contents; up to two extra closing braces after an already complete JSON
object may be removed. Unknown tools, conflicting fields, extra semantic JSON,
duplicate keys and incomplete responses remain blocked. Original bytes and
usage are preserved. The actual public response (excluding private reasoning)
is frozen in `data/retired_test_fixtures_20261030/fundamental_quant/native_control_revision_20261022.json`.
Reparsing that received response made no additional provider request and did
not reserve or charge its usage twice.

## Terminal outcome: paid review not accepted

The run stopped on `7a01adea4b8a-0055-review`: the service returned
`finish_reason=length`, 8,192 output tokens and empty public content. This is not
a usable review. Private reasoning is neither a report nor a substitute receipt.
The preceding larger request was rejected before reservation/HTTP dispatch and
is retained as unsent. No new authorization or deadline was created.

- Paid calls: 27; actual input: 17,349,229; actual output: 186,838.
- Provider cache hit: 10,001,280; miss: 7,347,949 (57.6% hit).
- Input reservations: 19,053,570; output reservations: 1,499,136 of 1,500,000.
- Unknown-use calls this round: zero. Frozen off-peak tariff calculation:
  CNY 8.2953266; this is not invoice reconciliation.
- Old-round cost CNY 5.49705244 and its one unknown-use request remain separate.

Large output reservations accumulated on tool continuations even when actual
outputs were small. This is a controller design problem, not evidence that the
user authorized too little spending. Repeated compaction/prefix changes also
prevented the assumed 90% cache ratio. Existing counters are not settled,
reset or recycled to resume this failed round. The paid checkpoint remains
blocked, publication and email remain disabled.

A fresh independent offline checkpoint review inspects the existing API-authored
draft and frozen sources without provider calls. It does not change the API
acceptance result or financial assumptions. Any remaining accounting or
normalization objections must remain visible; mechanical repairs cannot stand
in for that judgment. Complete outputs and the stopped-run costs are linked
from `outcome.html` in the continuation directory.

Program regression evidence: combined protocol/source/control/validation suite
247 passed; final stopping-control/budget suite 27 passed. These do not establish
financial or unattended end-to-end acceptance. No publication or email occurred.

The independent offline review is complete: 13 required checks, seven issues,
verdict `revise`. It independently recomputed 78 dependent calculations without
arithmetic errors and verified the original source locations, including a cash
flow statement image. Material objections remain: group cash proxy/common cash
attribution, Sichuan's deconsolidation effects on normalization, and a reversed
price/return statement. Other objections concern scenario labels, profit growth,
dividends including buybacks and Capex ranks. Optional mechanical patches are
saved unapplied; they cannot resolve the economic objections. Original report,
paid receipts and checkpoint are unchanged. Canonical JSON hashes and raw-file
hashes are recorded separately in the independent provenance.
