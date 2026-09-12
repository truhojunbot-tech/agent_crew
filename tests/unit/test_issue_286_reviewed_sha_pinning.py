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


# ── 1b. an existing pin outranks the current ref ──────────────────────
#
# Review of PR #287, P1. Pinning the *dispatch-time* resolution was not enough:
# prep is run more than once for a task — `_try_push_next` prepares and records
# `reviewed_sha`, and `_dispatch_task` prepares again — and the second run
# re-resolved the PR ref and overwrote the pin. A review queued at A and
# dispatched after the head moved to B silently reviewed and recorded B.
#
# ⛔The property is idempotence: preparing the same task twice must land on the
#   same commit both times, whatever the remote did in between.


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_an_existing_pin_survives_the_head_moving(pr_repo, role):
    """★★The reviewer's scenario: queued at A, remote advances to B, prepared."""
    clone, wt, sha_a, advance = pr_repo
    sha_b = advance()
    assert sha_b != sha_a

    reviewed = _prepare_worktree_for_task(
        str(wt), "task-287abc", PR_BRANCH, role,
        task_context={"pr_number": None, "reviewed_sha": sha_a},
    )
    assert reviewed == sha_a, "prep followed the branch instead of the pin"
    assert _sha(wt) == sha_a
    assert (wt / "a.txt").read_text() == "commit A\n"


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_preparing_twice_lands_on_the_same_commit(pr_repo, role):
    """★★Idempotence, which is the property the double-prep needs.

    The first run records the pin; the second must honour it. This is the exact
    shape of `_try_push_next` preparing and then `_dispatch_task` preparing
    again after the PR moved."""
    clone, wt, sha_a, advance = pr_repo
    first = _prepare_worktree_for_task(str(wt), "task-287abc", PR_BRANCH, role)
    assert first == sha_a

    advance()
    second = _prepare_worktree_for_task(
        str(wt), "task-287abc", PR_BRANCH, role,
        task_context={"reviewed_sha": first},      # what patch_context stored
    )
    assert second == first, "the second prep re-resolved and moved the task"
    assert _sha(wt) == sha_a


def test_the_retained_context_still_names_A(pr_repo):
    """The identity the #253 stale-review gate reads must not be rewritten to B
    by the very act of preparing."""
    clone, wt, sha_a, advance = pr_repo
    advance()
    context = {"reviewed_sha": sha_a}
    reviewed = _prepare_worktree_for_task(str(wt), "task-287abc", PR_BRANCH,
                                          "reviewer", task_context=context)
    # what the caller would then patch back onto the task
    assert {**context, "reviewed_sha": reviewed}["reviewed_sha"] == sha_a


def test_an_unresolvable_pin_falls_back_rather_than_failing(pr_repo):
    """⛔A pin can name a commit this repo does not have — a force-push, or a
    SHA copied from elsewhere. Refusing to prepare would strand the task, so
    fall back to the PR ref and let the caller record what was really used."""
    clone, wt, sha_a, _ = pr_repo
    reviewed = _prepare_worktree_for_task(
        str(wt), "task-287abc", PR_BRANCH, "reviewer",
        task_context={"reviewed_sha": "0" * 40},
    )
    assert reviewed == sha_a


# Revision expressions git would happily resolve, none of which is an object id.
# ⛔`origin/main` is the one that matters: task context is unrestricted, so a
#   pin of `origin/main` would resolve, short-circuit the PR-head lookup, and
#   silently prepare AND attribute a PR review to main (review of PR #287).
RESOLVABLE_NON_SHAS = ["origin/main", "main", "HEAD", "HEAD~1", "@", "origin/HEAD"]


@pytest.mark.parametrize("expression", RESOLVABLE_NON_SHAS)
def test_a_pin_must_be_an_object_id_not_a_revision(pr_repo, expression):
    """★★`rev-parse --verify <x>^{commit}` resolves any revision expression, so
    probing alone is not validation. A pin has to LOOK like a commit id before
    it is worth probing."""
    clone, wt, sha_a, _ = pr_repo
    reviewed = _prepare_worktree_for_task(
        str(wt), "task-287abc", PR_BRANCH, "reviewer",
        task_context={"reviewed_sha": expression},
    )
    assert reviewed == sha_a, f"{expression!r} was honoured as a pin"


