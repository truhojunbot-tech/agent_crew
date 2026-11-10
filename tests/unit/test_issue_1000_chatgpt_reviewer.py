"""#1000: the ``chatgpt`` reviewer backend -- request comment, strict reply,
fail-closed ends. GitHub is a fake with the four calls the backend makes."""

import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_crew import chatgpt_review as chatgpt
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.role_mapping import (
    RoleResolutionError, effective_role_mapping, provider_family,
    validate_explicit_role_agents,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40
BASE = "c" * 40
PR = 1000
REPO = "owner/repo"
INTAKE = "owner/intake#7"
ROLES = {"implementer": "claude", "reviewer": "chatgpt", "tester": "script"}
FINDING = "src/agent_crew/queue.py:42 must keep the review cascade intact"


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGh:
    def __init__(self):
        self.threads: dict[tuple[str, int], list[dict]] = {}
        self.posted: list[tuple[str, int, str]] = []
        self.next_id = 100
        self.pull_body = "## Tests\nfull suite: 5120 passed, 3 failed (main: 5118 passed, 3 failed)\n"

    def pull(self, repo, number):
        return {"base": {"sha": BASE}, "body": self.pull_body, "state": "open",
                "html_url": f"https://github.com/{repo}/pull/{number}"}

    def pull_files(self, repo, number):
        return ["src/agent_crew/chatgpt_review.py", "tests/unit/test_x.py"]

    def comments(self, repo, number):
        return list(self.threads.get((repo, number), []))

    def post_comment(self, repo, number, body):
        self.next_id += 1
        self.posted.append((repo, number, body))
        row = {"id": self.next_id, "body": body, "created_at": _iso(time.time()),
               "html_url": f"https://github.com/{repo}/issues/{number}#c{self.next_id}"}
        self.threads.setdefault((repo, number), []).append(row)
        return row

    def reply(self, repo, number, body, created_at=None):
        self.next_id += 1
        self.threads.setdefault((repo, number), []).append({
            "id": self.next_id, "body": body,
            "created_at": _iso(time.time() if created_at is None else created_at)})


@pytest.fixture
def queue(tmp_db):
    return TaskQueue(tmp_db)


@pytest.fixture
def notices(monkeypatch):
    seen = []
    monkeypatch.setattr("agent_crew.server.coordinator_notice_once",
                        lambda queue, task_id, kind, detail="", *a, **kw:
                        seen.append((task_id, kind, detail)))
    return seen


@pytest.fixture
def gh():
    return FakeGh()


def _enqueue(queue, *, implementer="claude", task_id="review-impl-1000-r0", **extra):
    queue.enqueue(TaskRequest(
        task_id="impl-1000", task_type="implement",
        description="Acceptance: (1) request comment format (2) matching reply",
        branch="agent/chatgpt", project="agent_crew", context={"repo": REPO}))
    context = {"pr_number": PR, "reviewed_sha": SHA, "repo": REPO, "prev_task_id": "impl-1000",
               "issue_number": 1000, "implementer_agent": implementer, **extra}
    queue.enqueue(TaskRequest(task_id=task_id, task_type="review", description="Review PR",
                              branch="agent/chatgpt", project="agent_crew", context=context))
    return task_id


def _dispatch(queue, gh, **kw):
    return chatgpt.dispatch_chatgpt_review(queue, role_to_agent=dict(ROLES), gh=gh, **kw)


def _thread(carrier):
    return (REPO, PR) if carrier == "pr" else ("owner/intake", 7)


@pytest.fixture(params=["pr", "intake"])
def carrier(request, monkeypatch):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, request.param)
    monkeypatch.setenv(chatgpt._ENV_INTAKE, INTAKE)
    return request.param


def _reply(task_id="review-impl-1000-r0", sha=SHA, verdict: "str | None" = "VERDICT: request_changes",
           text=FINDING):
    return "\n".join(line for line in (chatgpt.reply_header(task_id, sha), verdict, text)
                     if line is not None)


# (1) -----------------------------------------------------------------------

