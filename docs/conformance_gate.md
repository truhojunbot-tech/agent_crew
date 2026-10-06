# ADR-004 conformance gate before auto-merge

`crew run --auto-merge` calls the ADR-004 conformance gate immediately before it attempts `gh pr merge`. The integration is implemented by `_conformance_gate_allows_merge` in `src/agent_crew/cli.py`. It is enabled only when `AGENT_CREW_CONFORMANCE_GATE_CMD` contains a command prefix. With the variable unset or empty, the gate is not called and the auto-merge path proceeds without a conformance receipt.

The shared `~/.local/bin/crew` wrapper currently sets the prefix fleet-wide, unless it is already set, to:

```text
python3 /home/truhojun/alfred/tools/contract_registry.py
```

The CLI appends `capability-conformance-check --input <change path> --receipt <receipt path> --enforce` to that prefix and allows the command 30 seconds. The checker is owned by the Alfred repository; this repo supplies the pre-merge change evidence and handles its verdict.

## Change evidence and receipts

For task `<task>` and PR `<N>`, the CLI writes these files next to the crew database:

```text
<crew dir>/conformance_receipts/<task>-pr<N>.change.json
<crew dir>/conformance_receipts/<task>-pr<N>.receipt.json
```

The change file declares `PRE_MERGE`, the project, a short title and task description, `change_type` (default `modify`), and an issue or PR reference. It also forwards `capability_id`, `portable_core`, `dependencies`, and `role` when the task context supplies them. Missing declarations are left missing; they are not invented to satisfy the checker.

The task context records `conformance_gate` with the verdict, receipt path, SHA-256 of the receipt, exit code, PR number, and whether merging is allowed. If the normal receipt path cannot be written, the CLI tries a temporary receipt file and records that path instead.

## Merge behavior

| Gate result | Auto-merge behavior |
| --- | --- |
| `ALLOW` with exit 0 | Continue to `gh pr merge`. |
| `REVIEW` with exit 0 | Continue to merge and post the receipt and its SHA-256 as a PR comment for reviewer follow-up. |
| Exit 10 (`BLOCK`) | Do not merge. Mark the implement task `needs_human` with the receipt record. |
| Timeout, setup or command error, other nonzero exit, missing or unrecognized verdict, or `EVIDENCE_UNAVAILABLE` | Treat as `REVIEW`: continue to merge and attempt the PR receipt comment. |

Only exit 10 blocks auto-merge. A reported `BLOCK` in a receipt with exit 0 is treated as `REVIEW`. If recording the task context or posting a `REVIEW` comment fails, the CLI warns; that failure does not block the merge.
