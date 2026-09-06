"""#263 — the test suite was writing to a real GitHub PR.

`tests/unit/test_issue_250_terminal_pr_gate.py` used `PR = 241` — a real,
merged PR — and drove the real result handler, which posts a review comment for
any review result carrying a `pr_number`. Nothing in those tests patched that
path, so every full-suite run posted three comments to PR #241.

Measured 2026-09-04: **228 of PR #241's 263 comments were fixture data**, 76
from each of three test task ids (`review-late`, `review-open`,
`review-outage`), spanning 2026-09-02 → 2026-09-04. Two older tests were doing
the same against PR #42.

The lesson is not "those tests were careless". Every one of them passed, for
weeks, while writing to production — because the dispatcher wraps GitHub calls
in `except Exception: logger.exception(...)`, so nothing a test could observe
changed whether the write happened. A boundary that depends on each test
remembering is not a boundary.
"""

import pytest

from tests.conftest import GITHUB_WRITE_FUNCTIONS, GitHubWriteFromTest


def test_every_mutating_github_function_is_covered():
    """⛔The block is a list, so the list is the contract. A new write function
    added to `agent_crew.github` without being listed here is unguarded."""
    import inspect

    import agent_crew.github as gh

    mutating = {
        name for name, fn in inspect.getmembers(gh, inspect.isfunction)
        if fn.__module__ == gh.__name__
        and any(verb in name for verb in ("post", "create", "merge", "add", "remove",
                                          "close", "delete", "update", "edit"))
    }

    missing = mutating - set(GITHUB_WRITE_FUNCTIONS)
    assert not missing, (
        f"these agent_crew.github functions mutate GitHub but are not blocked "
        f"in the test suite: {sorted(missing)}"
    )


@pytest.mark.parametrize("fn_name", GITHUB_WRITE_FUNCTIONS)
def test_calling_a_write_function_from_a_test_raises(fn_name, github_writes_recorder):
    """★The guard itself: each blocked function refuses and names itself.

    Requests the recorder so it can clear the attempt it caused on purpose —
    otherwise the teardown would correctly fail this test for doing the very
    thing it is asserting.
    """
    import agent_crew.github as gh

    fn = getattr(gh, fn_name, None)
    if fn is None:
        pytest.skip(f"{fn_name} not present")

    with pytest.raises(GitHubWriteFromTest, match=fn_name):
        fn(1, "body")

    assert github_writes_recorder and github_writes_recorder[-1]["fn"] == fn_name
    github_writes_recorder.clear()


def test_a_swallowed_write_attempt_still_fails_the_test(testdir_factory=None):
    """★★Raising is not enough, and this is the part that matters.

    The dispatcher catches every exception around its GitHub calls, so a
    blocked write is invisible to the test that caused it — exactly how the
    #241 writes survived dozens of green runs. The fixture therefore records
    attempts and fails at teardown.

    Asserted by running a throwaway test in-process rather than by reading the
    fixture's source: what matters is the observable outcome for a test that
    swallows the error.
    """
    import subprocess
    import sys
    import textwrap

    body = textwrap.dedent('''
        def test_swallows_the_write():
            import agent_crew.github as gh
            try:
                gh.post_pr_comment(999241, "synthetic")
            except Exception:
                pass          # exactly what the dispatcher does
    ''')
    import pathlib
    tmp = pathlib.Path("tests/unit/test_tmp_swallow_probe.py")
    tmp.write_text(body)
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", str(tmp), "-q"],
                           capture_output=True, text=True, timeout=180)
        assert r.returncode != 0, (
            "a test that swallowed a blocked GitHub write still passed — the "
            "attempt has to fail the test, not just be blocked"
        )
        assert "post_pr_comment" in (r.stdout + r.stderr)
    finally:
        tmp.unlink(missing_ok=True)


