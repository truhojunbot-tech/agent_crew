# Context Pack benchmark (#239)

- run_id: `c421900744f5`
- generated_at: `2026-09-01T15:12:23+00:00`
- sample: 14 issues from truhojunbot-tech/agent_crew

> docs/context_pack_benchmark.json is canonical; the Markdown is rendered from it in the same run.
> p50/p95 are wall-clock and will differ between runs on the same revision; compare them only within one run_id.

Regenerate both artifacts together with:

```bash
python scripts/benchmark_context_pack.py --repo truhojunbot-tech/agent_crew \
       --limit 14 --write-report docs
```

| mode | required_recall | ac_present_rate | mean_tokens | irrelevant_ratio | duplicate_ratio | stale_conflict_rate | p50_ms | p95_ms |
|---|---|---|---|---|---|---|---|---|
| current | 0.0 | 0.286 | 462.4 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| lexical | 1.0 | 0.286 | 1941 | 0.78 | 0.0 | 0.0 | 53.1 | 75.1 |

## Reading this honestly

**The win is real.** `current` retrieves *nothing* — today the dispatcher
ships the issue text and whatever the provider conversation happens to
still hold. Required-artifact recall is 0.0 because no retrieval happens
at all. The lexical pack retrieves every issue-named file that exists.

**The cost is real too.** The pack is several times the size of the
current prompt, and most pack tokens fall outside the required set: the
lexical baseline over-retrieves, because `git grep -l` on identifier
keywords matches files that merely mention a symbol.

**`irrelevant_ratio` is not a fair head-to-head.** `current` scores 0.000
trivially, because a mode that retrieves nothing cannot retrieve anything
irrelevant. The number is only meaningful *between retrieval modes*.

**`ac_present_rate` measures the corpus, not the pack.** It reflects how
many sampled issues carry an `## Acceptance criteria` heading at all.

## What this establishes

A deterministic floor. Recall is solved; **precision is the open
problem**, and it is now measurable — which was the point of shipping
lexical before semantic rather than assuming embeddings help.

## Not measured

- **semantic/hybrid** — semantic/hybrid not implemented — the provider contract admits it without a dispatcher change, but no embedding backend ships, so no semantic row is reported rather than a fabricated one.
- **task/review outcome** — the pack is off by default and has not run
  in production, so there is no outcome sample yet.