def test_request_comment_format_on_each_carrier(queue, gh, notices, carrier):
    task_id = _enqueue(queue)
    outcome = _dispatch(queue, gh)
    assert outcome.action == "dispatched", outcome
    assert len(gh.posted) == 1
    repo, number, body = gh.posted[0]
    assert (repo, number) == _thread(carrier)
    first = body.splitlines()[0]
    assert first == (f"[REVIEW REQUEST][crew:agent_crew] task={task_id} pr=#{PR} "
                     f"sha={SHA} base={BASE}")
    assert re.fullmatch(r"\[REVIEW REQUEST\]\[crew:\S+\] task=\S+ pr=#\d+ "
                        r"sha=[0-9a-f]{40} base=[0-9a-f]{40}", first)
    assert f"https://github.com/{REPO}/pull/{PR}" in body  # the intake request links the PR
    assert f"https://github.com/{REPO}/issues/1000" in body
    assert "Acceptance: (1) request comment format" in body
    assert "- src/agent_crew/chatgpt_review.py" in body
    assert "5120 passed, 3 failed" in body
    assert f"[CHATGPT][CREW-REVIEW] task={task_id} sha={SHA}" in body
    assert "VERDICT: approve" in body and "VERDICT: request_changes" in body
    task = queue.get_task(task_id)
    assert task.status == "in_progress"
    assert task.context["chatgpt_thread"] == {"repo": _thread(carrier)[0],
                                              "number": _thread(carrier)[1]}
    assert queue.get_attribution(task_id)["agent"] == "chatgpt"
    assert notices == []


# (2) -----------------------------------------------------------------------

@pytest.mark.parametrize("verdict", ["approve", "request_changes"])
def test_matching_reply_becomes_review_result(queue, gh, notices, carrier, verdict):
    task_id = _enqueue(queue)
    _dispatch(queue, gh)
    gh.reply(*_thread(carrier), _reply(verdict=f"VERDICT: {verdict}"))
    submitted = []
    outcome = chatgpt.reconcile_chatgpt_reviews(
        queue, gh=gh, submit_review_result_fn=lambda tid, result:
        submitted.append((tid, result)) or {"status": "ok"})
    assert [o.action for o in outcome] == ["review_completed"]
    (tid, result), = submitted
    assert tid == task_id and result.verdict == verdict and result.pr_number == PR
    if verdict == "request_changes":
        assert result.findings == [FINDING]
    else:
        assert result.findings == [] and FINDING in result.summary


def test_reply_feeds_the_same_server_fix_cascade(tmp_path, monkeypatch, gh, notices):
    import agent_crew.server as server
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    fix_calls = []
    monkeypatch.setattr(server, "review_publication_decision",
                        lambda *_a, **_kw: SimpleNamespace(publish=True))
    monkeypatch.setattr(server, "_pipeline_auto_enqueue_fix",
                        lambda queue, task_id, **_kw: fix_calls.append(task_id) or None)
    monkeypatch.setattr("agent_crew.github.post_review_comment", lambda **kw: True)
    queue = TaskQueue(str(tmp_path / "chatgpt.db"))
    task_id = _enqueue(queue)
    _dispatch(queue, gh)
    gh.reply(REPO, PR, _reply())
    app = server.create_app(db_path=queue._db_path, project="agent_crew", watchdog_disabled=True)
    with TestClient(app) as client:
        def submit(tid, result):
            response = client.post(f"/tasks/{tid}/result", json=result.__dict__,
                                   headers={"X-Agent-Crew-Project": "agent_crew"})
            assert response.status_code == 200, response.text
            return response.json()
        outcomes = chatgpt.reconcile_chatgpt_reviews(queue, gh=gh, submit_review_result_fn=submit)
    assert [o.action for o in outcomes] == ["review_completed"]
    assert queue.get_task(task_id).status == "completed"
    assert queue.get_result(task_id).findings == [FINDING]
    assert fix_calls == [task_id]


def test_only_comments_after_the_request_on_its_thread_count(queue, gh, notices, monkeypatch):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "intake")
    monkeypatch.setenv(chatgpt._ENV_INTAKE, INTAKE)
    _enqueue(queue)
    gh.reply("owner/intake", 7, _reply(verdict="VERDICT: approve", text=""))  # before
    _dispatch(queue, gh)
    gh.reply(REPO, PR, _reply(verdict="VERDICT: approve", text=""))  # other thread
    gh.reply("owner/intake", 7, _reply(task_id="review-other", verdict="VERDICT: approve"))
    outcomes = chatgpt.reconcile_chatgpt_reviews(
        queue, gh=gh, submit_review_result_fn=lambda *_: pytest.fail("not a reply to us"))
    assert [o.action for o in outcomes] == ["still_waiting"]
    assert notices == []


def test_thread_is_read_at_most_once_per_poll_interval(queue, gh, notices, monkeypatch):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    reads = []
    original = gh.comments
    gh.comments = lambda *a: reads.append(a) or original(*a)
    for now in (1001.0, 1100.0, 1301.0):
        chatgpt.reconcile_chatgpt_reviews(queue, gh=gh, now=now)
    assert len(reads) == 2  # 1001 and 1301 (300 s default poll)


