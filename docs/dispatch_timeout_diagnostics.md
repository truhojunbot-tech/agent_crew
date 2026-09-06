# Dispatch timeouts vs actual durations (#265, item C)

Measured 2026-09-06 across 4,620 attribution rows from every project DB on the
host.

| cohort | n | median | p90 | max |
|---|---|---|---|---|
| `dispatcher_timeout` | 27 | 910s | 1810s | 1818s |
| `no_result_submitted` | 25 | 726s | 1394s | 1752s |
| `completed` | 2049 | **91s** | **734s** | 61374s |

Configured walls: **1800s** for `implementer`, **900s** for every other role
(`AGENT_CREW_DISPATCH_TIMEOUT_IMPLEMENTER` / `AGENT_CREW_DISPATCH_TIMEOUT`).

## What the numbers say

**The timeouts are real wall-clock kills, not crashes.** The `dispatcher_timeout`
cohort clusters at 910s and 1818s — the two walls, plus the overhead of killing
the process group. Nothing about those tasks failed; the dispatcher stopped
waiting.

**The 900s wall sits at roughly the p90 of successful work.** Completed tasks
have a median of 91s but a p90 of 734s, so about one non-implementer task in ten
legitimately runs within 20% of the wall. That is a thin margin for the tail of
normal work, and it matches the reporter's observation that every affected task
was an analysis-heavy one.

**The `completed` max of 61,374s (17h) is not a counter-example.** Tasks that run
that long are pane-delivered, not dispatched subprocesses, so no wall applies to
them.

## Not changed here

Raising a default timeout changes resource behaviour on every project at once,
which is an operator decision rather than a bug fix — so this records the
distribution and stops. If the intent is that no legitimate task is ever cut,
the non-implementer wall needs to be above the p99 of completed work, not the
p90.

The reason this was expensive was never the wall itself: it was that hitting the
wall was reported as `failed`. That part is fixed — the status is now `timed_out`
and a late result announces itself.
