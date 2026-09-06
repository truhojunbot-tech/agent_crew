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

## The opt-in, and why it is gated (review of PR #264)

`@pytest.mark.live_github` was originally a bare escape: a marked test got no
stubbing, on a normal run, with no target check. That is the same hole in
self-service form — any test could opt itself out and write to the production
repo.

A marked test now runs only when all three hold:

| condition | why |
|---|---|
| `AGENT_CREW_ALLOW_LIVE_GITHUB=1` | an operator said yes on this run, out loud |
| `AGENT_CREW_LIVE_GITHUB_REPO=<owner/name>` | the target is named, never inferred |
| that target is not this checkout's repo, and its name marks it disposable | the likely accident is pointing the opt-in at whatever is already configured |

Otherwise the test is **skipped** with the reason, and the write functions stay
stubbed for it regardless.

Approval names a target; it does not hand over the write functions. Every write
in an approved test is pinned to that repository at the boundary: the `repo`
argument is read out of the call by signature, and a missing one (which would
fall back to this checkout's origin), a mismatched one, or a helper with no
`repo` parameter at all is refused. Without that, an approved test calling
`post_pr_comment(241, "x")` would have written to production from inside the
exception granted to avoid exactly that.

## A second incident, 2026-09-06 (#263 review round 4)

Four more synthetic comments reached PR #241 — `"this must not reach
production"` ×2 and `"nope"` ×2, at 05:31–05:32Z — bringing its total from 263
to 304 (41 of today's are these plus ordinary review automation).

They were mine, and the cause is worth recording because it is not the original
bug. The write-boundary tests run probe files with the guard **deliberately
disabled**, which is how mutation testing a safety mechanism works: you remove
the safety and check that something notices. Those probes used the real PR
#241, so when the mutation removed the guard, the probe did exactly what it was
written to do.

The fix is that a probe payload must be harmless on its own: every probe now
targets PR **999241**, which cannot exist. Testing a guard means running the
dangerous thing with the safety off, so the dangerous thing has to be aimed
somewhere that does not matter.
