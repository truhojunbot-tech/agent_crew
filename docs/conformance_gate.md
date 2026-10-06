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

The change file declares `PRE_MERGE`, the project, a short title and task description, `change_type` (default `modify`), and an issue or PR reference. For each field below, a valid task-context value takes precedence: nonempty strings for `capability_id` and `role`, a boolean for `portable_core`, and a list for `dependencies`. `evidence_source` records `context` or `derived` for every value included in the change.

| Field | Value derived when task context does not supply it |
| --- | --- |
| `capability_id` | `<project with underscores changed to hyphens>.issue-<N>`, or `.pr-<N>` without an issue. This identifies the proposed change; it does not claim a registry declaration. |
| `role` | `implementer` for the implement task. |
| `portable_core` | `true` if the PR changes a path under `src/agent_crew/`; otherwise `false`. |
| `dependencies` | One `{"kind":"private_fleet","project":"alfred","file":<path>}` per changed `src/agent_crew/` file whose added lines reference `/alfred/`, `alfred/tools`, `import alfred`, or `from alfred`; otherwise `[]`. |

The last two fields come from one `gh pr diff --patch` read. If that read fails, fields without context values stay absent and the checker returns `REVIEW` rather than treating an unknown diff as evidence of no portable change or private dependency.

The task context records `conformance_gate` with the verdict, receipt path, SHA-256 of the receipt, exit code, PR number, and whether merging is allowed. If the normal receipt path cannot be written, the CLI tries a temporary receipt file and records that path instead.

## Merge behavior

| Gate result | Auto-merge behavior |
| --- | --- |
| `ALLOW` with exit 0 | Continue to the independent review check before `gh pr merge`. |
| `REVIEW` with exit 0 | Post the receipt and its SHA-256 as a PR comment for reviewer follow-up, then continue to the independent review check. |
| Exit 10 (`BLOCK`) | Do not merge. Mark the implement task `needs_human` with the receipt record. |
| Timeout, setup or command error, other nonzero exit, missing or unrecognized verdict, or `EVIDENCE_UNAVAILABLE` | Treat as `REVIEW`: attempt the PR receipt comment, then continue to the independent review check. |

Only exit 10 blocks on conformance grounds. A reported `BLOCK` in a receipt with exit 0 is treated as `REVIEW`. If recording the task context or posting a `REVIEW` comment fails, the CLI warns; that failure does not block the merge. The independent review check below can still prevent it.

## Independent review status before merge

After the gate allows a merge, `crew run --auto-merge` checks the latest completed review for the PR. It requires an `approve` verdict, a `reviewed_sha` equal to the current PR head, and a reviewer agent different from the implement task's agent; both agents come from `task_attribution`. Only then does it publish a successful `crew/independent-review` status on that head, naming the review task and reviewer agent, before calling `gh pr merge`. Missing evidence, a moved head, the same agent on both tasks, or status-publication failure prevents the merge. The server auto-merge path applies the same checks. Neither path uses `--admin` to bypass branch protection.
