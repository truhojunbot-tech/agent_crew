"""#224 — continuous GitHub issue ingestion/claim loop (manager watch mode).

`crew triage` was one-shot and the runtime is push-based, so creating a
GitHub issue never enqueued anything by itself. These tests pin the
control-plane path that closes that gap:

    open issue -> discovery -> eligibility -> atomic claim -> enqueue

The claim has to survive two things a plain "add a label" cannot: process
restart and a second manager running concurrently. The ledger's PRIMARY KEY
is the arbiter for both; the GitHub label is the visible half, not the lock.
"""

import sqlite3
import threading
from unittest.mock import MagicMock

import pytest

from agent_crew.queue import TaskQueue
from agent_crew.watch import (
    CLAIM_LABEL,
    ClaimLedger,
    backoff_seconds,
    priority_for,
    run_cycle,
    select_candidates,
)

REPO = "org/repo"


def make_issue(number=1, title="Fix bug", labels=None, body="details"):
    return {
        "number": number,
        "title": title,
        "labels": [{"name": n} for n in (labels or [])],
        "body": body,
    }


class FakeGitHub:
    """In-memory stand-in for the `gh` CLI seam.

    Records label mutations so tests can assert the claim is actually
    GitHub-visible, not merely a local row.
    """

    def __init__(self, issues=None, open_pr_for=None, fail_list_with=None):
        self._issues = list(issues or [])
        self._open_pr_for = set(open_pr_for or ())
        self._fail_list_with = fail_list_with
        self.list_calls = 0
        self.added = []
        self.removed = []

    def list_issues(self, repo):
        self.list_calls += 1
        if self._fail_list_with is not None:
            raise self._fail_list_with
        return [dict(i) for i in self._issues]

    def add_label(self, repo, number, label):
        self.added.append((number, label))
        for issue in self._issues:
            if issue["number"] == number:
                if label not in {l["name"] for l in issue["labels"]}:
                    issue["labels"].append({"name": label})
        return True

    def remove_label(self, repo, number, label):
        self.removed.append((number, label))
        for issue in self._issues:
            if issue["number"] == number:
                issue["labels"] = [l for l in issue["labels"] if l["name"] != label]
        return True

    def issue_has_open_pr(self, repo, number):
        return number in self._open_pr_for


@pytest.fixture
def ledger(tmp_db):
    return ClaimLedger(tmp_db)


@pytest.fixture
def queue(tmp_db):
    return TaskQueue(tmp_db)


# ── 1. discovery → enqueue, exactly once ──────────────────────────────


def test_actionable_issue_is_discovered_and_enqueued_once(queue, ledger):
    """★The whole point of #224 — an open issue becomes queued work."""
    gh = FakeGitHub([make_issue(7, "Fix crash on startup", ["bug"])])

    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)

    assert result["enqueued"] == [7], result
    assert result["error"] is None
    tasks = queue.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].task_type == "implement"
    # Issue metadata must survive into the task so the PR can link back (req 4).
    assert tasks[0].context["issue"] == 7
    assert tasks[0].context["repo"] == REPO
    assert "Fix crash on startup" in tasks[0].description
    # The claim is GitHub-visible (req 2).
    assert (7, CLAIM_LABEL) in gh.added


def test_second_cycle_does_not_re_enqueue_the_same_issue(queue, ledger):
    """⛔A still-open issue must not be picked up again every interval."""
    gh = FakeGitHub([make_issue(7, "Fix crash", ["bug"])])

    first = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)
    second = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)

    assert first["enqueued"] == [7]
    assert second["enqueued"] == []
    assert len(queue.list_tasks()) == 1


# ── 2. skip rules ─────────────────────────────────────────────────────


def test_issue_already_claimed_on_github_is_skipped(queue, ledger):
    """Another manager's claim label is respected even with an empty ledger."""
    gh = FakeGitHub([make_issue(9, "Already taken", ["bug", CLAIM_LABEL])])

    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)

    assert result["enqueued"] == []
    assert queue.list_tasks() == []
    assert gh.added == []


def test_issue_with_active_pr_is_skipped(queue, ledger):
    gh = FakeGitHub([make_issue(11, "Has a PR already", ["bug"])], open_pr_for=[11])

    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)

    assert result["enqueued"] == []
    assert queue.list_tasks() == []


