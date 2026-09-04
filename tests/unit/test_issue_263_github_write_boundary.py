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
                gh.post_pr_comment(241, "synthetic")
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
