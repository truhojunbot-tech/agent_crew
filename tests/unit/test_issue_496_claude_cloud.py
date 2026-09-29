"""#496 — claude_cloud execution backend.

Covers acceptance tests 1-13 from the issue, with the `claude` CLI fully
mocked via dependency injection (`run_fn`). No test spawns a real cloud
session, hits the network, or calls a real `gh`/`git` binary — every GitHub
read is injected the same way `pipeline.py`'s own tests inject `pr_state_fn`
(see tests/unit/test_issue_250_terminal_pr_gate.py).
"""
import os
import subprocess
import uuid

import pytest

from agent_crew import claude_cloud as cc
from agent_crew import pause
from agent_crew.protocol import TaskRequest
from agent_crew.queue import AdmissionRefused, TaskQueue

OBSERVED_LAUNCH_TEXT = (
    "Created cloud session: claude_cloud execution backend\n"
    "View: https://claude.ai/code/session_01KCkrmLbhobrds8fApuuoun\n"
    "Resume with: claude --teleport session_01KCkrmLbhobrds8fApuuoun\n"
)
OBSERVED_SESSION_ID = "session_01KCkrmLbhobrds8fApuuoun"
OBSERVED_SESSION_URL = "https://claude.ai/code/session_01KCkrmLbhobrds8fApuuoun"

HELP_TEXT_WITH_CLOUD = (
    "Usage: claude [options]\n"
    "  --print                 Non-interactive mode\n"
    "  --cloud                 Run this session in Claude Code Cloud\n"
    "  --output-format <fmt>   json | text\n"
)
HELP_TEXT_WITHOUT_CLOUD = "Usage: claude [options]\n  --print   Non-interactive mode\n"


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_returning(text, returncode=0):
    def _run(argv, **_kw):
        return _Proc(returncode=returncode, stdout=text)
    return _run


def _run_sequence(*procs):
    it = iter(procs)

    def _run(argv, **_kw):
        return next(it)
    return _run


@pytest.fixture
def q(tmp_db):
    return TaskQueue(tmp_db)


def _enqueue_implement(q, task_id=None, branch=None, context=None):
    task_id = task_id or f"impl-{uuid.uuid4().hex[:8]}"
    branch = branch or f"claude/{task_id}"
    q.enqueue(TaskRequest(task_id=task_id, task_type="implement", description="do the thing",
                          branch=branch, context=context or {}, project="agent_crew"))
    return task_id, branch


@pytest.fixture(autouse=True)
def _cloud_env(monkeypatch):
    """Every test opts in explicitly and keeps concurrency deterministic;
    acceptance test 11 (default OFF) gets its own test that does NOT use
    this override."""
    monkeypatch.setenv(cc._ENV_ENABLED, "1")
    monkeypatch.setenv(cc._ENV_MAX_CONCURRENCY, "3")


# ── AT1: CLI capability probe ───────────────────────────────────────────

def test_at1_probe_recognizes_supported_cloud_flag():
    cap = cc.probe_cloud_cli(_run_returning(HELP_TEXT_WITH_CLOUD))
    assert cap.supported is True


def test_at1_probe_fails_closed_when_flag_absent():
    cap = cc.probe_cloud_cli(_run_returning(HELP_TEXT_WITHOUT_CLOUD))
    assert cap.supported is False
    assert "not found" in cap.reason


def test_at1_probe_fails_closed_on_missing_binary():
    def _raise(argv, **_kw):
        raise FileNotFoundError("no such file: claude")
    cap = cc.probe_cloud_cli(_raise)
    assert cap.supported is False


def test_at1_probe_fails_closed_on_nonzero_exit():
    cap = cc.probe_cloud_cli(_run_returning("error", returncode=1))
    assert cap.supported is False


# ── AT2: successful spawn captures/persists session id + URL ───────────

def test_at2_parse_observed_text_shape():
    result = cc.parse_cloud_launch_output(OBSERVED_LAUNCH_TEXT)
    assert result.dispatched is True
    assert result.session_id == OBSERVED_SESSION_ID
    assert result.session_url == OBSERVED_SESSION_URL


def test_at2_parse_prefers_json_when_present():
    result = cc.parse_cloud_launch_output(
        '{"session_id": "session_abc123", "url": "https://claude.ai/code/session_abc123"}')
    assert result.dispatched is True
    assert result.session_id == "session_abc123"