def test_a_test_may_opt_in_to_observing_writes(github_writes):
    """The escape for tests whose subject IS the side effect: recorded, not
    raised, and never on the network."""
    import agent_crew.github as gh

    assert gh.post_pr_comment(999241, "observed") is True

    assert len(github_writes) == 1
    assert github_writes[0]["fn"] == "post_pr_comment"
    assert github_writes[0]["args"][0] == 999241


def test_the_fixture_pr_number_is_not_a_real_pr():
    """⛔The second brace: even with the block in place, a fixture must not
    name a live object. A future test that patches around the guard would
    otherwise still target PR #241."""
    from tests.unit import test_issue_250_terminal_pr_gate as t250

    assert t250.PR > 100000, (
        "the terminal-PR fixtures name a plausible real PR again; use a number "
        "that cannot exist in this repo"
    )


# ── the live_github marker is a request, not permission (review of PR #264) ──
#
# The first version yielded immediately for any test carrying the marker: no
# stubs, no env gate, no target check. A normal `pytest` run would therefore
# write to the production repo as soon as one test opted itself out — the same
# hole, self-service.


from tests.conftest import (  # noqa: E402
    DISPOSABLE_REPO_PATTERN,
    LIVE_GITHUB_ENV,
    LIVE_GITHUB_REPO_ENV,
    live_github_approval,
)

PROD = "truhojunbot-tech/agent_crew"


@pytest.mark.parametrize("env,expected_reason", [
    ({}, "not set"),
    ({LIVE_GITHUB_ENV: "1"}, "LIVE_GITHUB_REPO"),
    ({LIVE_GITHUB_ENV: "0", LIVE_GITHUB_REPO_ENV: "org/sandbox"}, "not set"),
    ({LIVE_GITHUB_ENV: "1", LIVE_GITHUB_REPO_ENV: PROD}, "own repository"),
    ({LIVE_GITHUB_ENV: "1", LIVE_GITHUB_REPO_ENV: "org/prod"}, "does not look disposable"),
])
def test_live_writes_are_refused_without_explicit_approval(env, expected_reason):
    """★Absence is refusal. Each condition is separately required."""
    approved, why = live_github_approval(env=env, production_repo=PROD)

    assert approved is False
    assert expected_reason in why


def test_a_named_disposable_target_is_approved():
    """⛔The gate has to be passable, or the escape hatch is a lie and someone
    will delete it rather than use it."""
    approved, why = live_github_approval(
        env={LIVE_GITHUB_ENV: "1", LIVE_GITHUB_REPO_ENV: "org/crew-sandbox"},
        production_repo=PROD)

    assert approved is True and "crew-sandbox" in why


def test_the_production_repo_never_matches_the_disposable_pattern():
    """⛔The name check is mechanical, so it is worth asserting it actually
    excludes the repository this suite runs against."""
    assert not DISPOSABLE_REPO_PATTERN.search(PROD)


def test_an_unapproved_live_github_test_is_skipped_and_still_blocked(tmp_path):
    """★★End to end: a marked test on a normal run must neither write nor pass
    quietly. Run in-process as a throwaway file, because what matters is what
    pytest actually does with the marker — not what the fixture source says.
    """
    import pathlib
    import subprocess
    import sys
    import textwrap

    probe = pathlib.Path("tests/unit/test_tmp_live_probe.py")
    probe.write_text(textwrap.dedent('''
        import pytest

        @pytest.mark.live_github
        def test_wants_to_write_for_real():
            import agent_crew.github as gh
            gh.post_pr_comment(999241, "this must never reach GitHub")
    '''))
    env = {k: v for k, v in __import__("os").environ.items()
           if k not in (LIVE_GITHUB_ENV, LIVE_GITHUB_REPO_ENV)}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", str(probe), "-q", "-rs"],
                           capture_output=True, text=True, timeout=180, env=env)
        assert r.returncode == 0, r.stdout + r.stderr      # skipped, not failed
        combined = r.stdout + r.stderr
        assert "skipped" in combined
        assert "not approved" in combined, (
            "an unapproved live_github test ran instead of being skipped"
        )
    finally:
        probe.unlink(missing_ok=True)


