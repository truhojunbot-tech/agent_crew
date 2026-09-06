# #269 — post-merge verification of #261 on organic live traffic

**Measured:** 2026-09-06 09:32–09:37 UTC · **Verdict: BLOCKED — #261 is not
running anywhere on the fleet.** Raw capture:
[`issue_269_snapshot_2026-09-06.json`](issue_269_snapshot_2026-09-06.json),
reproducible with `PYTHONPATH=src python3 scripts/context_economics_snapshot.py`.

That capture was taken with `PYTHONPATH=src` from this worktree, before the
script recorded the measuring tree itself. It now emits `measured_with`, and it
**refuses to report at all** — non-zero exit, empty stdout — if the cap
functions cannot be imported from the checkout it ships in (review of PR #271).
The capture above is left byte-for-byte as taken; it is evidence from a moment,
not a file to refresh.

#269's first acceptance criterion is a gate on all the others: *"Running
dispatchers are proven on `1db0c64c` or later before measurement."* They are
not, so no observation below can be attributed to #261. What follows is
therefore the **pre-deploy baseline** — the "before" side of the comparison
#269 asks for, captured while it is still capturable.

⛔Nothing here was manufactured. No cap was lowered, no session inflated, no
process restarted. Every number is organic traffic, read-only.

## 1. Build provenance — what is actually running

Taken from each process's own `GET /provenance` (frozen at import by #248),
not from the checkout:

| dispatcher | port | imported commit | fingerprint | uptime |
|---|---|---|---|---|
| alpha_engine | 8101 | `694be98` | `e33dd4529bfd2e36…` | 81.7 h |
| agent_crew | 8105 | `694be98` | `e33dd4529bfd2e36…` | 81.7 h |
| quota-core | 8106 | **pre-#248** (no `/provenance` route) | — | since 2026-09-01 00:02 UTC |
| quota-ops | 8107 | **pre-#248** (no `/provenance` route) | — | since 2026-09-01 00:03 UTC |

`1db0c64c` (#261) merged 2026-09-06. Every running process started 3–5 days
earlier, so none can contain it. `694be98` is #251.

Three things make this a proof rather than a timestamp argument:

* both live servers report `checkout_moved_since_start: false` and
  `source_changed_since_start: false`;
* re-running `provenance.snapshot()` against the deployed checkout right now
  reproduces `e33dd4529bfd2e36…` exactly — so the source I read below is
  byte-identical to what those processes imported;
* `dirty: true` on that checkout is one untracked `synthesis.md` at the repo
  root; `git status --porcelain -- src/` is clean, so the commit attribution
  holds.

quota-core and quota-ops answer their port but 404 on `/provenance`. That
route landed in #248, so those two are older than #248 — and the reflog dates
them precisely: the checkout sat at `e6a71c1` from 2026-08-31 23:06 UTC until
2026-09-02 11:54 UTC, spanning both start times. **The two dispatchers whose
consumers asked for this evidence are the furthest behind.**

## 2. What the running code does instead of #261

Read out of the fingerprint-verified deployed source:

* `server.py:2012` — `"codex", "exec", "resume", "--last"`. The global resume
  #261 replaced, verbatim.
* Cap coverage is **agy-only**: `AGENT_CREW_AGY_CONTEXT_MAX_MB` exists;
  `AGENT_CREW_CLAUDE_CONTEXT_MAX_MB` and `AGENT_CREW_CODEX_CONTEXT_MAX_MB` do
  not appear. Claude and Codex stores are never measured, so they can never
  trip.

The attribution rows agree, over 7 days across the four projects that have the
context-identity schema:

| agent | dispatches | with `provider_session_id` |
|---|---|---|
| claude | 383 | 383 (100 %) |
| **codex** | **747** | **0 (0 %)** |
| gemini | 290 | 0 (0 %) |

So #269's criterion 2 is not merely unmet — it is **unmeetable on this build**.
There is no recorded Codex session id to join a resume to, and the launch argv
would ignore it if there were.

## 3. Cross-project attachment risk, live

The newest Codex rollout on the host right now — the one `resume --last`
selects — is
`rollout-2026-09-03T11-57-02-01a06721-…`, whose `session_meta.cwd` is
**`/home/truhojun`**: an unrelated interactive session, outside every crew
worktree. 9,894 rollouts exist on this host, matching #262's count.

#261 would instead bind each reviewer to its own worktree session; those exist
and are identifiable today (`alpha_engine 01a02def…`, `halla 019ffa43…`,
`quota-core 01a02294…`, `quota-ops 01a02297…`). The deployed build ignores all
four.

⛔Stated as risk, not as an observed incident. It is 0.11 MB, so nothing about
  it would trip a cap; the exposure is misattachment, not size, and this
  window produced no evidence that a wrong session was in fact replayed.

## 4. Cap lifecycle — works, for the one provider that has it

Two organic `provider_context_capped` events in the window, both agy, both
alpha_engine, fully joined:

```
gen 1  context 38685e46  511 tasks  08-21 06:08 → 09-02 11:37   → failed:exit_1
  09-02 11:56:35  context_reset + provider_context_capped
                  provider=agy conversation=0aff70cb  147,435,520 B (140.6 MB) vs cap 64 MB
gen 2  context 89772d69    2 tasks  09-02 11:48 → 11:58         → failed:agy_quota_exhausted
  09-02 12:03:59  context_reset + provider_context_capped
                  same conversation 0aff70cb, same 147,435,520 B
gen 3  context ed473def   96 tasks  09-02 12:03 → 09-06 09:27   → completed
```

Identity stays coherent across both transitions: a new `context_id` per
generation, `session_task_index` restarting at 1, `previous_task_id` recorded,
and the first task of each new generation dispatched `fresh`.

The second trip fired 7.4 minutes after the first on the *identical*
conversation and *identical* byte count. The evidence supports the benign
reading: generation 2 died on `agy_quota_exhausted` after two tasks, so it
never wrote a new conversation, and the next dispatch measured the same stale
file and correctly capped again. It is **not** evidence that the reset failed
to detach — but it is also not proof that it detached, and one sample cannot
separate the two. Recorded here so a later window can settle it.

Across the whole fleet, `context_generation > 1` occurs for **gemini only**.
Claude and Codex sit at generation 1 everywhere — as expected when no cap is
wired for them.

## 5. Claude growth is not bounded — the finding that matters

`alpha_engine/claude` is **365.07 MB against a 64 MB cap**, and the deployed
build has no Claude cap to trip.

One transcript file, `ab2cf40c-…jsonl`, 229,969 lines, read from its own
entries:

* first entry `2026-06-27T07:50:53Z`, last entry `2026-09-06T09:32:48Z`;
* **71.07 days unbroken**, averaging **5.14 MB/day**;
* it crossed 64 MB around **2026-07-09** — roughly day 12.5 — and has been
  resumed on every dispatch for the ~59 days since.

#269 records this store at 290 MB; it is 365 MB now. The 5.14 MB/day figure is
the file's own measured lifetime average, not a rate inferred between those two
observations.

Every other worktree is comfortably bounded: next largest is
`quota-ops/codex` at 24.22 MB, then `alpha_engine/gemini` at 19.90 MB. Of 22
worktrees, exactly one is over cap. So "bounded growth over a representative
window" holds fleet-wide **except** for the single case #261 was written to fix.

⛔Store size ≠ context-window tokens. Every figure here is bytes on disk. The
  900k+ cache-read tail #269 cites is a token-side metric and is not measured
  by this snapshot.

## 6. Acceptance criteria

| # | criterion | status |
|---|---|---|
| 1 | dispatchers on `1db0c64c`+ | ❌ **proven false** — `694be98` and pre-#248 |
| 2 | Codex resume joined to recorded session id, not `--last` | ❌ unmeetable — 0/747 rows carry an id; argv is `resume --last` |
| 3 | fresh/reset proves prior state not resumed | ⚠️ agy only; claude/codex never leave generation 1 |
| 4 | organic cap observed, or bounded growth shown | ⚠️ agy observed end-to-end; Claude **unbounded** at 365 MB / 5.14 MB per day |
| 5 | identity coherent across transitions | ✅ for both agy transitions |
| 6 | no cross-project attachment or in-flight race | ⚠️ none observed; the exposure is live and demonstrated in §3 |
| 7 | evidence suitable for a quota cohort | ❌ two of three providers have no join key |

## 7. What unblocks this

A deploy and dispatcher restart — **operator action, deliberately not taken
here.** Restarting live dispatchers is on this repo's escalation list, and in
#247 the same deploy was the precondition for verification rather than part of
it. The same gap blocks #247.

After `git pull` to `1db0c64c`+ and a restart of ports 8101/8105/8106/8107:

1. re-run `PYTHONPATH=src python3 scripts/context_economics_snapshot.py` and
   confirm every dispatcher reports a commit containing `1db0c64c`. A non-zero
   exit means the environment could not be trusted to measure and the run
   produced nothing — that is the intended behaviour, not a script bug;
2. confirm Codex `provider_session_id` coverage moves off 0 %;
3. watch `alpha_engine/claude` — at 365 MB it should trip on the first
   dispatch, giving criterion 4 its end-to-end join for free and without
   manufacturing anything;
4. re-run after a representative window and diff against
   `issue_269_snapshot_2026-09-06.json`.