# (3) (4) -------------------------------------------------------------------

@pytest.mark.parametrize("body,reason", [
    (_reply(sha=OTHER_SHA), "sha mismatch"),
    (_reply(verdict=None), "missing VERDICT"),
    (_reply(verdict="VERDICT: approve\nVERDICT: request_changes"), "2 VERDICT lines"),
    (_reply(verdict="VERDICT: approved"), "garbled VERDICT"),
    (_reply(verdict="verdict: approve"), "garbled VERDICT"),
    (_reply(verdict="VERDICT: request_changes", text=""), "request_changes without findings"),
    ("[CHATGPT][CREW-REVIEW] task=review-impl-1000-r0 sha=", "garbled reply header"),
])
def test_bad_reply_is_ignored_and_ends_needs_human(queue, gh, notices, carrier, body, reason):
    task_id = _enqueue(queue)
    _dispatch(queue, gh)
    gh.reply(*_thread(carrier), body)
    outcomes = chatgpt.reconcile_chatgpt_reviews(
        queue, gh=gh, submit_review_result_fn=lambda *_: pytest.fail("bad reply submitted"))
    assert [o.action for o in outcomes] == ["needs_human"]
    assert queue.get_task(task_id).status == "needs_human"
    assert queue.get_result(task_id).verdict is None
    (tid, kind, detail), = notices
    assert tid == task_id and kind == "chatgpt_review_reply_rejected" and reason in detail


# (5) -----------------------------------------------------------------------

def test_no_reply_within_timeout_ends_timed_out_never_approve(queue, gh, notices, monkeypatch):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    monkeypatch.setenv(chatgpt._ENV_TIMEOUT_S, "600")
    task_id = _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    assert [o.action for o in chatgpt.reconcile_chatgpt_reviews(queue, gh=gh, now=1500.0)] \
        == ["still_waiting"]
    gh.reply(REPO, PR, "looks fine to me, approve")  # prose has no authority
    outcomes = chatgpt.reconcile_chatgpt_reviews(
        queue, gh=gh, now=1601.0, submit_review_result_fn=lambda *_: pytest.fail("approved"))
    assert [o.action for o in outcomes] == ["timed_out"]
    task = queue.get_task(task_id)
    assert task.status == "timed_out"
    result = queue.get_result(task_id)
    assert result is None or result.verdict is None
    assert [kind for _, kind, _ in notices] == ["chatgpt_review_timed_out"]


@pytest.mark.parametrize("verdict", ["approve", "request_changes"])
def test_reply_posted_before_deadline_but_read_after_it_is_accepted(
        queue, gh, notices, monkeypatch, verdict):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    monkeypatch.setenv(chatgpt._ENV_TIMEOUT_S, "600")
    task_id = _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    gh.reply(REPO, PR, _reply(verdict=f"VERDICT: {verdict}"), created_at=1599.0)
    submitted = []
    outcomes = chatgpt.reconcile_chatgpt_reviews(  # first read after an outage
        queue, gh=gh, now=5000.0,
        submit_review_result_fn=lambda tid, result: submitted.append(result) or {"status": "ok"})
    assert [o.action for o in outcomes] == ["review_completed"]
    (result,) = submitted
    assert result.verdict == verdict
    assert notices == []


@pytest.mark.parametrize("created_at", [1601.0, None])
def test_reply_posted_after_deadline_is_ignored_and_times_out(
        queue, gh, notices, monkeypatch, created_at):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    monkeypatch.setenv(chatgpt._ENV_TIMEOUT_S, "600")
    task_id = _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    gh.reply(REPO, PR, _reply(verdict="VERDICT: approve", text=""), created_at=created_at or 1601.0)
    if created_at is None:  # no readable post time: never accepted either
        gh.threads[(REPO, PR)][-1].pop("created_at")
    outcomes = chatgpt.reconcile_chatgpt_reviews(
        queue, gh=gh, now=5000.0, submit_review_result_fn=lambda *_: pytest.fail("late approve"))
    assert [o.action for o in outcomes] == ["timed_out"]
    assert queue.get_task(task_id).status == "timed_out"
    result = queue.get_result(task_id)
    assert result is None or result.verdict is None
    assert [kind for _, kind, _ in notices] == ["chatgpt_review_timed_out"]


def test_default_timeout_and_poll(monkeypatch):
    monkeypatch.delenv(chatgpt._ENV_TIMEOUT_S, raising=False)
    monkeypatch.delenv(chatgpt._ENV_POLL_S, raising=False)
    assert chatgpt.timeout_seconds() == 43200
    assert chatgpt.poll_seconds() == 300
    monkeypatch.setenv(chatgpt._ENV_POLL_S, "60")
    assert chatgpt.poll_seconds() == 60


