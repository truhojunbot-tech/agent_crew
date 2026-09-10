"""#286 — the tester must test the commit it was attributed to.

#282 shipped guidance telling the tester to always:

    git fetch origin <pr-branch-name>
    git checkout --detach FETCH_HEAD

The dispatcher prepares the worktree and records that state as
`context.reviewed_sha` BEFORE the agent prompt runs (#253). So a moving PR head
opens a clean TOCTOU:

    prep at A, reviewed_sha=A → remote advances to B → agent re-fetches → tests B
    → result, cost and every downstream guard still say A

Closed from both ends, because either alone leaves a hole:

  * the dispatcher resolves the ref to an exact commit and detaches at THAT, so
    the prepared state is an object rather than a moving name;
  * the prompts tell reviewer and tester to verify `HEAD == reviewed_sha` and
    fail closed, instead of re-resolving the branch themselves.

⛔The prompt half is advisory — an agent can ignore it. The dispatcher half is
  not, which is why the pinning is the primary fix and the wording is the
  backstop, not the other way round.
"""

import subprocess

import pytest
from unittest.mock import MagicMock, patch

from agent_crew import instructions
from agent_crew.server import _prepare_worktree_for_task

PR_BRANCH = "fix/moving-head-286"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=60)


def _sha(repo, ref="HEAD"):
    return _git("rev-parse", ref, cwd=repo).stdout.strip()