def test_the_unapproved_stub_refuses_and_explains():
    """The belt behind the brace.

    An unapproved `live_github` test is SKIPPED, which is the control that
    actually runs — the test above proves it. The fixture additionally installs
    a refusing stub for that case, which no skipped test can reach, so it is
    covered here directly rather than left as an untested line. It exists for
    the day a plugin or refactor makes a skipped test's body run anyway.
    """
    from tests.conftest import _blocked_live

    with pytest.raises(GitHubWriteFromTest, match="UNAPPROVED"):
        _blocked_live("post_pr_comment", "AGENT_CREW_ALLOW_LIVE_GITHUB is not set")(1, "x")


def test_the_unapproved_stub_refuses_and_explains():
    """The belt behind the brace.

    An unapproved `live_github` test is SKIPPED, which is the control that
    actually runs — the test above proves it. The fixture additionally installs
    a refusing stub for that case, which no skipped test can reach, so it is
    covered here directly rather than left as an untested line. It exists for
    the day a plugin or refactor makes a skipped test's body run anyway.
    """
    from tests.conftest import _blocked_live

    with pytest.raises(GitHubWriteFromTest, match="UNAPPROVED"):
        _blocked_live("post_pr_comment", "AGENT_CREW_ALLOW_LIVE_GITHUB is not set")(1, "x")


# ── approval names a target; it does not hand over the writes ─────────
#
# The gate validated the env var and then let the approved test run with the
# write functions unwrapped. They all take `repo=None` and fall back to
# `get_repo()`, which resolves to the real checkout's origin — so an approved
# test calling `post_pr_comment(241, "x")` wrote to PRODUCTION, from inside the
# exception granted to avoid exactly that (review of PR #264).

APPROVED_TARGET = "truhojunbot-tech/crew-sandbox"


def _flat(text):
    """Collapse whitespace before matching.

    pytest wraps long assertion output, so a message can be split mid-sentence
    across lines — a substring check against the raw capture fails for a
    message that is plainly there.
    """
    import re as _re

    return _re.sub(r"\s+", " ", text)


def _run_probe(tmp_path, body, *, approved=True, target=APPROVED_TARGET):
    """Run a `live_github`-marked test in a subprocess and return the result."""
    import os as _os
    import pathlib
    import subprocess
    import sys
    import textwrap

    probe = pathlib.Path("tests/unit/test_tmp_approved_probe.py")
    probe.write_text(textwrap.dedent(body))
    env = dict(_os.environ)
    if approved:
        env[LIVE_GITHUB_ENV] = "1"
        env[LIVE_GITHUB_REPO_ENV] = target
    else:
        env.pop(LIVE_GITHUB_ENV, None)
        env.pop(LIVE_GITHUB_REPO_ENV, None)
    try:
        return subprocess.run([sys.executable, "-m", "pytest", str(probe), "-q"],
                              capture_output=True, text=True, timeout=180, env=env)
    finally:
        probe.unlink(missing_ok=True)


def test_an_approved_test_cannot_write_with_the_default_repo(tmp_path):
    """★★The reported hole, end to end: approval granted, no `repo` passed.

    Without a repo the helper resolves the checkout's origin — production — so
    this must fail rather than reach the network.
    """
    r = _run_probe(tmp_path, '''
        import pytest

        @pytest.mark.live_github
        def test_writes_with_the_default_repo():
            import agent_crew.github as gh
            gh.post_pr_comment(999241, "this must not reach production")
    ''')

    assert r.returncode != 0, "an approved test wrote with the default repo"
    assert "without an explicit `repo`" in (r.stdout + r.stderr)