# (6) -----------------------------------------------------------------------

@pytest.mark.parametrize("intake", [None, "", "not-a-thread"])
def test_intake_carrier_without_intake_refuses_dispatch_with_notice(
        queue, gh, notices, monkeypatch, intake):
    monkeypatch.delenv(chatgpt._ENV_CARRIER, raising=False)  # intake is the default
    if intake is None:
        monkeypatch.delenv(chatgpt._ENV_INTAKE, raising=False)
    else:
        monkeypatch.setenv(chatgpt._ENV_INTAKE, intake)
    task_id = _enqueue(queue)
    outcome = _dispatch(queue, gh)
    assert outcome.action == "carrier_refused"
    assert gh.posted == []
    assert queue.get_task(task_id).status == "pending"
    (tid, kind, detail), = notices
    assert tid == task_id and kind == "chatgpt_review_carrier_refused"
    assert chatgpt._ENV_INTAKE in detail


def test_unknown_carrier_refuses_dispatch(queue, gh, notices, monkeypatch):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "email")
    _enqueue(queue)
    assert _dispatch(queue, gh).action == "carrier_refused"
    assert gh.posted == [] and len(notices) == 1


# (7) -----------------------------------------------------------------------

def test_claude_implementer_with_chatgpt_reviewer_passes_independence(queue, gh, notices,
                                                                      monkeypatch):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    assert provider_family("chatgpt") == "chatgpt"
    assert provider_family("chatgpt") not in {provider_family(a) for a in
                                              ("claude", "claude_cloud", "codex", "gemini")}
    task_id = _enqueue(queue, implementer="claude")
    # The dispatcher's #986 sweep binds nothing away from chatgpt.
    assert queue.bind_independent_reviews("chatgpt", []) == {"bound": [], "blocked": []}
    guard = lambda task, agent: provider_family(task.context["implementer_agent"]) \
        == provider_family(agent)
    outcome = _dispatch(queue, gh, review_guard_fn=guard)
    assert outcome.action == "dispatched" and outcome.task_id == task_id


# (8) -----------------------------------------------------------------------

@pytest.mark.parametrize("role", ["implementer", "tester"])
def test_chatgpt_may_hold_only_the_reviewer_role(role):
    mapping = {**ROLES, role: "chatgpt", "reviewer": "codex"}
    with pytest.raises(ValueError, match="chatgpt"):
        validate_explicit_role_agents(mapping)
    with pytest.raises(RoleResolutionError):
        effective_role_mapping({"role_agents": mapping})


def test_chatgpt_reviewer_mapping_is_accepted():
    assert effective_role_mapping({"role_agents": dict(ROLES)})[0]["reviewer"] == "chatgpt"


# server wiring ---------------------------------------------------------------

def test_headless_dispatcher_posts_request_and_watchdog_reconciles(tmp_path, monkeypatch,
                                                                    notices):
    import json
    import time

    from agent_crew.server import create_app
    fake = FakeGh()
    monkeypatch.setattr(chatgpt, "GhClient", lambda *a, **kw: fake)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    roles = []
    for role, agent in (("implementer", "claude"), ("tester", "script")):
        worktree = tmp_path / f"wt_{role}"
        (worktree / ".git").mkdir(parents=True)
        roles.append({"role": role, "agent": agent, "worktree": str(worktree)})
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"project": "agent_crew", "role_agents": dict(ROLES),
                                 "roles": roles}))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task_id = _enqueue(queue)
    app = create_app(queue._db_path, state_path=str(state), project="agent_crew",
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not queue.get_task_context(task_id).get(
                "chatgpt_request_comment_id"):
            time.sleep(.02)
        assert queue.get_task(task_id).status == "in_progress"
        assert len(fake.posted) == 1 and fake.posted[0][:2] == (REPO, PR)
        assert queue.get_attribution(task_id)["agent"] == "chatgpt"
        fake.reply(REPO, PR, _reply(sha=OTHER_SHA))
        app.state.watchdog_tick(time.time())
    assert queue.get_task(task_id).status == "needs_human"
    assert [kind for _, kind, _ in notices] == ["chatgpt_review_reply_rejected"]


def test_unreadable_pr_leaves_review_pending_and_posts_nothing(queue, gh, notices, monkeypatch):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    gh.pull = lambda *a: None
    task_id = _enqueue(queue)
    assert _dispatch(queue, gh).action == "github_unavailable"
    assert queue.get_task(task_id).status == "pending" and gh.posted == []