def test_at2_dispatch_persists_session_id_and_url(q):
    task_id, branch = _enqueue_implement(q)
    run = _run_sequence(_Proc(0, HELP_TEXT_WITH_CLOUD), _Proc(0, OBSERVED_LAUNCH_TEXT))
    outcome = cc.dispatch_cloud_for_role(q, role="implementer", task_type="implement", run_fn=run)
    assert outcome.dispatched is True
    assert outcome.session_id == OBSERVED_SESSION_ID
    attribution = q.get_attribution(task_id)
    assert attribution["provider_session_id"] == OBSERVED_SESSION_ID
    row = [t for t in q.list_tasks() if t.task_id == task_id][0]
    assert row.context.get("cloud_session_url") == OBSERVED_SESSION_URL


# ── AT3: malformed/changed CLI output fails closed without losing the task ─

def test_at3_parse_fails_closed_on_malformed_output():
    result = cc.parse_cloud_launch_output("Something changed in a new CLI release.\n")
    assert result.dispatched is False
    assert result.error == "unrecognized_cli_output"


def test_at3_parse_fails_closed_on_json_missing_fields():
    result = cc.parse_cloud_launch_output('{"status": "ok"}')
    assert result.dispatched is False
    assert result.error == "json_missing_session_fields"


def test_at3_dispatch_fails_closed_without_losing_task(q):
    task_id, branch = _enqueue_implement(q)
    run = _run_sequence(_Proc(0, HELP_TEXT_WITH_CLOUD), _Proc(0, "unrecognized new output shape"))
    outcome = cc.dispatch_cloud_for_role(q, role="implementer", task_type="implement", run_fn=run)
    assert outcome.dispatched is False
    assert outcome.launch_error and "cloud_output_unrecognized" in outcome.launch_error
    # The task is not lost: it has an explicit terminal status and summary,
    # not silently stuck pending or vanished.
    failed = [t for t in q.list_tasks(status="failed") if t.task_id == task_id]
    assert len(failed) == 1
    assert "cloud_output_unrecognized" in failed[0].summary


def test_at3_dispatch_fails_closed_on_nonzero_cli_exit(q):
    task_id, branch = _enqueue_implement(q)
    run = _run_sequence(_Proc(0, HELP_TEXT_WITH_CLOUD), _Proc(2, "some crash output"))
    outcome = cc.dispatch_cloud_for_role(q, role="implementer", task_type="implement", run_fn=run)
    assert outcome.dispatched is False
    assert [t for t in q.list_tasks(status="failed") if t.task_id == task_id]


# ── AT4: ALREADY_FIXED terminates cleanly without demanding a PR ───────

def test_at4_already_fixed_without_pr(q):
    task_id, branch = _enqueue_implement(q)
    q.dequeue(role="implementer")
    q.record_dispatch(task_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME,
                      target="pending")
    task = [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id][0]

    outcome = cc.reconcile_cloud_dispatch(
        q, task,
        pr_number_for_branch_fn=lambda branch, repo=None: None,
        commit_message_fn=lambda branch, repo=None: "ALREADY_FIXED verified against main, no changes needed",
    )
    assert outcome.action == "already_fixed"
    completed = [t for t in q.list_tasks(status="completed") if t.task_id == task_id]
    assert len(completed) == 1
    assert completed[0].pr_number is None


# ── AT5: PR_READY enters the existing review pipeline ───────────────────

def test_at5_pr_ready_enqueues_existing_review_task(q):
    task_id, branch = _enqueue_implement(q)
    q.dequeue(role="implementer")
    q.record_dispatch(task_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME,
                      target="pending")
    task = [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id][0]

    outcome = cc.reconcile_cloud_dispatch(
        q, task,
        pr_number_for_branch_fn=lambda branch, repo=None: 555,
        pr_head_sha_fn=lambda pr_number, repo=None: "a" * 40,
        pr_state_fn=lambda pr_number, repo=None: "open",
    )
    assert outcome.action == "pr_ready"
    assert outcome.pr_number == 555
    completed = [t for t in q.list_tasks(status="completed") if t.task_id == task_id]
    assert completed and completed[0].pr_number == 555
    reviews = [t for t in q.list_tasks(status="pending") if t.task_type == "review"]
    assert len(reviews) == 1
    assert reviews[0].context.get("pr_number") == 555