def test_issue_with_active_task_in_queue_is_skipped(queue, ledger):
    """A task already in flight for this issue means the work exists."""
    from agent_crew.protocol import TaskRequest

    queue.enqueue(TaskRequest(
        task_id="impl-existing", task_type="implement",
        description="already working on it", context={"issue": 13, "repo": REPO},
    ))
    gh = FakeGitHub([make_issue(13, "Being worked on", ["bug"])])

    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)

    assert result["enqueued"] == []
    assert len(queue.list_tasks()) == 1


def test_done_labelled_issue_is_skipped(queue, ledger):
    gh = FakeGitHub([make_issue(15, "Finished", ["bug", "agent_crew:done"])])

    assert run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)["enqueued"] == []


def test_issue_blocked_by_open_parent_is_skipped(queue, ledger):
    """Reuses the existing dependency markers — parent still open."""
    gh = FakeGitHub([
        make_issue(20, "Parent work", ["bug"]),
        make_issue(21, "Child work", ["bug"], body="Depends on #20"),
    ])

    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh, max_claims=5)

    assert result["enqueued"] == [20]


# ── 3. no actionable work → no synthetic task ─────────────────────────


def test_no_actionable_issues_creates_no_task(queue, ledger):
    """⛔Never invent work (explicit non-goal of #224)."""
    gh = FakeGitHub([])

    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)

    assert result["enqueued"] == []
    assert result["error"] is None
    assert queue.list_tasks() == []


# ── 4. restart safety ─────────────────────────────────────────────────


def test_restart_does_not_duplicate_an_enqueued_issue(tmp_db):
    """★A fresh process re-reads the ledger from disk and stands down."""
    gh = FakeGitHub([make_issue(31, "Survive restart", ["bug"])])

    run_cycle(queue=TaskQueue(tmp_db), ledger=ClaimLedger(tmp_db), repo=REPO, gh=gh)

    # Simulate a full restart: brand-new objects over the same DB file.
    reborn = TaskQueue(tmp_db)
    result = run_cycle(queue=reborn, ledger=ClaimLedger(tmp_db), repo=REPO, gh=gh)

    assert result["enqueued"] == []
    assert len(reborn.list_tasks()) == 1


def test_claim_survives_a_reopened_ledger_object(tmp_db):
    """The row, not the object, is the state."""
    assert ClaimLedger(tmp_db).try_claim(REPO, 800, owner="a") is True
    assert ClaimLedger(tmp_db).try_claim(REPO, 800, owner="b") is False
    assert ClaimLedger(tmp_db).get(REPO, 800)["owner"] == "a"


# ── 5. concurrency ────────────────────────────────────────────────────


def test_two_ledgers_cannot_both_claim_the_same_issue(tmp_db):
    """★The PRIMARY KEY is the mutex — a label add never could be."""
    a, b = ClaimLedger(tmp_db), ClaimLedger(tmp_db)

    assert a.try_claim(REPO, 42, owner="watcher-a") is True
    assert b.try_claim(REPO, 42, owner="watcher-b") is False


def test_two_concurrent_watchers_enqueue_the_issue_only_once(tmp_db):
    """Two managers racing on one DB — exactly one task."""
    results = []
    barrier = threading.Barrier(2)

    def _worker(name):
        gh = FakeGitHub([make_issue(50, "Contended", ["bug"])])
        q, led = TaskQueue(tmp_db), ClaimLedger(tmp_db)
        barrier.wait()
        results.append(run_cycle(queue=q, ledger=led, repo=REPO, gh=gh, owner=name))

    threads = [threading.Thread(target=_worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len([r for r in results if r["enqueued"] == [50]]) == 1, results
    assert len(TaskQueue(tmp_db).list_tasks()) == 1


def test_many_concurrent_watchers_claim_exactly_once(tmp_db):
    """★Eight racing managers, one winner — the contention path, not the happy one."""
    n = 8
    barrier = threading.Barrier(n)
    wins = []

    def _worker(i):
        led = ClaimLedger(tmp_db)
        barrier.wait()
        if led.try_claim(REPO, 777, owner=f"w{i}"):
            wins.append(i)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(wins) == 1, wins


# ── 6. GitHub failure → backoff, no claim ─────────────────────────────


def test_github_transient_failure_makes_no_claim(queue, ledger):
    """⛔Fetch fails before any claim exists, so nothing can be half-claimed."""
    gh = FakeGitHub(fail_list_with=RuntimeError("gh: API rate limit"))

    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)

    assert result["enqueued"] == []
    assert result["error"] is not None
    assert "rate limit" in result["error"]
    assert gh.added == []
    assert queue.list_tasks() == []


def test_failure_then_recovery_still_enqueues_once(queue, ledger):
    run_cycle(queue=queue, ledger=ledger, repo=REPO,
              gh=FakeGitHub(fail_list_with=RuntimeError("boom")))

    healthy = FakeGitHub([make_issue(60, "Recovered", ["bug"])])
    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=healthy)

    assert result["enqueued"] == [60]
    assert len(queue.list_tasks()) == 1


