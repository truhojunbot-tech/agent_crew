# agent_crew — Test Plan

> status: draft
> version: 0.1
> see also: requirements.md, architecture.md

---

## 1. Test Strategy

| Level | Scope | Dependencies | Runner |
|-------|-------|-------------|--------|
| Unit | Single module, pure logic | None (mocks for DB/tmux/HTTP) | pytest |
| Integration | Module interactions, real DB | SQLite (temp), FastAPI TestClient | pytest + httpx |
| System (E2E) | Full CLI workflows | tmux, git, real filesystem | pytest + subprocess |

**Conventions**:
- All tests under `tests/`
- Fixtures in `tests/conftest.py`
- Temp directories via `tmp_path` fixture (no real `/tmp/agent_crew` pollution)
- SQLite uses `:memory:` or temp file for unit/integration tests

---

## 2. Unit Tests

### 2.1 protocol.py — `test_protocol.py`

| # | Test | Verify |
|---|------|--------|
| U-P01 | TaskRequest valid construction | All fields set, defaults applied (priority=3, context={}) |
| U-P02 | TaskRequest rejects invalid task_type | ValueError on `task_type="unknown"` |
| U-P03 | TaskResult valid construction | status, summary, verdict, findings, pr_number |
| U-P04 | TaskResult rejects invalid status | ValueError on `status="random"` |
| U-P05 | GateRequest valid construction | id, type, message, status="pending", created_at auto |
| U-P06 | GateRequest rejects invalid type | ValueError on `type="unknown"` |

### 2.2 queue.py — `test_queue.py`

| # | Test | Verify |
|---|------|--------|
| U-Q01 | Enqueue single task | Task stored, status=pending |
| U-Q02 | Enqueue respects priority ordering | Higher priority (lower number) dequeued first |
| U-Q03 | Dequeue returns highest-priority pending task | Correct task returned, status→assigned |
| U-Q04 | Dequeue with role filter | Only matching role tasks returned |
| U-Q05 | Dequeue empty queue returns None | No error, returns None |
| U-Q06 | Dequeue atomicity — no double assignment | Two concurrent dequeue calls get different tasks |
| U-Q07 | Submit result updates task | status→completed, result fields stored |
| U-Q08 | Submit result for nonexistent task | Raises error / returns 404 |
| U-Q09 | Cancel task | status→cancelled |
| U-Q10 | List tasks by status filter | Returns correct subset |
| U-Q11 | Persistence — tasks survive reconnect | Close DB, reopen, pending tasks still there |
| U-Q12 | Gate create | Gate stored, status=pending |
| U-Q13 | Gate resolve approved | status→approved |
| U-Q14 | Gate resolve rejected | status→rejected |
| U-Q15 | Gate list pending | Only pending gates returned |
| U-Q16 | Gate resolve nonexistent | Raises error |
| U-Q17 | Gate resolve already resolved | Raises error (idempotency guard) |

### 2.3 session.py — `test_session.py`

| # | Test | Verify |
|---|------|--------|
| U-S01 | Load sessions.json | Parses all agent entries correctly |
| U-S02 | Save sessions.json | Written JSON matches expected structure |
| U-S03 | Check refresh needed — age exceeded | `started_at` > SESSION_MAX_HOURS → True |
| U-S04 | Check refresh needed — failures exceeded | `failures` >= SESSION_MAX_FAILURES → True |
| U-S05 | Check refresh not needed | Fresh session, 0 failures → False |
| U-S06 | Increment failure count | failures counter updated in state |
| U-S07 | Reset after refresh | started_at=now, failures=0 |
| U-S08 | Health check — pane alive (mock tmux) | Returns True when capture-pane returns content |
| U-S09 | Health check — pane dead (mock tmux) | Returns False when capture-pane fails |
| U-S10 | Refresh uses cmd from sessions.json | Correct agent-specific command sent to tmux |

### 2.4 instructions.py — `test_instructions.py`

| # | Test | Verify |
|---|------|--------|
| U-I01 | Generate CLAUDE.md for implementer | Contains common section + implementer role |
| U-I02 | Generate AGENTS.md for reviewer | Contains common section + reviewer role |
| U-I03 | Generate GEMINI.md for tester | Contains common section + tester role |
| U-I04 | Generate with custom role | Arbitrary role text injected |
| U-I05 | Port placeholder replaced | `<port>` replaced with actual port or port file path |
| U-I06 | Project name injected | `<project>` replaced in file paths |

### 2.5 triage.py — `test_triage.py`

