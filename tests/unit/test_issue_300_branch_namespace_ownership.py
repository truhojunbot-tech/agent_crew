"""#300 — a namespace prefix is not proof of ownership.

#280 stopped agent_crew force-moving refs it does not own, and #283 drew the
line at the *namespace*: `agent/`, `review/` and `test/` are ours, everything
else is somebody's work. That line is in the wrong place, and alpha_engine paid
for it on 2026-09-13.

Measured on this host (2026-09-13 11:11 KST, alpha_engine worktree
`~/.agent_crew/worktrees/alpha_engine/claude`):

    refs/heads/agent/claude-cli/4270-durable-id-backlog  8faef4d09
    refs/remotes/origin/agent/…/4270-durable-id-backlog  b53280095
    $ git merge-base --is-ancestor 8faef4d09 origin/main   → true
    $ git rev-list --count 8faef4d09..b53280095           → 18

The local ref sits on a commit from `main`'s history, 18 commits behind the
branch's own remote tip. That is #280's signature exactly — and it happened
*after* #283 shipped, because `agent/claude-cli/4270-durable-id-backlog` was
created by alpha_engine's own CLI tooling, not by agent_crew. It merely starts
with `agent/`. All 24 of alpha_engine's `agent/`-prefixed local branches are
somebody else's; none has the shape agent_crew actually generates
(`agent/<task_id[:12]}`, one segment). agent_crew's own `--help` even documents
`--branch agent/claude-cli/1665-feature`, so this was the advertised path.

⚠️The report guessed the trigger was the implementer role alternating between
  the claude and codex worktrees. It is not: nothing in the prep path branches
  on which provider holds the role, and one provider dispatched twice reaches
  the same `checkout -B`. Alternation only raised the dispatch count against a
  branch that was already unprotected. Fixing "alternation" would have fixed
  nothing — the same wrong-line-of-code trap #280 itself carried.

The rule this pins: agent_crew may force-move exactly the name it invents for
*this* task. A name it merely could have invented, for some other task, in a
namespace it also uses, belongs to whoever made it.

This is also a correctness fix, not only a safety one. A #244 fix-round task
dispatched at the implementer's own `agent/claude/<n>-<slug>` branch was being
reset to `origin/main` — so the round meant to fix the previous implementation
started by discarding it. That is alpha_engine's second failure verbatim:
"local PR branch ref points to origin/main while remote PR head and prior fix
are elsewhere".
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agent_crew.server import _agent_crew_owns_branch, _prepare_worktree_for_task

TASK_ID = "task-abc123"
#: What the implementer path invents when the task names no branch (#140).
GENERATED = f"agent/{TASK_ID[:12]}"
#: alpha_engine's, made by its own tooling, inside the same namespace.
FOREIGN = "agent/claude-cli/4270-durable-id-backlog"


def _git_calls(cmds):
    return [c for c in cmds if isinstance(c, list) and c[:2] == ["git", "-C"]]


def _run_prepare(task_branch, role, *, task_id=TASK_ID, rc=0):
    cmds = []

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        return MagicMock(returncode=rc, stderr="", stdout="")

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task("/wt/claude", task_id, task_branch, role)
    return _git_calls(cmds)


def _forced(git_calls):
    out = []
    for c in git_calls:
        if "checkout" in c and "-B" in c:
            out.append(c[c.index("-B") + 1])
    return out


def _detached_at(git_calls):
    out = []
    for c in git_calls:
        if "checkout" in c and "--detach" in c:
            out.append(c[-1])
    return out


# ── 1. ownership is per task, not per namespace ───────────────────────


def test_the_name_this_task_generates_is_ours():
    """⛔The control. #140's fresh-branch-per-task must survive: the implementer
    needs a real branch to commit and push, and detaching everything would
    'fix' the bug by breaking the role."""
    assert _agent_crew_owns_branch(GENERATED, TASK_ID) is True


@pytest.mark.parametrize("branch", [
    FOREIGN,
    "agent/claude-cli/5374-doc-llm-budget",   # live on alpha_engine today
    "agent/codex/4270-durable-id-backlog-slice",
    "agent/claude/280-fix",                   # what an agent is told to create
    "agent/diag-4585-bo",
    "agent/alpha_engine/claude",              # a sibling worktree's own branch
    "agent/feat-xyz",
])
def test_someone_elses_name_in_our_namespace_is_not_ours(branch):
    """★★The regression. Every one of these was force-moved to `origin/main`."""
    assert _agent_crew_owns_branch(branch, TASK_ID) is False


def test_another_tasks_generated_name_is_not_ours_either():
    """Ownership is proof, not resemblance. `agent/<12 hex>` shaped like ours but
    belonging to a different task is still a ref someone else may be committing
    to right now — that is the whole hazard of one shared refs namespace."""
    assert _agent_crew_owns_branch("agent/task-999zzz", TASK_ID) is False


def test_without_a_task_id_nothing_is_ours():
    """⛔Fail closed. If we cannot name the branch we would have generated, we
    cannot prove we generated this one."""
    assert _agent_crew_owns_branch(GENERATED, "") is False


@pytest.mark.parametrize("branch", [
    "main", "dev", "fix/foo", "review/ab12cd34", "test/ff00ff00",
    "agentic/x", "agents/x", "", "   ",
])
def test_everything_outside_stays_outside(branch):
    assert _agent_crew_owns_branch(branch, TASK_ID) is False


# ── 2. at the argv level ──────────────────────────────────────────────


def test_a_foreign_agent_branch_is_never_force_moved():
    """★★alpha_engine's incident, at the line that caused it."""
    git = _run_prepare(FOREIGN, "implementer")
    assert FOREIGN not in _forced(git), \
        f"force-moved a branch agent_crew did not create: {_forced(git)}"