def test_the_origin_main_case_end_to_end(pr_repo):
    """⛔The concrete harm, spelled out: a review prepared and attributed to
    main instead of the PR. Asserted on content as well as SHA, because the
    point is that the wrong code would have been reviewed."""
    clone, wt, sha_a, _ = pr_repo
    main_tip = _sha(clone, "origin/main")
    assert main_tip != sha_a

    reviewed = _prepare_worktree_for_task(
        str(wt), "task-287abc", PR_BRANCH, "reviewer",
        task_context={"pr_number": None, "reviewed_sha": "origin/main"},
    )
    assert reviewed != main_tip and reviewed == sha_a
    assert (wt / "a.txt").read_text() == "commit A\n"


def test_an_abbreviated_sha_is_not_a_pin(pr_repo):
    """⛔Abbreviations are ambiguous by construction and no real pin is one:
    `reviewed_sha` is written from `rev-parse HEAD`, which is always full.

    The remote is advanced FIRST so the two outcomes differ — an accepted
    abbreviation lands on A, a rejected one falls through to the PR ref at B.
    Without that the assertion holds either way and pins nothing; mutation
    caught exactly that in the first version of this test."""
    clone, wt, sha_a, advance = pr_repo
    sha_b = advance()
    reviewed = _prepare_worktree_for_task(
        str(wt), "task-287abc", PR_BRANCH, "reviewer",
        task_context={"reviewed_sha": sha_a[:12]},
    )
    assert reviewed == sha_b, "an abbreviation was honoured as a pin"


def test_a_full_sha_is_still_honoured_in_any_case(pr_repo):
    """⛔The control. Tightening the shape must not reject real pins — git
    accepts uppercase hex, so the check has to as well."""
    clone, wt, sha_a, advance = pr_repo
    advance()
    for form in (sha_a, sha_a.upper(), f"  {sha_a}  "):
        assert _prepare_worktree_for_task(
            str(wt), "task-287abc", PR_BRANCH, "reviewer",
            task_context={"reviewed_sha": form},
        ) == sha_a, f"a real pin was rejected in the form {form!r}"


@pytest.mark.parametrize("junk", ["", None, "not-a-sha", 12345, True])
def test_a_junk_pin_is_ignored(pr_repo, junk):
    clone, wt, sha_a, _ = pr_repo
    reviewed = _prepare_worktree_for_task(
        str(wt), "task-287abc", PR_BRANCH, "reviewer",
        task_context={"reviewed_sha": junk},
    )
    assert reviewed == sha_a


def test_the_implementer_is_not_pinned(pr_repo):
    """⛔Scope. A fix task carries the `reviewed_sha` of the review it answers;
    pinning the implementer to it would check out the code being fixed instead
    of the branch to fix it on."""
    clone, wt, sha_a, advance = pr_repo
    advance()
    _prepare_worktree_for_task(str(wt), "task-287abc", PR_BRANCH, "implementer",
                               task_context={"reviewed_sha": sha_a})
    # Asserted as "not the pin" rather than as an exact SHA: what the
    # implementer path starts from is #140's business and differs by branch
    # (#280 is still in flight), but it must never be the review's commit.
    assert _sha(wt) != sha_a, "the implementer was pinned to the review's commit"


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


def test_a_pinned_task_does_not_ask_github_for_the_pr_head(pr_repo, monkeypatch):
    """⛔The PR-head lookup is the only network call in prep, and when the task
    is pinned its answer is discarded. Worse, a failure logs "THIS MAY NOT BE
    THE PR'S CODE; treat any finding from this task as suspect" about a task
    about to be prepared at exactly the commit it names — a misleading error is
    not free."""
    clone, wt, sha_a, advance = pr_repo
    advance()
    monkeypatch.setattr(
        "agent_crew.server._resolve_pr_head_branch",
        lambda *a, **k: pytest.fail("asked GitHub for a PR head while pinned"))

    reviewed = _prepare_worktree_for_task(
        str(wt), "task-287abc", PR_BRANCH, "reviewer",
        task_context={"pr_number": 287, "reviewed_sha": sha_a})
    assert reviewed == sha_a


def test_an_unpinned_task_still_resolves_the_pr_head(pr_repo, monkeypatch):
    """⛔The control: #186's PR-head resolution must keep working for every task
    that has no pin yet, which is every task's first preparation."""
    clone, wt, sha_a, _ = pr_repo
    asked = []
    monkeypatch.setattr("agent_crew.server._resolve_pr_head_branch",
                        lambda pr, **k: asked.append(pr) or PR_BRANCH)

    _prepare_worktree_for_task(str(wt), "task-287abc", "main", "reviewer",
                               task_context={"pr_number": 287})
    assert asked == [287]
    assert _sha(wt) == sha_a
