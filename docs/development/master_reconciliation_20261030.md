# Local and published master reconciliation (2026-10-30)

The local `master` at `13a82c3` and published `origin/master` at `6a55894`
had diverged by 12 and 17 commits. Published runtime retirement and shared
execution files were also present in the dirty local directory. They must
not be presented as new, uncommitted features solely because local HEAD
did not contain their published commits.

The merge retains both histories. Twenty conflicting files were reviewed in
an isolated checkout. Local USD dividend evidence, fixed FX, HK listing-date
evidence, issuer identity recovery, announcement ingestion and scan failure
reporting remain. Published bounded HKEX parsing/retries, record-date evidence,
shared execution, outbound Bots and optimizer auto-promotion also remain.
Both dividend/results and poll-results searches are retained under the shared
document-attempt budget. Notice cache identities distinguish bounded parsing
from earlier cached extraction. Integrity-checked persistent source caching
is retained, so mock source tests use separate working directories.

Two older assistant regressions used the retired private in-memory queue.
They now exercise durable admission, duplicate rejection, shared execution
completion and cancellation while another conversation is still queued.
The currency compatibility regressions and published promotion regressions
are both retained.

Validation used mocked external requests and did not send reports or run an
optimizer. The initial test invocation had a missing parent for pytest's
temporary directory, and HK mock responses shared persistent cache entries;
those failures are retained in the local audit logs. After correcting the
test setup, the broad selection had 578 passing, 3 skipped and the two older
queue-test failures. The repaired selection passed all 127 cases. Across
the latest result for each distinct test, 588 passed and 3 skipped.
This is targeted merge validation, not the full suite or production acceptance.

The original checkout's source files and untracked code were backed up and
hashed before reconciliation. Uncommitted research, configuration and report
changes are restored as working-directory changes, not included in this
merge. Overlapping local files are preserved rather than blindly checking
out the merged tree over them. The local audit and restoration receipts live
in `.tmp/merge_audit_20261030/`; this disposable evidence is not committed.
There is no push, deployment, service restart or active-strategy change.