def test_an_approved_test_cannot_write_to_another_repo(tmp_path):
    """Approval is for ONE target. Naming a different one is still a write to
    somewhere nobody approved."""
    r = _run_probe(tmp_path, '''
        import pytest

        @pytest.mark.live_github
        def test_writes_to_production_explicitly():
            import agent_crew.github as gh
            gh.post_pr_comment(999241, "nope", repo="truhojunbot-tech/agent_crew")
    ''')

    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "approved disposable target" in combined


def test_an_approved_test_may_write_to_its_own_target(tmp_path):
    """⛔The gate has to be passable at the boundary too, or the approval is
    decorative and the next person deletes the whole mechanism."""
    r = _run_probe(tmp_path, '''
        import pytest

        @pytest.mark.live_github
        def test_writes_to_the_sandbox(monkeypatch):
            import agent_crew.github as gh
            seen = {}
            # Stand in for the network at the LAST hop, so the guard is what is
            # being exercised and nothing actually leaves the machine.
            monkeypatch.setattr(gh, "check_gh_installed", lambda: True)

            def fake_run(argv, **kw):
                seen["argv"] = argv
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            monkeypatch.setattr(gh.subprocess, "run", fake_run)
            assert gh.post_pr_comment(1, "hello", repo="truhojunbot-tech/crew-sandbox") is True
            assert "truhojunbot-tech/crew-sandbox" in seen["argv"]
    ''')

    assert r.returncode == 0, r.stdout + r.stderr


def test_the_pin_reads_the_repo_from_the_call_not_a_fixed_position():
    """The helpers put `repo` in different places; the guard binds by signature
    so a new write function is covered without being special-cased."""
    from tests.conftest import _pinned_to_repo

    calls = []

    def fake_create_issue(title, body, repo=None):
        calls.append(repo)
        return "url"

    guarded = _pinned_to_repo("create_issue", fake_create_issue, APPROVED_TARGET)

    assert guarded("t", "b", repo=APPROVED_TARGET) == "url"
    assert calls == [APPROVED_TARGET]
    with pytest.raises(GitHubWriteFromTest, match="without an explicit"):
        guarded("t", "b")
    with pytest.raises(GitHubWriteFromTest, match="approved disposable target"):
        guarded("t", "b", repo="someone/else")


def test_a_write_function_without_a_repo_parameter_is_refused():
    """⛔If we cannot see where it writes, we cannot approve it."""
    from tests.conftest import _pinned_to_repo

    def no_repo_param(pr_number, body):
        return True

    guarded = _pinned_to_repo("no_repo_param", no_repo_param, APPROVED_TARGET)

    with pytest.raises(GitHubWriteFromTest, match="no `repo` parameter"):
        guarded(1, "x")


# ── the alias hole: patching by name is patched too late ─────────────
#
# `monkeypatch.setattr(agent_crew.github, "post_pr_comment", ...)` does nothing
# to a name a test module already bound at COLLECTION time with
# `from agent_crew.github import post_pr_comment`. That alias points at the
# original function and the fixture never sees the call (review of PR #264).
#
# Every helper reaches `subprocess.run(["gh", ...])`, and `subprocess.run` is
# resolved through the module at CALL time — so the guard sits there, below
# every alias.

from tests.conftest import _gh_argv_repo, _gh_write_argv  # noqa: E402


@pytest.mark.parametrize("argv,is_write", [
    (["gh", "pr", "view", "1", "--json", "comments"], False),
    (["gh", "pr", "list", "--repo", "o/r"], False),
    (["gh", "issue", "view", "1"], False),
    (["gh", "issue", "list"], False),
    (["gh", "--version"], False),
    (["git", "remote", "get-url", "origin"], False),
    (["gh", "pr", "comment", "241", "--body", "x"], True),
    (["gh", "issue", "comment", "1", "--body", "x"], True),
    (["gh", "pr", "merge", "1"], True),
    (["gh", "issue", "create", "--title", "t"], True),
    (["gh", "issue", "edit", "1", "--add-label", "x"], True),
    (["gh", "api", "graphql", "-f", "query=mutation{...}"], True),
    (["gh", "some-future-verb", "thing"], True),
])
def test_writes_are_recognised_at_the_transport(argv, is_write):
    """⛔An allowlist of reads, so an unknown verb fails CLOSED. The cost of
    being wrong that way is a test that says so; the cost of the reverse is a
    production mutation."""
    assert _gh_write_argv(argv) is is_write