def test_malformed_github_payload_is_an_error_not_a_crash(queue, ledger):
    """⛔A shape change in `gh` output must not take the manager down."""
    class Malformed(FakeGitHub):
        def list_issues(self, repo):
            return [{"title": "no number field"}, "not even a dict"]

    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=Malformed())

    assert result["error"] is not None
    assert result["enqueued"] == []
    assert queue.list_tasks() == []


def test_backoff_grows_and_is_capped():
    assert backoff_seconds(0, base=10) == 10
    assert backoff_seconds(1, base=10) == 20
    assert backoff_seconds(2, base=10) == 40
    assert backoff_seconds(99, base=10, cap=300) == 300
    assert backoff_seconds(0, base=10) > 0


# ── 7. enqueue failure releases the claim ─────────────────────────────


def test_enqueue_failure_releases_the_claim(queue, ledger):
    """⛔A claimed-but-not-queued issue must not be stuck 'in progress'."""
    gh = FakeGitHub([make_issue(70, "Enqueue will fail", ["bug"])])
    broken = MagicMock(wraps=queue)
    broken.enqueue.side_effect = sqlite3.OperationalError("database is locked")

    result = run_cycle(queue=broken, ledger=ledger, repo=REPO, gh=gh)

    assert result["enqueued"] == []
    assert result["released"] == [70]
    # Released on GitHub too — the label must not linger.
    assert (70, CLAIM_LABEL) in gh.removed
    assert ledger.get(REPO, 70)["state"] == "released"

    # And the next healthy cycle picks it back up.
    assert run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)["enqueued"] == [70]


def test_repeated_release_stops_re_claiming_forever(queue, ledger):
    """⛔Terminal failure must not become an infinite re-claim loop (req 5)."""
    gh = FakeGitHub([make_issue(80, "Always fails", ["bug"])])
    broken = MagicMock(wraps=queue)
    broken.enqueue.side_effect = sqlite3.OperationalError("nope")

    seen = [
        run_cycle(queue=broken, ledger=ledger, repo=REPO, gh=gh,
                  max_attempts=3)["released"]
        for _ in range(6)
    ]

    assert seen[:3] == [[80], [80], [80]]
    assert seen[3:] == [[], [], []], seen
    # Even a healthy queue leaves it alone once abandoned.
    assert run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh,
                     max_attempts=3)["enqueued"] == []


def test_claim_label_failure_releases_rather_than_claiming_invisibly(queue, ledger):
    """⛔A claim nobody can see on GitHub defeats the point of req 2."""
    class NoLabels(FakeGitHub):
        def add_label(self, repo, number, label):
            return False

    gh = NoLabels([make_issue(85, "Label write fails", ["bug"])])
    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)

    assert result["enqueued"] == []
    assert result["released"] == [85]
    assert queue.list_tasks() == []


# ── 8. priority policy ────────────────────────────────────────────────


def test_bugs_outrank_features():
    assert priority_for(["bug"]) < priority_for(["enhancement"])
    assert priority_for(["regression"]) < priority_for(["feature"])


def test_p0_outranks_plain_bug():
    assert priority_for(["p0"]) < priority_for(["bug"])
    assert priority_for(["critical"]) < priority_for(["bug"])


def test_explicit_priority_label_overrides_policy():
    """★An explicit label must win, even on a feature (req 3)."""
    assert priority_for(["enhancement", "priority:1"]) == 1
    assert priority_for(["p0", "priority:5"]) == 5


def test_unlabelled_issue_gets_default_priority():
    assert priority_for([]) == 3