@pytest.fixture
def pr_repo(tmp_path):
    """origin + clone + a worktree, with a PR branch that can be advanced."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], capture_output=True,
                   check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git("config", k, v, cwd=clone)
    (clone / "a.txt").write_text("base\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "base", cwd=clone)
    _git("push", "-u", "origin", "main", cwd=clone)

    _git("checkout", "-b", PR_BRANCH, cwd=clone)
    (clone / "a.txt").write_text("commit A\n")
    _git("commit", "-am", "A", cwd=clone)
    _git("push", "-u", "origin", PR_BRANCH, cwd=clone)
    sha_a = _sha(clone)
    _git("checkout", "main", cwd=clone)

    wt = tmp_path / "wt"
    _git("worktree", "add", "--detach", str(wt), "HEAD", cwd=clone)

    def advance():
        """Push commit B onto the PR branch, as a fixup would."""
        _git("checkout", PR_BRANCH, cwd=clone)
        (clone / "a.txt").write_text("commit B\n")
        _git("commit", "-am", "B", cwd=clone)
        _git("push", "origin", PR_BRANCH, cwd=clone)
        b = _sha(clone)
        _git("checkout", "main", cwd=clone)
        return b

    return clone, wt, sha_a, advance


# ── 1. the race, with real git ────────────────────────────────────────


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_the_prepared_head_does_not_follow_a_moving_pr(pr_repo, role):
    """★★The race from the issue, executed.

    Prep at A, remote advances to B, and the worktree must still be A — the
    commit `reviewed_sha` names."""
    clone, wt, sha_a, advance = pr_repo
    reviewed = _prepare_worktree_for_task(str(wt), "task-286abc", PR_BRANCH, role)
    assert reviewed == sha_a, "prep did not land on the PR head"

    sha_b = advance()
    assert sha_b != sha_a

    assert _sha(wt) == sha_a, "the worktree followed the branch instead of the commit"
    assert (wt / "a.txt").read_text() == "commit A\n"


def test_the_old_guidance_would_have_moved_it(pr_repo):
    """⛔The counterfactual, so the fix is not being asserted against nothing.

    This runs exactly what #282 told the tester to run. It lands on B while
    `reviewed_sha` still says A — which is the bug, reproduced."""
    clone, wt, sha_a, advance = pr_repo
    reviewed = _prepare_worktree_for_task(str(wt), "task-286abc", PR_BRANCH, "tester")
    sha_b = advance()

    _git("fetch", "origin", PR_BRANCH, cwd=wt)
    _git("checkout", "--detach", "FETCH_HEAD", cwd=wt)

    assert _sha(wt) == sha_b != reviewed, \
        "the counterfactual did not reproduce — this test is not proving anything"


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_the_prepared_worktree_is_detached(pr_repo, role):
    """⛔Complements #280: reviewer and tester never commit, so a branch buys
    them nothing and costs the immutability. This is also the last
    reviewer/tester write to the shared `refs/heads/*` namespace, removed."""
    clone, wt, sha_a, _ = pr_repo
    _prepare_worktree_for_task(str(wt), "task-286abc", PR_BRANCH, role)
    assert _git("symbolic-ref", "-q", "HEAD", cwd=wt).returncode != 0


def test_no_new_branch_ref_is_created(pr_repo):
    """The old prep created `review/<id>` / `test/<id>` in the shared namespace."""
    clone, wt, _, _ = pr_repo
    before = set(_git("for-each-ref", "--format=%(refname)", "refs/heads",
                      cwd=clone).stdout.split())
    _prepare_worktree_for_task(str(wt), "task-286abc", PR_BRANCH, "tester")
    after = set(_git("for-each-ref", "--format=%(refname)", "refs/heads",
                     cwd=clone).stdout.split())
    assert after == before, f"prep created refs: {sorted(after - before)}"


def test_reviewed_sha_is_what_the_worktree_actually_holds(pr_repo):
    """#253's contract: the returned SHA IS the prepared state, not a name that
    might resolve elsewhere later."""
    clone, wt, sha_a, advance = pr_repo
    reviewed = _prepare_worktree_for_task(str(wt), "task-286abc", PR_BRANCH, "tester")
    advance()
    assert reviewed == _sha(wt) == sha_a


def test_an_unresolvable_ref_falls_back_to_main(pr_repo):
    clone, wt, _, _ = pr_repo
    reviewed = _prepare_worktree_for_task(str(wt), "task-286abc", "no/such/branch",
                                          "tester")
    assert reviewed == _sha(clone, "origin/main")


# ── 2. the argv shape ─────────────────────────────────────────────────


def _prep_calls(role, branch=PR_BRANCH, rc=0, sha="a" * 40):
    cmds = []

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        out = sha if "rev-parse" in cmd else ""
        return MagicMock(returncode=rc, stdout=out, stderr="")

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task("/wt/gemini", "task-286abc", branch, role)
    return [c for c in cmds if isinstance(c, list) and c[:2] == ["git", "-C"]]


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_prep_resolves_before_it_checks_out(role):
    """The order is the guarantee: name → object → checkout. Checking out the
    name and reading the SHA afterwards leaves a window between the two."""
    calls = _prep_calls(role)
    verbs = [c[3] for c in calls if len(c) > 3]
    assert "rev-parse" in verbs and "checkout" in verbs
    assert verbs.index("rev-parse") < verbs.index("checkout")


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_prep_checks_out_the_resolved_sha_not_the_ref(role):
    checkout = [c for c in _prep_calls(role) if "checkout" in c]
    assert checkout and "--detach" in checkout[0]
    assert checkout[0][-1] == "a" * 40, f"checked out {checkout[0][-1]!r}, not the SHA"


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_prep_never_force_moves_a_branch(role):
    """#280's invariant, still true for these roles."""
    assert not [c for c in _prep_calls(role) if "checkout" in c and "-B" in c]


# ── 3. what the agents are told ───────────────────────────────────────


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_the_prompt_no_longer_tells_them_to_re_fetch(role):
    """★★#282's exact recipe, gone. It is quoted in the new text as the
    anti-pattern, so match the imperative form rather than the substring."""
    text = instructions.generate(role, "demo", 8105, delivery="dispatcher")
    assert "```bash\ngit fetch origin <pr-branch-name>\ngit checkout --detach FETCH_HEAD\n```" \
        not in text


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_the_prompt_requires_verifying_against_reviewed_sha(role):
    text = instructions.generate(role, "demo", 8105, delivery="dispatcher")
    assert "git rev-parse HEAD" in text
    assert "reviewed_sha" in text
    assert "needs_human" in text


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_the_prompt_still_forbids_shared_ref_writes(role):
    """⛔#280/#283 and #286 are complementary; neither fix may quietly drop the
    other's rule while rewriting the same paragraph."""
    text = instructions.generate(role, "demo", 8105, delivery="dispatcher")
    assert "branch -f" in text and "update-ref" in text