def test_the_repo_is_read_out_of_the_argv():
    assert _gh_argv_repo(["gh", "pr", "comment", "1", "--repo", "o/r"]) == "o/r"
    assert _gh_argv_repo(["gh", "pr", "comment", "1"]) is None


#: ⛔Probes must target a PR that CANNOT exist. These tests are run with the
#: guard deliberately disabled during mutation testing, and on 2026-09-06 an
#: earlier version of them — which used the real PR #241 — posted "this must
#: not reach production" and "nope" to it four times while I was mutating the
#: guard away. Testing a safety guard means running the dangerous thing with
#: the safety off; the payload therefore has to be harmless on its own.
UNREACHABLE_PR = 999241

ALIAS_PROBE = """
    from agent_crew.github import post_pr_comment      # bound at collection

    def test_writes_through_a_captured_alias(monkeypatch):
        import agent_crew.github as gh
        monkeypatch.setattr(gh, "check_gh_installed", lambda: True)
        monkeypatch.setattr(gh, "get_repo", lambda cwd=None: "truhojunbot-tech/agent_crew")
        post_pr_comment(999241, "this must not reach production")
"""

APPROVED_ALIAS_PROBE = """
    import pytest
    from agent_crew.github import post_pr_comment      # bound at collection

    @pytest.mark.live_github
    def test_writes_to_production_through_an_alias(monkeypatch):
        import agent_crew.github as gh
        monkeypatch.setattr(gh, "check_gh_installed", lambda: True)
        post_pr_comment(999241, "nope", repo="truhojunbot-tech/agent_crew")
"""


def test_a_module_level_alias_cannot_dodge_the_block(tmp_path):
    """★★The reported hole: the writer is imported before any fixture runs."""
    r = _run_probe(tmp_path, ALIAS_PROBE, approved=False)

    combined = _flat(r.stdout + r.stderr)
    assert r.returncode != 0, "a module-level alias wrote unguarded"
    # ⛔The helper swallows the refusal (`except Exception: return False`), so
    #   the probe's own test body passes and the block surfaces at TEARDOWN.
    #   That is the design — the attempt is the defect even when the caller
    #   ignores the exception — so the markers asserted here are the ones
    #   pytest actually prints for a teardown error, not the message text,
    #   which `-q` truncates.
    assert "GitHubWriteFromTest" in combined
    assert "ERROR at teardown" in combined
    assert "tried to mutate" in combined


def test_an_approved_alias_with_a_mismatched_repo_is_blocked(tmp_path):
    """★★Approved, imported early, aimed at production: still refused — and
    refused at the transport, which is the layer the alias dodged.

    ⛔The probe carries the marker. Without it this exercised the DEFAULT path
      and said nothing about the approved one — which is how a mutation
      removing the approved-target check survived a green run of this very
      test.
    """
    r = _run_probe(tmp_path, APPROVED_ALIAS_PROBE, approved=True)

    combined = _flat(r.stdout + r.stderr)
    assert r.returncode != 0
    assert "GitHubWriteFromTest" in combined
    assert "ERROR at teardown" in combined


def test_reads_still_pass_through_the_guard():
    """⛔The suite depends on `pr_state` and `pr_head_sha`. Blocking reads would
    push tests toward mocking the whole module and lose coverage of the real
    call shapes — which is how #255's repo-resolution bug was caught."""
    import agent_crew.github as gh

    r = gh.subprocess.run(["gh", "--version"], capture_output=True, text=True)

    assert r.returncode == 0