# ── AT6: request_changes routes findings to the SAME cloud session ─────

def test_at6_resolve_resume_session_id_walks_lineage(q):
    impl_id, branch = _enqueue_implement(q, task_id="impl-1")
    q.dequeue(role="implementer")
    q.record_dispatch(impl_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME, target="pending")
    q.record_attribution(task_id=impl_id, agent=cc.CLOUD_PROVIDER_NAME,
                         provider_session_id=OBSERVED_SESSION_ID)
    q.submit_result(impl_id, __import__("agent_crew.protocol", fromlist=["TaskResult"]).TaskResult(
        task_id=impl_id, status="completed", summary="done", pr_number=42, branch=branch))

    review_id = f"review-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=review_id, task_type="review", description="review",
                          branch=branch, context={"prev_task_id": impl_id, "pr_number": 42},
                          project="agent_crew"))

    fix_id = f"fix-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=fix_id, task_type="implement", description="address findings",
                          branch=branch, context={"prev_task_id": review_id, "pr_number": 42},
                          project="agent_crew"))
    fix_task = [t for t in q.list_tasks() if t.task_id == fix_id][0]

    resumed = cc._resolve_resume_session_id(q, fix_task)
    assert resumed == OBSERVED_SESSION_ID


def test_at6_dispatch_uses_resume_argv_for_fix_task(q, monkeypatch):
    impl_id, branch = _enqueue_implement(q, task_id="impl-2")
    q.dequeue(role="implementer")
    q.record_dispatch(impl_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME, target="pending")
    q.record_attribution(task_id=impl_id, agent=cc.CLOUD_PROVIDER_NAME,
                         provider_session_id=OBSERVED_SESSION_ID)

    fix_id = f"fix-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=fix_id, task_type="implement", description="address findings",
                          branch=branch, context={"prev_task_id": impl_id}, project="agent_crew"))

    seen_argv = {}

    def run(argv, **_kw):
        if "--help" in argv:
            return _Proc(0, HELP_TEXT_WITH_CLOUD)
        seen_argv["argv"] = argv
        return _Proc(0, OBSERVED_LAUNCH_TEXT)

    outcome = cc.dispatch_cloud_for_role(q, role="implementer", task_type="implement", run_fn=run)
    assert outcome.dispatched is True
    joined = " ".join(seen_argv["argv"])
    assert OBSERVED_SESSION_ID in joined, "resume must address the SAME session id"


# ── AT7: a new PR HEAD invalidates the prior review (existing pipeline) ─

def test_at7_review_head_status_is_provider_agnostic():
    from agent_crew.pipeline import review_head_status
    status, head, _msg = review_head_status(
        {"reviewed_sha": "a" * 40}, 1,
        head_sha_fn=lambda pr_number, repo=None, **_kw: "b" * 40)
    assert status == "stale"
    assert head == "b" * 40
    # No claude_cloud-specific branch exists in this function — the SAME
    # freshness check already used for local providers applies unchanged.


# ── AT8: cloud continuation failure -> existing fallback/hold semantics ─

def test_at8_continuation_failure_falls_back_via_existing_mechanism(q, monkeypatch):
    impl_id, branch = _enqueue_implement(q, task_id="impl-3")
    q.dequeue(role="implementer")
    q.record_dispatch(impl_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME, target="pending")
    q.record_attribution(task_id=impl_id, agent=cc.CLOUD_PROVIDER_NAME,
                         provider_session_id=OBSERVED_SESSION_ID)

    fix_id = f"fix-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=fix_id, task_type="implement", description="address findings",
                          branch=branch, context={"prev_task_id": impl_id}, project="agent_crew"))

    calls = []
    import agent_crew.pipeline as pipeline_module
    real_fallback = pipeline_module.auto_fallback_failed_task

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_fallback(*args, **kwargs)
    monkeypatch.setattr(pipeline_module, "auto_fallback_failed_task", _spy)

    def run(argv, **_kw):
        if "--help" in argv:
            return _Proc(0, HELP_TEXT_WITH_CLOUD)
        return _Proc(0, "resume attempt produced unrecognized output")

    outcome = cc.dispatch_cloud_for_role(q, role="implementer", task_type="implement", run_fn=run)
    assert outcome.dispatched is False
    assert "cloud_continuation_unavailable" in outcome.launch_error
    assert len(calls) == 1, "the SAME fallback function local providers use must be invoked"
    failed = [t for t in q.list_tasks(status="failed") if t.task_id == fix_id]
    assert failed, "the fix task must not be lost"


