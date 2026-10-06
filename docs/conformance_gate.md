# ADR-004 conformance gate before auto-merge

`crew run --auto-merge` and the server's review/test-result auto-merge paths call the ADR-004 conformance gate before publishing the independent-review status or merging. Both use `_conformance_gate_allows_merge` in `src/agent_crew/conformance_gate.py`. The CLI leaves the gate off when `AGENT_CREW_CONFORMANCE_GATE_CMD` is unset. The server refuses auto-merge and marks the implement task `needs_human` with reason `conformance gate not configured` when it is unset. The server must receive this variable in its own environment; the CLI wrapper's default does not configure an already-running server.

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

The change file declares `PRE_MERGE`, the project, a short title and task description, `change_type` (default `modify`), and an issue or PR reference. For each field below, a valid task-context value takes precedence: nonempty strings for `capability_id` and `role`, a boolean for `portable_core`, and a list for `dependencies`. `evidence_source` records `context`, `issue`, `registry_path`, or `derived` according to how `capability_id` was selected; other fields record `context` or `derived`.

| Field | Value derived when task context does not supply it |
| --- | --- |
| `capability_id` | First, an issue-body line beginning `capability:` or `capability_id:`. Next, the unique active, project-owned registry record best matched by changed file paths. The registry path follows CEA wiring: `AGENT_CREW_CEA_REGISTRY_PATH`, then `AGENT_CREW_CEA_CAPABILITY_REGISTRY`, then its default registry path. If the resolved file is unreadable or no record qualifies, use `<project with underscores changed to hyphens>.issue-<N>`, or `.pr-<N>` without an issue. |
| `role` | `implementer` for the implement task. |
| `portable_core` | `true` if the PR changes a path under `src/agent_crew/`; otherwise `false`. |
| `dependencies` | One `{"kind":"private_fleet","project":"alfred","file":<path>}` per changed `src/agent_crew/` file whose added lines reference `/alfred/`, `alfred/tools`, `import alfred`, or `from alfred`; otherwise `[]`. |

The gate reads `gh pr diff --patch` once. It appends up to 20 changed paths to the change text for registry matching and uses the same diff for `portable_core` and `dependencies`. If the diff read fails, those two fields stay absent unless task context supplied them; the checker then returns `REVIEW` rather than treating an unknown diff as evidence of no portable change or private dependency. A registry path tie also leaves the synthetic capability ID in place.

The implement task context records `conformance_gate` with the verdict, receipt path, SHA-256 of the receipt, exit code, PR number, PR head when available, and whether merging is allowed. A retry on the same PR head reuses a verified receipt. If the normal receipt path cannot be written, the gate tries a temporary receipt file and records that path instead.

## Merge behavior

| Gate result | Auto-merge behavior |
| --- | --- |
| `ALLOW` with exit 0 | Continue to the independent review check before `gh pr merge`. |
| `REVIEW` with exit 0 | Post the receipt and its SHA-256 as a PR comment for reviewer follow-up, then continue to the independent review check. |
| Exit 10 (`BLOCK`) | Do not merge. Mark the implement task `needs_human` with the receipt record. |
| Timeout, setup or command error, other nonzero exit, missing or unrecognized verdict, or `EVIDENCE_UNAVAILABLE` | CLI: treat as `REVIEW` and continue after the receipt comment. Server: refuse merge, mark `needs_human`, and record a failed merge operation. |

Exit 10 is an explicit checker `BLOCK`. A reported `BLOCK` in a receipt with exit 0 is treated as `REVIEW` by the CLI. Server gate failures, including an unreadable receipt, also prevent merge. If recording the task context or posting a `REVIEW` comment fails, the CLI warns; that failure does not block the merge. The independent review check below can still prevent it.

## Independent review status before merge

After the gate allows a merge, `crew run --auto-merge` checks the latest completed review for the PR. It requires an `approve` verdict, a `reviewed_sha` equal to the current PR head, and a reviewer agent different from the implement task's agent; both agents come from `task_attribution`. Only then does it publish a successful `crew/independent-review` status on that head, naming the review task and reviewer agent, before calling `gh pr merge`. Missing evidence, a moved head, the same agent on both tasks, or status-publication failure prevents the merge. The server auto-merge path applies the same checks. Neither path uses `--admin` to bypass branch protection.
