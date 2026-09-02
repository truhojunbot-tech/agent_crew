# Post-merge cascade waste on PR #241 (#250)

Measured 2026-09-02 with `scripts/post_merge_waste.py 241`.

| | |
|---|---|
| PR #241 merged | 2026-09-02 01:42:20Z |
| automated exhaustion comments, total | **25** |
| …pre-merge | 15, from 15 distinct review lineages |
| …**post-merge** | **10, from 10 distinct review lineages** |
| post-merge window | 01:45:32Z → 15:30:26Z (**13.8h** after the merge) |
| cascade tasks created post-merge in local crew DBs | 0 |

## How to read these numbers

**The 10 post-merge comments are a floor on post-merge reviewer invocations,
not the total cost.** Each is emitted once per *completed* review task result,
so every one implies a reviewer invocation that finished after the PR had
already merged. There may have been further post-merge invocations that never
reached the exhaustion branch.

**Per-task token/wall-clock cost cannot be attributed for this cohort.** The 10
lineages have no rows in any surviving `~/.agent_crew/*/tasks.db` or
`attribution.jsonl` — they ran under a crew state directory that no longer
exists. Quoting an estimated token figure here would be inventing a number
nobody can check, so this artifact records what is countable and stops.

**The 15 pre-merge comments are a different defect on the same path.** They are
not post-merge waste; they are one escalation repeated across 15 separate review
lineages against a single PR, seven of them inside three minutes. #250 fixes
both, by different mechanisms: the terminal-PR gate, and the once-per-PR notice.

## Classification for Context Efficiency

Work in the post-terminal window should be excluded from "cost to land this PR"
and counted as **cascade waste** — it cannot improve the artifact by
construction. `scripts/post_merge_waste.py` reproduces the split for any PR, so
the cohort can be excluded consistently rather than by hand.
