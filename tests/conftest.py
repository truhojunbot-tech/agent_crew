import os
import re
import shutil
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


#: Every function in `agent_crew.github` that MUTATES something on GitHub.
#: Reads (`get_repo`, `pr_state`, `pr_head_sha`, `branch_has_pr`, …) are left
#: alone: they are safe, and blocking them would push tests toward mocking the
#: whole module and losing coverage of the real call shapes.
GITHUB_WRITE_FUNCTIONS = (
    "post_pr_comment",
    "post_review_comment",
    "post_discussion_comment",
    "create_pr",
    "create_issue",
    "merge_pr",
)


class GitHubWriteFromTest(AssertionError):
    """Raised when the suite tries to mutate a real GitHub object."""


#: Environment gate for tests that genuinely need the live write path. Default
#: off: absence is refusal, not permission.
LIVE_GITHUB_ENV = "AGENT_CREW_ALLOW_LIVE_GITHUB"
#: The repository such a test is allowed to write to. Must be named explicitly;
#: "whatever repo the checkout points at" is exactly the wrong default.
LIVE_GITHUB_REPO_ENV = "AGENT_CREW_LIVE_GITHUB_REPO"
#: A disposable target has to look disposable. This is a mechanical check, not
#: a guarantee — but it stops the most likely accident, which is pointing the
#: opt-in at the production repo because that is what is already configured.
DISPOSABLE_REPO_PATTERN = re.compile(
    r"(sandbox|scratch|disposable|throwaway|fixture|-test$|_test$)", re.IGNORECASE)


#: `gh` invocations that only READ. Everything else is treated as a write.
#:
#: ⛔An allowlist, not a blocklist. A `gh` verb nobody thought about should fail
#:   closed at a write boundary; the cost of getting that wrong is a test that
#:   says so, and the cost of the reverse is a production mutation.
GH_READ_ONLY_COMMANDS = frozenset({
    ("pr", "view"), ("pr", "list"), ("pr", "diff"), ("pr", "status"),
    ("pr", "checks"), ("issue", "view"), ("issue", "list"),
    ("repo", "view"), ("auth", "status"),
})


#: gh global flags that take a VALUE. Skipping the flag without its value would
#: read that value as the command — `gh --repo o/r pr comment` would look like
#: the command `o/r pr`.
GH_VALUE_FLAGS = frozenset({"--repo", "-R", "--hostname"})


def _gh_command(parts) -> tuple:
    """The (group, verb) of a gh argv, ignoring global flags.

    ⛔Global flags come BEFORE the command: `gh --repo o/r pr comment 1` is a
      valid mutation, and treating any leading flag as "not a command" let it
      through both the block and the approved-target pin (review of PR #264).
      `gh --repo <owner/repo> pr comment --help` exits 0, so this is a form gh
      really accepts, not a theoretical one.
    """
    words = []
    i = 1
    while i < len(parts) and len(words) < 2:
        token = parts[i]
        if token.startswith("-"):
            # Always advances: a value flag consumes its value, anything else
            # consumes itself. Written as one expression because a branchy
            # version can fail to advance and spin — mine did, under a mutation
            # that removed the value-flag case.
            i += 2 if token in GH_VALUE_FLAGS else 1
            continue
        words.append(token)
        i += 1
    return tuple(words)


def _gh_write_argv(argv) -> bool:
    """Is this subprocess argv a GitHub MUTATION?"""
    try:
        parts = [str(a) for a in argv]
    except Exception:  # noqa: BLE001
        return False
    if not parts or os.path.basename(parts[0]) != "gh":
        return False
    command = _gh_command(parts)
    if not command:
        return False                      # `gh --version`, `gh --help`
    return command not in GH_READ_ONLY_COMMANDS


def _gh_argv_repo(argv):
    """The repository named anywhere in a gh argv, or None.

    Handles the global form (`gh --repo o/r pr comment`), the per-command form
    (`gh pr comment --repo o/r`), the short flag and the `=` spelling — a pin
    that only understood one of them would be a pin with a hole in it.
    """
    parts = [str(a) for a in argv]
    for i, token in enumerate(parts):
        if token in ("--repo", "-R") and i + 1 < len(parts):
            return parts[i + 1]
        if token.startswith("--repo="):
            return token.split("=", 1)[1]
        if token.startswith("-R=") :
            return token.split("=", 1)[1]
    return None