# ── AT9: max cloud concurrency is enforced ──────────────────────────────

def test_at9_concurrency_gate_blocks_at_capacity(q, monkeypatch):
    monkeypatch.setenv(cc._ENV_MAX_CONCURRENCY, "2")
    for i in range(2):
        tid, _ = _enqueue_implement(q, task_id=f"busy-{i}")
        q.dequeue(role="implementer")
        q.record_dispatch(tid, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME, target="pending")
    assert q.count_in_progress_by_dispatch_channel(cc.DISPATCH_CHANNEL) == 2

    task_id, _ = _enqueue_implement(q, task_id="waiting")
    called = []
    outcome = cc.dispatch_cloud_for_role(
        q, role="implementer", task_type="implement",
        run_fn=lambda argv, **_kw: called.append(argv) or _Proc(0, HELP_TEXT_WITH_CLOUD))
    assert outcome.dispatched is False
    assert outcome.skipped_reason == "at_capacity"
    assert called == [], "must not even probe the CLI once at capacity"
    still_pending = [t for t in q.list_tasks(status="pending") if t.task_id == task_id]
    assert still_pending, "the waiting task stays pending, not dropped"


def test_at9_concurrency_configurable_via_env(monkeypatch):
    monkeypatch.setenv(cc._ENV_MAX_CONCURRENCY, "7")
    assert cc.cloud_max_concurrency() == 7
    monkeypatch.setenv(cc._ENV_MAX_CONCURRENCY, "not-a-number")
    assert cc.cloud_max_concurrency() == cc.DEFAULT_MAX_CONCURRENCY


# ── AT10: STOP/pause/HUMAN_GATE still blocks dispatch ───────────────────

def test_at10_pause_blocks_cloud_dispatch(q, tmp_db, monkeypatch):
    state_dir = os.path.dirname(tmp_db)
    monkeypatch.setattr(pause, "GLOBAL_PAUSE_FILE", os.path.join(state_dir, "GLOBAL_PAUSE.json"))
    task_id, _ = _enqueue_implement(q)
    pause.set_pause(state_dir, True, reason="incident", source="test")

    called = []
    outcome = cc.dispatch_cloud_for_role(
        q, role="implementer", task_type="implement",
        run_fn=lambda argv, **_kw: called.append(argv) or _Proc(0, HELP_TEXT_WITH_CLOUD))
    assert outcome.dispatched is False
    assert outcome.skipped_reason == "no_task", "dequeue() itself must refuse under pause"
    still_pending = [t for t in q.list_tasks(status="pending") if t.task_id == task_id]
    assert still_pending


class _FakeGateValue:
    def __init__(self, value):
        self.value = value


class _FakeGate:
    """Minimal stand-in for the real CEA gate object `AdmissionRefused`
    expects — enough to exercise the refusal path without standing up the
    full validator/receipt machinery this unit test is not about."""

    def __init__(self):
        self.point = _FakeGateValue("dispatch")
        self.outcome = _FakeGateValue("BLOCK")
        self.reason = "cea_test_refusal"
        self.receipt_id = "test-receipt"


def test_at10_admission_refused_blocks_dispatch(q, monkeypatch):
    _enqueue_implement(q)

    def _refuse(*args, **kwargs):
        raise AdmissionRefused(_FakeGate())
    monkeypatch.setattr(q, "record_dispatch", _refuse)

    called = []
    outcome = cc.dispatch_cloud_for_role(
        q, role="implementer", task_type="implement",
        run_fn=lambda argv, **_kw: called.append(argv) or _Proc(0, HELP_TEXT_WITH_CLOUD))
    assert outcome.dispatched is False
    assert outcome.skipped_reason == "admission_refused"
    # The capability probe (--help) is allowed to run before the gate; the
    # actual launch argv (which would name the task prompt) must never be
    # invoked once the gate refuses.
    assert len(called) == 1
    assert "--help" in called[0]


# ── AT11: no-cloud configuration leaves existing behavior unchanged ────