def test_a_foreign_agent_branch_is_detached_at_its_own_remote_tip():
    """The task must still see the code it was dispatched for — the reviewers
    that burned their turn re-establishing branch state were looking at
    `origin/main`'s content under the branch's name."""
    git = _run_prepare(FOREIGN, "implementer")
    assert _detached_at(git)[:1] == [f"origin/{FOREIGN}"]


def test_the_generated_branch_still_gets_a_real_branch():
    assert _forced(_run_prepare("", "implementer")) == [GENERATED]


def test_a_task_may_name_its_own_generated_branch_explicitly():
    """Re-dispatching the same task id at the name it would have invented is
    still agent_crew moving its own ref."""
    assert _forced(_run_prepare(GENERATED, "implementer")) == [GENERATED]


def test_an_unpushed_local_branch_is_preferred_over_falling_back_to_main():
    """⛔Falling straight to `origin/main` when `origin/<branch>` misses is how a
    fix round silently starts from main. Try the local ref first — reading it
    moves nothing, and it is the only place unpushed work exists."""
    cmds = []

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        # only `origin/<FOREIGN>` is missing; the local ref resolves
        failed = "checkout" in cmd and cmd[-1] == f"origin/{FOREIGN}"
        return MagicMock(returncode=1 if failed else 0, stderr="no such ref", stdout="")

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task("/wt/claude", TASK_ID, FOREIGN, "implementer")

    tried = _detached_at(_git_calls(cmds))
    assert FOREIGN in tried, f"never tried the local ref; tried {tried}"
    assert tried.index(FOREIGN) < (tried.index("origin/main")
                                   if "origin/main" in tried else len(tried)), \
        "fell back to main before trying the branch's own local ref"


# ── 3. against a real repository ──────────────────────────────────────


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=60)