| # | Test | Verify |
|---|------|--------|
| U-T01 | Parse GitHub issues JSON | Correct extraction of issue number, title, labels |
| U-T02 | Filter already-processed issues | Issues with `agent_crew:done` label excluded |
| U-T03 | Build triage prompt | Prompt contains issues list + recent merge history |
| U-T04 | Parse triage agent response | Extracts selected issue + task description |
| U-T05 | No open issues → early exit | Returns None, no gate created |

### 2.6 discussion.py — `test_discussion.py`

| # | Test | Verify |
|---|------|--------|
| U-D01 | Enqueue panel tasks for all agents | One task per agent, task_type=discuss |
| U-D02 | Assign perspectives correctly | Default: analyst, critic, advocate, risk (round-robin if fewer) |
| U-D03 | Assign custom perspectives | `--perspectives` map applied |
| U-D04 | Collect results → build synthesis | synthesis.md has all panel opinions |
| U-D05 | Multi-round: round 2 context includes round 1 synthesis | Previous synthesis in task description |
| U-D06 | --then-run chains to code-review loop | Returns synthesis as task input |

### 2.7 loop.py — `test_loop.py`

| # | Test | Verify |
|---|------|--------|
| U-L01 | Enqueue implement task | task_type=implement, TDD instructions in context |
| U-L02 | On coder completion → enqueue review task | task_type=review, 3-layer checklist in context |
| U-L03 | Review approved → enqueue test task (if tester) | task_type=test |
| U-L04 | Review approved + --no-tester → done | Loop exits successfully |
| U-L05 | Review request_changes → re-enqueue implement | Review layer feedback (which of 3 layers failed) in context |
| U-L06 | Max iterations reached → escalation gate | Gate type=escalation created |
| U-L07 | Test passed → loop complete | Final status = success |
| U-L08 | Test failed → re-enqueue implement | Test feedback in context |
| U-L09 | Review verdict: tests-pass-only not sufficient | request_changes when only happy path tested |
| U-L10 | Review feedback carries layer label | findings tagged with "test_quality" / "code_quality" / "business_gap" |

### 2.8 setup.py — `test_setup.py`

| # | Test | Verify |
|---|------|--------|
| U-SE01 | Validates git repo exists | Error if not in git repo |
| U-SE02 | Creates worktree directories | Correct paths under AGENT_CREW_WORKTREES |
| U-SE03 | Generates instruction files | Correct filename per agent |
| U-SE04 | Writes sessions.json with cmd field | Each agent has correct resume command |
| U-SE05 | Auto port selection | Finds first available port starting from 8100 |
| U-SE06 | Writes port file | Port number written to correct path |
| U-SE07 | Custom agent list | --agents flag respected |

### 2.9 cli.py — `test_cli.py`

| # | Test | Verify |
|---|------|--------|
| U-C01 | `crew --help` exits 0 | Help text shown, all subcommands listed |
| U-C02 | `crew setup` validates required args | Error without project name |
| U-C03 | `crew status` with no project running | Clean message, exit 0 |
| U-C04 | `crew run` validates task description | Error when empty |
| U-C05 | `crew discuss` validates topic | Error when empty |
| U-C06 | `crew teardown` validates project exists | Error for unknown project |

---

## 3. Integration Tests

### 3.1 Server API — `test_server_integration.py`

Uses FastAPI `TestClient` (no real uvicorn, but real SQLite temp DB).

| # | Test | Verify |
|---|------|--------|
| I-SV01 | POST /tasks → GET /tasks/{id} | Task created and retrievable |
| I-SV02 | POST /tasks → GET /tasks/next → POST /tasks/{id}/result | Full task lifecycle |
| I-SV03 | GET /tasks/next concurrent (2 clients) | Each gets a different task (no double-assign) |
| I-SV04 | GET /tasks?status=pending | Filters correctly |
| I-SV05 | DELETE /tasks/{id} | Task cancelled, not returned by next |
| I-SV06 | POST /gates → GET /gates/pending | Gate appears in pending list |
| I-SV07 | POST /gates/{id}/resolve → GET /gates/{id} | Status updated to approved/rejected |
| I-SV08 | POST /gates/{id}/resolve twice | Second call rejected (already resolved) |
| I-SV09 | Priority ordering via API | POST 3 tasks with different priorities, GET /tasks/next returns highest first |
| I-SV10 | Invalid task_type in POST /tasks | 422 validation error |
| I-SV11 | POST /tasks/{id}/result for unknown task | 404 |
| I-SV12 | GET /tasks/next with role filter | Only matching role tasks dequeued |

### 3.2 Queue + Server Persistence — `test_persistence_integration.py`

