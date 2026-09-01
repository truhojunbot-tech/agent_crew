# Context Pack benchmark (#239)

Measured on **14 real closed issues** from `truhojunbot-tech/agent_crew`,
regenerate with:

```bash
python scripts/benchmark_context_pack.py --repo truhojunbot-tech/agent_crew --limit 14
```

| mode | required_recall | ac_present_rate | mean_tokens | irrelevant_ratio | duplicate_ratio | stale_conflict_rate | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|
| current | 0.000 | 0.286 | 462.4 | 0.000 | 0.000 | 0.000 | 0.0 | 0.0 |
| lexical | **1.000** | 0.286 | 1790.9 | **0.761** | 0.000 | 0.000 | 32.1 | 39.3 |

## Reading this honestly

**The win is real.** `current` retrieves *nothing* — today the dispatcher ships
the issue text and whatever the provider conversation happens to still hold.
Required-artifact recall is 0.000 because no retrieval happens at all. The
lexical pack retrieves every issue-named file that exists in the tree: recall
1.000.

**The cost is real too.** Mean pack size is ~4× the current prompt (462 → 1791
estimated tokens), and **76% of pack tokens are outside the required set**. The
lexical baseline over-retrieves: `git grep -l` on identifier keywords matches
plenty of files that merely mention a symbol.

**`irrelevant_ratio` is not a fair head-to-head.** `current` scores 0.000
trivially, because a mode that retrieves nothing cannot retrieve anything
irrelevant. The number is only meaningful *between retrieval modes* — which is
the comparison a future semantic/hybrid mode should win.

**`ac_present_rate` measures the corpus, not the pack.** Only 4 of 14 issues
carry an `## Acceptance criteria` heading, so both modes score 0.286. Where the
heading exists, the pack always extracts it as a separate mandatory artifact.

## What this establishes

A deterministic floor. Recall is solved; **precision is the open problem**, and
it is now measurable — which was the point of shipping lexical before semantic
rather than assuming embeddings help. A semantic or hybrid mode should be
judged on whether it cuts `irrelevant_ratio` while holding recall at 1.000.

## Not measured

- **semantic/hybrid** — not implemented. The provider contract admits it with
  no dispatcher change, but no embedding backend ships, so no semantic row is
  reported rather than a fabricated one.
- **task/review outcome** — the pack is off by default and has not run in
  production, so there is no outcome sample yet. Turning it on for one project
  and re-running this is the natural follow-up.