@pytest.fixture
def shared_clone(tmp_path):
    """origin + a clone + two sibling worktrees — alpha_engine's real topology,
    where claude and codex share one `refs/heads/*`."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], capture_output=True,
                   check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git("config", k, v, cwd=clone)
    (clone / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "first", cwd=clone)
    _git("push", "-u", "origin", "main", cwd=clone)

    # the branch alpha_engine's own tooling made, with real work on it
    _git("checkout", "-b", FOREIGN, cwd=clone)
    (clone / "a.txt").write_text("the implementer's work\n")
    _git("commit", "-am", "durable id backlog", cwd=clone)
    _git("push", "-u", "origin", FOREIGN, cwd=clone)
    _git("checkout", "main", cwd=clone)

    # main moves on, so a reset-to-main is unmistakable
    (clone / "b.txt").write_text("governance\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "governance: enforce admitted slice markers", cwd=clone)
    _git("push", "origin", "main", cwd=clone)

    claude = tmp_path / "wt-claude"
    codex = tmp_path / "wt-codex"
    _git("worktree", "add", "--detach", str(claude), "HEAD", cwd=clone)
    _git("worktree", "add", "--detach", str(codex), "HEAD", cwd=clone)
    return clone, claude, codex


def _sha(repo, ref):
    return _git("rev-parse", ref, cwd=repo).stdout.strip()


def test_the_foreign_ref_does_not_move(shared_clone):
    """★★The incident reproduced end to end with real git."""
    clone, claude, _ = shared_clone
    before = _sha(clone, FOREIGN)
    main_tip = _sha(clone, "main")
    assert before != main_tip, "fixture is degenerate"

    _prepare_worktree_for_task(str(claude), TASK_ID, FOREIGN, "implementer")

    after = _sha(clone, FOREIGN)
    where = "to main's tip — alpha_engine's symptom" if after == main_tip else "elsewhere"
    assert after == before, f"the ref moved {where}"


def test_alternating_the_role_between_two_worktrees_moves_nothing(shared_clone):
    """★★#300's actual question. claude implements, then codex implements the
    same branch: two sibling worktrees, one shared refs namespace.

    Both must land on the branch's content and neither may move its ref — and
    the second dispatch must not inherit the first worktree's state either,
    which is what produced 'unborn HEAD' and stale-premise reports."""
    clone, claude, codex = shared_clone
    before = _sha(clone, FOREIGN)

    for wt, task in ((claude, "impl-82da4089"), (codex, "impl-d3a27e7e")):
        _prepare_worktree_for_task(str(wt), task, FOREIGN, "implementer")
        assert _sha(wt, "HEAD") == _sha(clone, f"origin/{FOREIGN}"), \
            f"{wt.name} did not land on the branch it was dispatched for"
        assert _git("symbolic-ref", "-q", "HEAD", cwd=wt).returncode != 0, \
            f"{wt.name} owns the shared branch, so its next reset moves it"

    assert _sha(clone, FOREIGN) == before, "the ref moved during role alternation"


def test_the_worktree_sees_the_implementers_work_not_mains(shared_clone):
    """The reviewers' complaint, stated as content: they were reading main."""
    clone, claude, _ = shared_clone
    _prepare_worktree_for_task(str(claude), TASK_ID, FOREIGN, "implementer")
    assert (claude / "a.txt").read_text() == "the implementer's work\n"
    assert not (claude / "b.txt").exists(), "worktree is on main's history"


def test_the_gate_uses_the_dispatched_tasks_own_id():
    """⛔A gate that consults some other task's id is not a gate.

    Every other test here shares one task id, so hardcoding an id at the call
    site passes them all while leaving the real dispatch deciding ownership
    from the wrong task — which in a shared refs namespace means force-moving
    whatever branch that other task happened to generate.
    """
    other = "impl-deadbeef"
    other_generated = f"agent/{other[:12]}"
    assert other_generated != GENERATED, "fixture is degenerate"

    assert _forced(_run_prepare(other_generated, "implementer", task_id=other)) \
        == [other_generated], "a task lost its own working branch"
    assert _forced(_run_prepare(GENERATED, "implementer", task_id=other)) == [], \
        f"force-moved {GENERATED}, which belongs to task {TASK_ID}, not {other}"