| # | Test | Verify |
|---|------|--------|
| I-PS01 | Enqueue tasks → stop server → restart → tasks still pending | SQLite persistence works |
| I-PS02 | In-progress task survives restart | Status preserved |
| I-PS03 | Completed tasks queryable after restart | History intact |
| I-PS04 | Gates survive restart | Pending gates still pending |

### 3.3 Session + Setup — `test_session_integration.py`

Requires tmux (skip if not available).

| # | Test | Verify |
|---|------|--------|
| I-SS01 | Create tmux session + panes | Panes exist with correct targets |
| I-SS02 | Health check on live pane | Returns alive |
| I-SS03 | Health check on killed pane | Returns dead |
| I-SS04 | Refresh restarts agent in pane | New process running, sessions.json updated |
| I-SS05 | sessions.json round-trip | Write → read → matches |

### 3.4 Discussion Orchestration — `test_discussion_integration.py`

Uses real server (TestClient) + mock agent responses.

| # | Test | Verify |
|---|------|--------|
| I-DI01 | Full single-round discussion | Tasks enqueued → mock results submitted → synthesis.md written |
| I-DI02 | Multi-round discussion (2 rounds) | Round 2 tasks contain round 1 synthesis |
| I-DI03 | Discussion with 2 agents (not 3) | Works with fewer agents |
| I-DI04 | Agent fails during discussion | Failure recorded, synthesis notes missing opinion |

### 3.5 Code-Review Loop Orchestration — `test_loop_integration.py`

Uses real server (TestClient) + mock agent responses.

| # | Test | Verify |
|---|------|--------|
| I-LO01 | Approve on first review | implement → review(approve) → test(pass) → done |
| I-LO02 | Request changes once then approve | implement → review(changes) → implement → review(approve) → done |
| I-LO03 | Max iterations → escalation gate | 5 review cycles → gate created |
| I-LO04 | --no-tester skips test step | implement → review(approve) → done (no test) |
| I-LO05 | Test failure triggers re-implement | implement → review(approve) → test(fail) → implement |
| I-LO06 | Reviewer rejects happy-path-only tests | Mock reviewer returns request_changes with finding tagged "test_quality" |
| I-LO07 | Reviewer approves after edge cases added | Second implement adds edge case tests → reviewer approves |
| I-LO08 | Business gap finding survives into re-implement | reviewer finding tagged "business_gap" present in next implement task context |

### 3.6 Triage + Gate — `test_triage_integration.py`

| # | Test | Verify |
|---|------|--------|
| I-TR01 | Triage → gate created → approved → task enqueued | Full flow with mock gh + mock agent |
| I-TR02 | Triage → gate rejected → skipped | No task enqueued |
| I-TR03 | Triage with no issues → clean exit | No gate, no task |
| I-TR04 | Gate timeout → auto-reject | Gate expires, next cycle proceeds |

---

## 4. System Tests (E2E)

End-to-end tests using real CLI, tmux, and git. Run in isolated temp directory with a throwaway git repo. Agent CLIs are replaced with **stub scripts** that simulate behavior (read task from API, write result).

### 4.1 Prerequisites

```bash
# stub agent: reads task from server, writes a result
#!/bin/bash
PORT=$(cat /tmp/agent_crew/$PROJECT/port)
TASK=$(curl -s "http://localhost:$PORT/tasks/next?agent=$AGENT&role=$ROLE")
# ... process task ...
curl -s -X POST "http://localhost:$PORT/tasks/$TASK_ID/result" -d '{"status":"completed","summary":"done"}'
```

### 4.2 Setup + Teardown — `test_e2e_setup.py`

| # | Test | Verify |
|---|------|--------|
| E-ST01 | `crew setup testproj` in git repo | Worktrees created, panes exist, server running, port file written |
| E-ST02 | `crew status testproj` after setup | Shows all agents alive, server port, 0 tasks |
| E-ST03 | `crew teardown testproj` | Worktrees removed, panes closed, server stopped, port file deleted |
| E-ST04 | `crew setup` outside git repo | Error: "not a git repository" |
| E-ST05 | `crew setup` with custom agents | Only specified agents get panes/worktrees |
| E-ST06 | Double `crew setup` same project | Error or idempotent (no duplicate panes) |

### 4.3 Code-Review Loop E2E — `test_e2e_loop.py`

Stub coder: writes tests first, then implementation. Stub reviewer: checks 3 layers.

| # | Test | Verify |
|---|------|--------|
| E-LO01 | `crew run "add hello.py"` → stub coder (TDD) → stub reviewer approves all 3 layers | Task completes, PR simulated |
| E-LO02 | Stub reviewer rejects for test_quality → stub coder adds edge cases → approved | 2 implement cycles, rejection carries layer label |
| E-LO03 | `crew run --max-iter 2` with perpetual rejection | Escalation gate created after 2 iterations |
| E-LO04 | `crew run --no-tester` | No test task enqueued |