def _gh():
    import agent_crew.github as gh

    return gh


def live_github_approval(env=None, production_repo=None) -> tuple:
    """``(approved, reason)`` for running a `live_github` test.

    Three conditions, all required:

      * `AGENT_CREW_ALLOW_LIVE_GITHUB` is truthy — an operator said yes, on
        this run, out loud;
      * `AGENT_CREW_LIVE_GITHUB_REPO` names the target — never inferred;
      * that target is not the repository this checkout points at, and its name
        marks it as disposable.

    The second and third exist because the first is easy to leave switched on.
    """
    env = os.environ if env is None else env
    if str(env.get(LIVE_GITHUB_ENV, "")).strip().lower() not in ("1", "true", "yes", "on"):
        return (False, f"{LIVE_GITHUB_ENV} is not set")
    target = str(env.get(LIVE_GITHUB_REPO_ENV, "")).strip()
    if not target:
        return (False, f"{LIVE_GITHUB_REPO_ENV} is not set — a live test must "
                       f"name its disposable target")
    if production_repo is None:
        try:
            production_repo = _gh().get_repo(
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        except Exception:  # noqa: BLE001
            production_repo = None
    if production_repo and target == production_repo:
        return (False, f"{LIVE_GITHUB_REPO_ENV}={target!r} is this checkout's own "
                       f"repository, not a disposable target")
    if not DISPOSABLE_REPO_PATTERN.search(target):
        return (False, f"{LIVE_GITHUB_REPO_ENV}={target!r} does not look disposable "
                       f"(expected one of sandbox/scratch/disposable/throwaway/"
                       f"fixture/-test)")
    return (True, f"approved for {target}")


def _install_gh_transport_guard(monkeypatch, gh, sink, observing, target=None):
    """Guard `gh` mutations at the subprocess boundary.

    Below every import alias, because that is the level a test cannot capture
    early. Reads pass through untouched — the suite depends on `pr_state` and
    `pr_head_sha`, and blocking those would push tests toward mocking the whole
    module and lose coverage of the real call shapes.

    `target` is set only for an approved live test: then a mutation is allowed
    if and only if it names that repository on the command line.
    """
    real_run = gh.subprocess.run

    def guarded_run(argv, *args, **kwargs):
        if not _gh_write_argv(argv):
            return real_run(argv, *args, **kwargs)
        parts = [str(a) for a in argv]
        if target is not None:
            repo = _gh_argv_repo(parts)
            if repo == target:
                return real_run(argv, *args, **kwargs)
            msg = (f"a gh mutation reached the transport layer targeting "
                   f"{repo!r}, but the approved disposable target is "
                   f"{target!r}: {' '.join(parts[:4])}")
            # ⛔Recorded as well as raised, on the SAME sink the default path
            #   uses. The helpers swallow exceptions, so a refusal that is only
            #   raised is invisible to the test that caused it.
            sink.append({"fn": "gh", "args": tuple(parts[:4]), "kwargs": {},
                         "refused": msg})
            raise GitHubWriteFromTest(msg)
        sink.append({"fn": "gh", "args": tuple(parts[:4]), "kwargs": {}})
        if observing:
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()
        msg = (f"a gh MUTATION was issued from a test: {' '.join(parts[:4])}. "
               f"This was caught below the import alias, which means something "
               f"called a writer the fixture could not patch by name.")
        raise GitHubWriteFromTest(msg)

    monkeypatch.setattr(gh.subprocess, "run", guarded_run)


def _pinned_to_repo(name, fn, target):
    """Allow `fn` only when it is explicitly told to write to `target`.

    The repo is read out of the CALL, via the function's own signature, so this
    holds for every write helper and for any added later. Three refusals:

      * the function has no `repo` parameter — we cannot prove where it writes;
      * `repo` was not passed, or passed as None — it would fall back to the
        checkout's origin, i.e. production;
      * `repo` names something other than the approved disposable target.
    """
    import functools
    import inspect

    @functools.wraps(fn)
    def _guarded(*args, **kwargs):
        try:
            bound = inspect.signature(fn).bind(*args, **kwargs)
        except TypeError:
            raise GitHubWriteFromTest(
                f"agent_crew.github.{name}() called with arguments that do not "
                f"match its signature; refusing to guess the target"
            )
        if "repo" not in bound.signature.parameters:
            raise GitHubWriteFromTest(
                f"agent_crew.github.{name}() has no `repo` parameter, so an "
                f"approved live test cannot prove where it writes"
            )
        bound.apply_defaults()
        repo = bound.arguments.get("repo")
        if not repo:
            raise GitHubWriteFromTest(
                f"agent_crew.github.{name}() was called without an explicit "
                f"`repo`; it would fall back to this checkout's origin — pass "
                f"repo={target!r}"
            )
        if repo != target:
            raise GitHubWriteFromTest(
                f"agent_crew.github.{name}() targets {repo!r}, but the approved "
                f"disposable target is {target!r}"
            )
        return fn(*args, **kwargs)

    return _guarded


def _blocked_live(name, why):
    def _raise(*args, **kwargs):
        raise GitHubWriteFromTest(
            f"agent_crew.github.{name}() was called from an UNAPPROVED "
            f"live_github test: {why}"
        )
    return _raise


@pytest.fixture
def github_writes():
    """Opt in to OBSERVING GitHub writes instead of failing on them.

    A test that exercises a code path whose side effect is a GitHub comment
    requests this fixture and gets the recorded calls. Nothing reaches the
    network either way — this only changes whether an attempt is a defect or
    the thing under test.
    """
    return []


@pytest.fixture(autouse=True)
def _no_github_writes(request, github_writes_recorder, monkeypatch):
    """⛔The default suite may not write to GitHub. Ever.

    Not hypothetical caution. `tests/unit/test_issue_250_terminal_pr_gate.py`
    used `PR = 241` — a real, merged PR — and drove the real result handler,
    which posts a review comment for any review result carrying a pr_number.
    Nothing patched that path, so **every full-suite run posted three comments
    to PR #241**: 228 of that PR's 263 comments were fixture data, 76 from each
    of three test task ids. Two older tests were doing the same to PR #42 (#263).

    Individual tests remembering to patch is precisely what failed, so the block
    is autouse and sits at the boundary. Two escapes, both explicit:

      * request the `github_writes` fixture — the call is recorded and returns
        success, for tests whose subject IS the side effect;
      * mark `@pytest.mark.live_github` — no stubbing at all, for a deliberate
        smoke test that must point at a disposable target.
    """
    if "live_github" in request.keywords:
        approved, why = live_github_approval()
        if approved:
            # ⛔Approval names a target; it does not hand over the write
            #   functions. They all take `repo=None` and fall back to
            #   `get_repo()`, which resolves to the real checkout's origin — so
            #   an approved test calling `post_pr_comment(241, "x")` with no
            #   repo would write to PRODUCTION, which is the thing the marker
            #   was granted an exception from (review of PR #264). Every write
            #   is therefore pinned to the approved repository at the boundary.
            target = str(os.environ.get(LIVE_GITHUB_REPO_ENV, "")).strip()
            for name in GITHUB_WRITE_FUNCTIONS:
                fn = getattr(_gh(), name, None)
                if fn is not None:
                    monkeypatch.setattr(_gh(), name, _pinned_to_repo(name, fn, target))
            # ...and below the aliases, where a module-level import cannot dodge it.
            _install_gh_transport_guard(monkeypatch, _gh(), github_writes_recorder,
                                        False, target=target)
            yield
            refusals = [c["refused"] for c in github_writes_recorder
                        if isinstance(c, dict) and c.get("refused")]
            if refusals:
                # ⛔The helpers wrap their `gh` calls in `except Exception`, so a
                #   refusal that is merely raised is swallowed and the offending
                #   test still passes. The attempt is the defect.
                raise GitHubWriteFromTest(
                    "an approved live test tried to write outside its target:\n  "
                    + "\n  ".join(refusals))
            return
        if not approved:
            # ⛔The marker is a REQUEST, not permission. Left as a bare escape,
            #   any test could opt itself out of the boundary and a normal
            #   `pytest` run would write to the production repo again — the
            #   exact hole this whole guard exists to close (review of PR #264).
            #   Unapproved means skipped AND still stubbed: if some future
            #   collection path runs it anyway, it still cannot write.
            for name in GITHUB_WRITE_FUNCTIONS:
                if hasattr(_gh(), name):
                    monkeypatch.setattr(_gh(), name, _blocked_live(name, why))
            _install_gh_transport_guard(monkeypatch, _gh(), github_writes_recorder,
                                        False, target=None)
            pytest.skip(f"live_github test not approved: {why}")
        yield
        return

    import agent_crew.github as gh

    observing = "github_writes" in request.fixturenames
    sink = request.getfixturevalue("github_writes") if observing else github_writes_recorder

    def _stub(name):
        def _call(*args, **kwargs):
            sink.append({"fn": name, "args": args, "kwargs": kwargs})
            if observing:
                return True
            # ⛔Raised AND recorded. Raising alone is not enough: the dispatcher
            #   wraps GitHub calls in `except Exception: logger.exception`, so a
            #   blocked write is swallowed and the offending test still passes —
            #   which is how three of them targeted a real PR through dozens of
            #   green runs. The teardown turns a swallowed attempt into a failure.
            raise GitHubWriteFromTest(
                f"agent_crew.github.{name}() was called from a test. The suite "
                f"must not mutate real GitHub objects — patch it, request the "
                f"`github_writes` fixture if the write is the thing under test, "
                f"or mark the test @pytest.mark.live_github with a disposable "
                f"target."
            )
        return _call

    for name in GITHUB_WRITE_FUNCTIONS:
        if hasattr(gh, name):
            monkeypatch.setattr(gh, name, _stub(name))
    # ⛔The function stubs are the readable half; this is the load-bearing one.
    #   Patching `agent_crew.github.post_pr_comment` does nothing to a name a
    #   test module already bound with `from agent_crew.github import
    #   post_pr_comment` at collection time — that alias is captured before any
    #   fixture runs and calls the ORIGINAL (review of PR #264). Every helper
    #   ultimately reaches `subprocess.run(["gh", ...])`, and `subprocess.run`
    #   is resolved through the module at call time, so guarding it there is
    #   below every alias.
    _install_gh_transport_guard(monkeypatch, gh, sink, observing, target=None)
    yield
    if not observing and github_writes_recorder:
        calls = "\n  ".join(f"{c['fn']}(kwargs={c['kwargs']})"
                             for c in github_writes_recorder)
        raise GitHubWriteFromTest(
            "this test tried to mutate GitHub; the call was blocked, but the "
            f"attempt is the defect:\n  {calls}"
        )



#: tmux subcommands that WRITE into a pane. `send-keys` and the buffer pair are
#: how the dispatcher delivers a task block; everything else tmux offers here
#: (capture-pane, list-panes, display-message, new-session) only reads or works
#: on the suite's own throwaway session, so it passes through.
TMUX_INJECTION_SUBCOMMANDS = ("send-keys", "paste-buffer", "load-buffer")


def _tmux_injection_argv(argv) -> bool:
    """Is this subprocess argv a tmux write INTO a pane?"""
    if isinstance(argv, (str, bytes)):
        return False
    try:
        parts = [str(a) for a in argv]
    except TypeError:
        return False
    if not parts or os.path.basename(parts[0]) != "tmux":
        return False
    return any(part in TMUX_INJECTION_SUBCOMMANDS for part in parts[1:])


@pytest.fixture
def github_writes_recorder():
    return []


@pytest.fixture(autouse=True)
def _mock_pane_alive_for_push(request):
    """Default all pane liveness checks to True in unit tests.

    Tests that need to simulate dead panes use monkeypatch to override
    agent_crew.server._pane_alive_for_push themselves.
    """
    if "no_pane_alive_mock" in request.keywords:
        yield
        return
    with patch("agent_crew.server._pane_alive_for_push", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _mock_pane_process_kind(request):
    """Default the G_DT pane-process probe to "an agent is running".

    Fixture pane ids are not real panes, so the real probe would fail closed
    and refuse every push. Tests of the guard itself opt out with
    `@pytest.mark.real_pane_process_kind` or monkeypatch the probe.
    """
    if "real_pane_process_kind" in request.keywords:
        yield
        return
    with patch("agent_crew.server._pane_process_kind",
               return_value=("agent", "current_command=claude")):
        yield


@pytest.fixture
def tmux_injections():
    """Every pane write the suite attempted, as argv tuples."""
    return []


@pytest.fixture(autouse=True)
def _no_tmux_injection(request, tmux_injections, monkeypatch):
    """Keep pane writes issued by a test inside the test.

    A pane id in a fixture is a plausible-looking string, but tmux hands the
    same ids to real panes: `pane_map={"reviewer": "%91"}` pushed a fixture task
    block into a live session on this machine roughly every time the suite ran.
    Guarded at the subprocess boundary, below every import alias, for the same
    reason the gh guard sits there — a test cannot capture it early.

    Pane writes are recorded and answered with fake success. This preserves
    assertions about dispatch attempts while making every test scope incapable
    of reaching the real tmux server; broad production exception handlers
    cannot swallow this containment boundary.
    """
    if "allow_tmux_injection" in request.keywords:
        yield
        return

    real_run = subprocess.run

    def guarded_run(argv, *args, **kwargs):
        if not _tmux_injection_argv(argv):
            return real_run(argv, *args, **kwargs)
        tmux_injections.append(tuple(str(a) for a in argv))
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(subprocess, "run", guarded_run)
    yield


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def task_queue(tmp_db):
    return TaskQueue(tmp_db)


@pytest.fixture
def test_client(tmp_db):
    app = create_app(tmp_db)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def tmux_session():
    if not shutil.which("tmux"):
        pytest.skip("tmux not available")
    subprocess.run(["tmux", "new-session", "-d", "-s", "test_crew"], capture_output=True)
    yield "test_crew"
    subprocess.run(["tmux", "kill-session", "-t", "test_crew"], capture_output=True)


@pytest.fixture
def resolve_approved():
    """Valid resolve body approving a gate — {"status": "approved"}."""
    return {"status": "approved"}


@pytest.fixture
def resolve_rejected():
    """Valid resolve body rejecting a gate — {"status": "rejected"}."""
    return {"status": "rejected"}


@pytest.fixture
def stub_agents(tmp_path):
    scripts = {}
    for agent in ["claude", "codex"]:
        script = tmp_path / f"{agent}_stub.sh"
        script.write_text("#!/bin/sh\necho stub agent running\n")
        script.chmod(0o755)
        scripts[agent] = str(script)
    return scripts


# ── P6 loosening authority for the suites ────────────────────────────────────
#
# A runtime with no verifier refuses every loosening (`RefuseAllLoosening`), which
# is the fail-closed default and is tested directly in
# `tests/unit/test_sev0_runtime_authority.py`. Suites that legitimately resume a
# runtime need a *verifier that grants*, not a bypass — so they get a real
# `SnapshotLooseningAuthority` over a signed fake snapshot carrying the decision
# record they cite. The verification path under test is the production one; only
# the snapshot is a fixture.

TEST_BUILD_COMMIT = "b" * 40
TEST_DECISION_ID = "D-51-9"
TEST_OWNER_PRINCIPALS = ("owner", "owner:test", "owner:hojun", "owner:alfred", "owner:cli")


def signed_snapshot(*, decision_ids=(TEST_DECISION_ID,), principals=TEST_OWNER_PRINCIPALS,
                    build_commits=(TEST_BUILD_COMMIT,), runtimes=(), generation=7):
    """A `PolicySnapshotProvider` whose snapshot verifies as VALID."""
    from agent_crew.cea.providers import PolicySnapshotRef, SignatureStatus
    from agent_crew.cea.receipt import DecisionRev

    records = tuple(
        DecisionRev(decision_id=d, body_hash="a" * 32, principals=tuple(principals),
                    build_commits=tuple(build_commits), runtimes=tuple(runtimes))
        for d in decision_ids)

    class _Snapshots:
        def current(self, intent=None):
            return PolicySnapshotRef(generation=generation, hash="h" * 16, produced_at=None,
                                     decisions=records, in_scope=records,
                                     signature=SignatureStatus.VALID)

    return _Snapshots()


def granting_authority(**kw):
    """The production verifier, pointed at a snapshot that does grant."""
    from agent_crew.queue import SnapshotLooseningAuthority
    build = kw.pop("build_commit", TEST_BUILD_COMMIT)
    return SnapshotLooseningAuthority(signed_snapshot(**kw), build_commit=build)


@pytest.fixture(autouse=True)
def _p6_runtime_authority():
    """Install the granting verifier for the duration of each test.

    Autouse because 54 `TaskQueue(...)` call sites across the suites resume a
    runtime as a *precondition* of what they actually test; without a verifier
    every one of them would be refused, which would test the refusal 54 times and
    the thing they are about zero times.
    """
    from agent_crew import queue as _queue

    _queue.set_default_runtime_authority(granting_authority())
    try:
        yield
    finally:
        _queue.set_default_runtime_authority(None)
