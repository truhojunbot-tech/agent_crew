# Synthetic review comments written to real PRs by the test suite (#263)

Measured 2026-09-04, after tracing the producer.

| PR | comments | synthetic | posted after #251 merged | window |
|---|---|---|---|---|
| **#241** | 263 | **228** | 30 | 2026-09-02 21:16Z → 2026-09-04 09:37Z |
| #42 | 112 | 0 | 0 | — |

Of #241's 228 synthetic comments, exactly 76 came from each of three test task
ids — `review-late`, `review-open`, `review-outage` — i.e. one triplet per full
run of the suite, across roughly 76 runs.

## Producer

`tests/unit/test_issue_250_terminal_pr_gate.py` defined `PR = 241`, a real and
by then merged PR, and drove the real `POST /tasks/{id}/result` handler. That
handler posts a review verdict as a PR comment (#178) for any review result
carrying a `pr_number`, and none of the three tests patched it.

Two further tests reached the same write path with other PR numbers
(`test_u213_get_task_http_endpoint_returns_result_fields`,
`test_u216_branch_has_pr_not_called_when_pr_number_already_known`); the guard
caught them in the same pass. They wrote nothing detectable to #42 — the
comment count there is unaffected — but the attempt was live.

## Why it survived so long

Every one of those tests passed on every run. The dispatcher wraps its GitHub
calls in `except Exception: logger.exception(...)`, so nothing observable to a
test changed whether the write happened or not. A boundary that depends on each
test remembering to patch is not a boundary, which is why the fix is a suite-wide
block rather than five patched tests.

## For review-economics consumers

Comments matching those task ids on PR #241 are **fixture data** and should be
excluded from review-outcome and post-merge-waste measurements. They are
distinguishable by task id; the organic automation on the same PR uses
`review-<8 hex>` ids.

They have **not** been deleted. Removing 228 comments from a real PR is an
irreversible edit to project history, and it is an operator's call rather than
something this repository's automation should do to itself.