### 4.4 Discussion Loop E2E — `test_e2e_discussion.py`

| # | Test | Verify |
|---|------|--------|
| E-DI01 | `crew discuss "should we use REST or gRPC?"` | All stub agents respond, synthesis.md written |
| E-DI02 | `crew discuss --rounds 2` | Two rounds, round 2 references round 1 |
| E-DI03 | `crew discuss --then-run` | Synthesis → automatically triggers code-review loop |

### 4.5 Triage + Poll E2E — `test_e2e_triage.py`

| # | Test | Verify |
|---|------|--------|
| E-TR01 | `crew triage --repo mock/repo` | Stub agent selects issue, gate created |
| E-TR02 | `crew triage --no-confirm` | Skips gate, immediately enqueues task |
| E-TR03 | `crew poll --interval 10s` (short for test) | At least 2 triage cycles observed |

### 4.6 Recovery E2E — `test_e2e_recovery.py`

| # | Test | Verify |
|---|------|--------|
| E-RC01 | Kill tmux session → `crew recover testproj` | Panes recreated, agents restarted with --continue |
| E-RC02 | Kill server process → `crew recover testproj` | Server restarted, pending tasks preserved |
| E-RC03 | Enqueue task → kill server → recover → task still pending | SQLite persistence verified end-to-end |
| E-RC04 | `crew recover` with no prior setup | Error: "no session found for project" |

### 4.7 Gate E2E — `test_e2e_gates.py`

| # | Test | Verify |
|---|------|--------|
| E-GA01 | Gate created → resolve via API → loop continues | Approval unblocks workflow |
| E-GA02 | Gate created → reject via API → loop skips | Rejection handled gracefully |
| E-GA03 | Gate timeout | After timeout, auto-rejected, loop proceeds |
| E-GA04 | Multiple pending gates | All listed in GET /gates/pending |

---

## 5. Test Infrastructure

### 5.1 Fixtures (`conftest.py`)

```python
@pytest.fixture
def tmp_project(tmp_path):
    """Git repo + agent_crew temp dirs for isolated testing."""

@pytest.fixture
def task_queue(tmp_path):
    """SQLite-backed TaskQueue instance."""

@pytest.fixture
def gate_queue(tmp_path):
    """SQLite-backed GateQueue instance."""

@pytest.fixture
def test_client(task_queue, gate_queue):
    """FastAPI TestClient with real queues."""

@pytest.fixture
def tmux_session():
    """Creates/destroys a tmux session for integration tests. Skips if tmux unavailable."""

@pytest.fixture
def stub_agents(tmp_path):
    """Generates stub agent scripts that interact with the HTTP API."""
```

### 5.2 Markers

```ini
# pyproject.toml
[tool.pytest.ini_options]
markers = [
    "unit: unit tests (no external deps)",
    "integration: requires SQLite, may require tmux",
    "e2e: full system tests, requires tmux + git",
]
```

### 5.3 CI Considerations

```bash
# Unit only (fast, no tmux needed)
pytest -m unit

# Unit + integration
pytest -m "unit or integration"

# Full suite (requires tmux)
pytest
```

---

## 6. Coverage Targets

| Module | Target |
|--------|--------|
| protocol.py | 100% |
| queue.py | 95%+ |
| server.py | 90%+ (API paths) |
| session.py | 85%+ (tmux mock boundaries) |
| instructions.py | 100% |
| triage.py | 85%+ (gh CLI mocked) |
| discussion.py | 90%+ |
| loop.py | 90%+ |
| setup.py | 80%+ (tmux/git boundaries) |
| cli.py | 80%+ (click testing) |

---

## 7. Test File Structure

```
tests/
├── conftest.py                    # shared fixtures
├── unit/
│   ├── test_protocol.py
│   ├── test_queue.py
│   ├── test_session.py
│   ├── test_instructions.py
│   ├── test_triage.py
│   ├── test_discussion.py
│   ├── test_loop.py
│   ├── test_setup.py
│   └── test_cli.py
├── integration/
│   ├── test_server_integration.py
│   ├── test_persistence_integration.py
│   ├── test_session_integration.py
│   ├── test_discussion_integration.py
│   ├── test_loop_integration.py
│   └── test_triage_integration.py
└── e2e/
    ├── test_e2e_setup.py
    ├── test_e2e_loop.py
    ├── test_e2e_discussion.py
    ├── test_e2e_triage.py
    ├── test_e2e_recovery.py
    └── test_e2e_gates.py
```