def test_selection_is_deterministic_and_priority_ordered():
    issues = [
        {"number": 3, "title": "f", "labels": ["enhancement"], "parents": [], "phase": None},
        {"number": 1, "title": "p", "labels": ["p0"], "parents": [], "phase": None},
        {"number": 2, "title": "b", "labels": ["bug"], "parents": [], "phase": None},
    ]
    picked = select_candidates(issues, claimed=set(), active_issues=set())
    assert [i["number"] for i in picked] == [1, 2, 3]
    # Same set, same order, regardless of the order GitHub returned it in.
    assert picked == select_candidates(list(reversed(issues)), claimed=set(),
                                       active_issues=set())


def test_highest_priority_issue_is_claimed_first(queue, ledger):
    gh = FakeGitHub([
        make_issue(90, "Nice to have", ["enhancement"]),
        make_issue(91, "Production is down", ["p0"]),
    ])

    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh, max_claims=1)

    assert result["enqueued"] == [91]
    assert queue.list_tasks()[0].priority == priority_for(["p0"])


# ── 9. ledger bookkeeping ─────────────────────────────────────────────


def test_ledger_records_task_id_on_enqueue(queue, ledger):
    gh = FakeGitHub([make_issue(100, "Track me", ["bug"])])
    run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)

    row = ledger.get(REPO, 100)
    assert row["state"] == "enqueued"
    assert row["task_id"] == queue.list_tasks()[0].task_id


def test_ledger_is_scoped_per_repo(tmp_db):
    led = ClaimLedger(tmp_db)
    assert led.try_claim("org/a", 5, owner="x") is True
    assert led.try_claim("org/b", 5, owner="x") is True


def test_max_claims_bounds_one_cycle(queue, ledger):
    gh = FakeGitHub([make_issue(n, f"bug {n}", ["bug"]) for n in (110, 111, 112)])

    result = run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh, max_claims=2)

    assert len(result["enqueued"]) == 2
    assert len(queue.list_tasks()) == 2


# ── 10. operator escape hatch ─────────────────────────────────────────


def test_force_release_lets_a_parked_issue_be_retaken(queue, ledger):
    """A terminal failure parks the claim; a human can hand it back."""
    from agent_crew.protocol import TaskResult

    gh = FakeGitHub([make_issue(300, "Stuck", ["bug"])])
    run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)
    assert ledger.get(REPO, 300)["state"] == "enqueued"

    # The task dies terminally — that alone must NOT trigger a re-claim,
    # otherwise a permanently failing issue loops forever (req 5).
    task_id = queue.list_tasks()[0].task_id
    queue.submit_result(task_id, TaskResult(
        task_id=task_id, status="failed", summary="gave up"))
    gh.remove_label(REPO, 300, CLAIM_LABEL)
    assert run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)["enqueued"] == []

    assert ledger.force_release(REPO, 300) is True
    assert ledger.get(REPO, 300)["attempts"] == 0
    assert run_cycle(queue=queue, ledger=ledger, repo=REPO, gh=gh)["enqueued"] == [300]


def test_force_release_reports_unknown_issue(ledger):
    assert ledger.force_release(REPO, 999) is False


# ── 11. loop behaviour ────────────────────────────────────────────────


def test_watch_backs_off_between_failing_cycles(tmp_db):
    """Failing cycles must not hammer GitHub at the poll interval."""
    from agent_crew.watch import watch as watch_loop

    gh = FakeGitHub(fail_list_with=RuntimeError("502 bad gateway"))
    delays = []

    watch_loop(queue=TaskQueue(tmp_db), ledger=ClaimLedger(tmp_db), repo=REPO,
               gh=gh, interval=10, max_cycles=4, sleep_fn=delays.append)

    assert gh.list_calls == 4
    # No sleep after the final cycle, so 4 cycles record 3 delays.
    assert len(delays) == 3, delays
    assert delays[0] < delays[1] < delays[2], delays


def test_watch_uses_plain_interval_when_healthy(tmp_db):
    from agent_crew.watch import watch as watch_loop

    delays = []
    totals = watch_loop(queue=TaskQueue(tmp_db), ledger=ClaimLedger(tmp_db),
                        repo=REPO, gh=FakeGitHub([]), interval=45,
                        max_cycles=2, sleep_fn=delays.append)

    assert delays == [45]
    assert totals["errors"] == 0