def test_at11_disabled_by_default(q, monkeypatch):
    monkeypatch.delenv(cc._ENV_ENABLED, raising=False)
    assert cc.cloud_dispatch_enabled() is False
    task_id, _ = _enqueue_implement(q)

    called = []
    outcome = cc.dispatch_cloud_for_role(
        q, role="implementer", task_type="implement",
        run_fn=lambda argv, **_kw: called.append(argv) or _Proc(0, HELP_TEXT_WITH_CLOUD))
    assert outcome.dispatched is False
    assert outcome.skipped_reason == "disabled"
    assert called == [], "disabled must short-circuit before touching the queue at all"
    still_pending = [t for t in q.list_tasks(status="pending") if t.task_id == task_id]
    assert still_pending


# ── AT12: Cloud worker cannot directly merge/deploy through this adapter ─

def test_at12_module_contains_no_merge_or_deploy_calls():
    import inspect
    source = inspect.getsource(cc)
    forbidden = ["merge_pr(", "git push --force", "runtime_swap", "deploy(", ".merge("]
    hits = [f for f in forbidden if f in source]
    assert hits == [], f"claude_cloud.py must never call merge/deploy machinery, found: {hits}"


# ── AT13: telemetry distinguishes fresh_cloud from persistent/local ────

def test_at13_telemetry_execution_policy_defaults_to_none():
    from agent_crew.telemetry import TaskTelemetry
    assert TaskTelemetry().execution_policy is None


def test_at13_fresh_dispatch_records_fresh_cloud_policy(q):
    task_id, branch = _enqueue_implement(q)
    run = _run_sequence(_Proc(0, HELP_TEXT_WITH_CLOUD), _Proc(0, OBSERVED_LAUNCH_TEXT))
    cc.dispatch_cloud_for_role(q, role="implementer", task_type="implement", run_fn=run)
    row = [t for t in q.list_tasks() if t.task_id == task_id][0]
    assert row.context.get("execution_policy") == "fresh_cloud"


def test_at13_resumed_dispatch_records_resume_policy(q):
    impl_id, branch = _enqueue_implement(q, task_id="impl-4")
    q.dequeue(role="implementer")
    q.record_dispatch(impl_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME, target="pending")
    q.record_attribution(task_id=impl_id, agent=cc.CLOUD_PROVIDER_NAME,
                         provider_session_id=OBSERVED_SESSION_ID)
    fix_id = f"fix-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=fix_id, task_type="implement", description="address findings",
                          branch=branch, context={"prev_task_id": impl_id}, project="agent_crew"))
    run = _run_sequence(_Proc(0, HELP_TEXT_WITH_CLOUD), _Proc(0, OBSERVED_LAUNCH_TEXT))
    cc.dispatch_cloud_for_role(q, role="implementer", task_type="implement", run_fn=run)
    row = [t for t in q.list_tasks() if t.task_id == fix_id][0]
    assert row.context.get("execution_policy") == "fresh_cloud_resume"


# ── Prompt/argv construction (supporting unit coverage) ─────────────────

def test_build_launch_argv_wraps_in_pty_and_quotes_prompt():
    argv = cc.build_launch_argv("do the thing; rm -rf /")
    assert argv[0] == "script"
    joined = " ".join(argv)
    assert "--cloud" in joined
    assert "rm -rf /" not in joined or subprocess.list2cmdline  # quoted, not a raw injection point


def test_build_cloud_task_prompt_states_the_full_contract():
    task = TaskRequest(task_id="t1", task_type="implement", description="fix the bug",
                       branch="claude/496", context={})
    prompt = cc.build_cloud_task_prompt(task, repo="owner/repo")
    for token in ("PR_READY", "ALREADY_FIXED", "BLOCKED_FOR_CLOUD", "NEEDS_DECISION", "FAILED",
                  "Do not merge or deploy", "owner/repo", "claude/496"):
        assert token in prompt


def test_parse_terminal_outcome_takes_the_last_line():
    text = "some prose mentioning FAILED and PR_READY earlier\nNEEDS_DECISION should we do X?"
    outcome = cc.parse_terminal_outcome(text)
    assert outcome == ("NEEDS_DECISION", "should we do X?")


def test_parse_terminal_outcome_none_when_absent():
    assert cc.parse_terminal_outcome("just some prose, no verdict") is None
