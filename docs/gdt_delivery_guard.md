# G_DT — DELIVERY_TOPOLOGY_HAZARD guard

SEV-0 gate G_DT (alfred#51, gap map `ccb9485`). Branch `sev0/gdt-delivery-guard`.
Not deployed.

## discovery

- `AGENT_CREW_DELIVERY` defaults to `both` (`server.py`), so tmux push stays on
  alongside the dispatcher.
- Owned panes on :8100, :8101, :8104, :8105, :8106 and :8107 run
  `crew-log-viewer` (#182). A push into them lands in a log viewer, not an
  agent. :8101 recorded 4 pushes in 7 days (from the gap map; not re-measured here).
- The #373 guards refuse unknown tasks and foreign panes. They accept an owned
  pane whatever runs in it.
- On this host, `#{pane_current_command}` cannot tell those panes apart:
  `python3` is both `crew-log-viewer` and the claude quota wrapper
  (`python3 ~/.local/bin/claude`), and `node` is codex and gemini as well as
  unrelated tools. The native claude binary reports its version, e.g. `2.1.185`.

## result — the guard

`_pane_process_kind(pane_id)` reads the pane's `#{pane_pid}` and its whole
process tree from `ps`. It returns one of `agent`, `log_viewer`, `shell` or
`unknown`. A name is matched on argv[0], or on argv[1] under an interpreter.

`_guard_tmx_push` (#373's boundary) now also requires `agent`. Any other
verdict is:

- **refused**: nothing is typed into the pane;
- **logged**: `refusing tmux dispatch task_id=… target=… resolved=…
  reason=pane_not_agent_<verdict> detail=current_command=… refusals=N delivery=…`;
- **counted**: `app.state.delivery_guard_refusals` and `/health` →
  `delivery_guard.refusals`, keyed by reason;
- **backed off, then ended visibly**: the task goes back to pending with a
  per-(task, pane) refusal count (`context.push_refusals`) and an
  exponential `push_not_before` (`AGENT_CREW_PUSH_REFUSAL_BACKOFF_S`, default
  30s, doubling). Only the tmux push dequeue honours the backoff, so the same
  oldest task cannot hot-loop claim→requeue and starve later tasks, while the
  dispatcher and MCP can still take it. After
  `AGENT_CREW_PUSH_REFUSAL_MAX` (default 3) refusals on one pane, the task
  ends as `needs_human` with summary `push_refused_<reason>` (fix round 1,
  review of `addc29e` P1).

The guard fails closed. If tmux or `ps` fails, or the process is unrecognised,
the verdict is `unknown` and the push is refused. When the tree holds both a
log viewer and an agent, the log viewer wins.

Scope:

| Caller | Process check | On refusal |
|---|---|---|
| `_try_push_next`, `_try_push_discuss` | yes | back off; `needs_human` after N |
| watchdog reminder text | yes | skip; the running task is left alone |
| watchdog busy/timeout probe | no (`require_agent=False`) | — |
| #173 Ctrl+C recovery, timeout Ctrl+C | no | — |

The busy/timeout probe only inspects the pane. If it refused a crashed
(`shell`) pane, the task in that pane would never time out.

Tests: `tests/unit/test_gdt_delivery_guard.py`. The suite defaults the probe to
`agent` through the autouse fixture `_mock_pane_process_kind`, because fixture
pane ids are not real panes. The marker `real_pane_process_kind` opts out.

## proposal — the fleet `AGENT_CREW_DELIVERY` value (Alfred / owner decides)

This branch does **not** change the default. The options:

| Option | Effect | Trade-off |
|---|---|---|
| A. Keep `both`, rely on this guard | Pushes into viewer panes are refused and counted | Keeps a live push path that does nothing useful in dispatcher mode. Every enqueue pays a claim → probe → requeue round trip, and every refusal is a log line. Legacy interactive panes keep working. |
| B. `mcp` fleet-wide for dispatcher instances | No tmux push at all | Matches the :8102 deviation (G2). Pending tasks are auto-failed after the stale window if nothing dequeues (#145). Needs confirmation that the dispatcher dequeues independently of `DELIVERY`. |
| C. Derive it: `AGENT_CREW_DISPATCHER=1` ⇒ push off unless set explicitly | Topology and delivery cannot disagree | Changes a default. Instances that relied on implicit `both` + dispatcher change behaviour on restart. |

Recommendation: deploy the guard first; it is safe under every option. Then
choose B or C as the G2 normalisation, with the refusal counter as the
before/after evidence. Not deciding costs little while the guard is deployed.
Until then, every push into a log viewer is a lost task block.