def test_watch_loop_reports_every_cycle(tmp_db):
    """`on_cycle` fires for every cycle including the last (no silent tail)."""
    from agent_crew.watch import watch as watch_loop

    gh = FakeGitHub([make_issue(n, f"bug {n}", ["bug"]) for n in (900, 901)])
    seen = []

    watch_loop(queue=TaskQueue(tmp_db), ledger=ClaimLedger(tmp_db), repo=REPO,
               gh=gh, interval=10, max_cycles=2, max_claims=1,
               sleep_fn=lambda _s: None,
               on_cycle=lambda c, r, d: seen.append((c, list(r["enqueued"]))))

    assert seen == [(1, [900]), (2, [901])], seen


def test_interval_is_clamped_to_bounds():
    from agent_crew.watch import (
        MAX_INTERVAL_SECONDS,
        MIN_INTERVAL_SECONDS,
        clamp_interval,
    )

    assert clamp_interval(0) == MIN_INTERVAL_SECONDS
    assert clamp_interval(10**9) == MAX_INTERVAL_SECONDS
    assert clamp_interval(120) == 120


# ── 12. CLI wiring ────────────────────────────────────────────────────


def test_triage_cli_still_accepts_the_original_flags():
    """⛔The one-shot command must stay backward compatible (acceptance criterion)."""
    from click.testing import CliRunner

    from agent_crew.cli import crew

    help_text = CliRunner().invoke(crew, ["triage", "--help"]).output
    for flag in ("--repo", "--db", "--project", "--base", "--branch",
                 "--no-confirm", "--merge-history"):
        assert flag in help_text, flag
    assert "--watch" in help_text
    assert "--interval" in help_text


def test_triage_watch_runs_bounded_cycles_and_enqueues(tmp_db, monkeypatch):
    from click.testing import CliRunner

    from agent_crew import watch as watch_module
    from agent_crew.cli import crew

    gh = FakeGitHub([make_issue(200, "Watch me", ["bug"])])
    monkeypatch.setattr(watch_module, "GhCli", lambda *a, **k: gh)

    result = CliRunner().invoke(crew, [
        "triage", "--repo", REPO, "--db", tmp_db,
        "--watch", "--max-cycles", "1", "--interval", "10s",
    ])

    assert result.exit_code == 0, result.output
    assert "#200" in result.output
    tasks = TaskQueue(tmp_db).list_tasks()
    assert len(tasks) == 1
    assert tasks[0].context["issue"] == 200


def test_triage_watch_reports_no_actionable_issues(tmp_db, monkeypatch):
    from click.testing import CliRunner

    from agent_crew import watch as watch_module
    from agent_crew.cli import crew

    monkeypatch.setattr(watch_module, "GhCli", lambda *a, **k: FakeGitHub([]))

    result = CliRunner().invoke(crew, [
        "triage", "--repo", REPO, "--db", tmp_db, "--watch", "--max-cycles", "1",
    ])

    assert result.exit_code == 0, result.output
    assert "no actionable issues" in result.output
    assert TaskQueue(tmp_db).list_tasks() == []


def test_claims_cli_lists_and_releases(tmp_db, monkeypatch):
    from click.testing import CliRunner

    from agent_crew import watch as watch_module
    from agent_crew.cli import crew

    gh = FakeGitHub([make_issue(310, "Listed", ["bug"])])
    monkeypatch.setattr(watch_module, "GhCli", lambda *a, **k: gh)
    runner = CliRunner()
    runner.invoke(crew, ["triage", "--repo", REPO, "--db", tmp_db,
                         "--watch", "--max-cycles", "1"])

    listed = runner.invoke(crew, ["claims", "--db", tmp_db])
    assert listed.exit_code == 0, listed.output
    assert "310" in listed.output and "enqueued" in listed.output

    released = runner.invoke(crew, ["claims", "--db", tmp_db,
                                    "--repo", REPO, "--release", "310"])
    assert released.exit_code == 0, released.output
    assert "Released" in released.output
    assert ClaimLedger(tmp_db).get(REPO, 310)["state"] == "released"


def test_claims_cli_on_empty_ledger(tmp_db):
    from click.testing import CliRunner

    from agent_crew.cli import crew

    result = CliRunner().invoke(crew, ["claims", "--db", tmp_db])
    assert result.exit_code == 0
    assert "No claims." in result.output
