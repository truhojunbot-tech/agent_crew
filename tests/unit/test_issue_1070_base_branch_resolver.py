"""#1070: one base-branch resolver — context, repo default, fleet env, ``main``.

Live evidence: claude_autonomous_trader (default ``master``) ran three script
tests that merged against a stale ``main`` and FAILED with ``merge_conflict``
("no counts parsed") because operator-created review/test tasks carried no
``base_branch`` and every site fell back to the fleet constant.
"""

import ast
import pathlib
import subprocess

import pytest
from fastapi.testclient import TestClient

from agent_crew import script_tester, server
from agent_crew.cli import _sync_worktrees_to_main
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import _prepare_worktree_for_task, create_app
from agent_crew.worktree_default_branch import (
    DEFAULT_BASE_BRANCH, fallback_base_branch, remote_default_branch, resolve_base_branch,
)

SRC = pathlib.Path(server.__file__).parent
RESOLVER = SRC / "worktree_default_branch.py"


def _git(*args):
    return subprocess.run(
        ["git", *map(str, args)], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(tmp_path, *, default="master", stale=None, feature="feature", advance=2):
    """A bare origin whose default is ``default`` and a worker clone of it.

    ``stale`` pins a branch at the first commit while ``default`` gains
    ``advance`` more commits — the trader shape (``origin/main`` far behind
    ``master``). ``feature`` is one commit on top of the default's tip.
    Returns ``(clone, tips)``.
    """
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    _git("init", "--bare", f"--initial-branch={default}", remote)
    _git("init", f"--initial-branch={default}", seed)
    _git("-C", seed, "config", "user.name", "Test User")
    _git("-C", seed, "config", "user.email", "test@example.invalid")
    (seed / "marker").write_text("base")
    _git("-C", seed, "add", "marker")
    _git("-C", seed, "commit", "-m", "base")
    _git("-C", seed, "remote", "add", "origin", remote)
    _git("-C", seed, "push", "origin", default)
    tips = {}
    if stale:
        _git("-C", seed, "push", "origin", f"HEAD:refs/heads/{stale}")
        tips[stale] = _git("-C", seed, "rev-parse", "HEAD")
    for i in range(advance):
        (seed / "marker").write_text(f"{default} {i}")
        _git("-C", seed, "commit", "-am", f"{default} advance {i}")
    _git("-C", seed, "push", "origin", default)
    tips[default] = _git("-C", seed, "rev-parse", "HEAD")
    if feature:
        _git("-C", seed, "checkout", "-b", feature)
        (seed / "feature.txt").write_text(feature)
        _git("-C", seed, "add", "feature.txt")
        _git("-C", seed, "commit", "-m", feature)
        _git("-C", seed, "push", "origin", feature)
        tips[feature] = _git("-C", seed, "rev-parse", "HEAD")
    _git("clone", remote, clone)
    return clone, tips


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)


# --- the resolver itself ------------------------------------------------------

def test_resolver_order_context_repo_env_main(tmp_path, monkeypatch):
    clone, _ = _repo(tmp_path, default="master", stale="main")

    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    assert resolve_base_branch({}, str(clone)) == "master"            # repo default
    assert resolve_base_branch({}) == DEFAULT_BASE_BRANCH             # no repo, no env
    assert resolve_base_branch({}, str(tmp_path / "not-a-repo")) == "main"

    monkeypatch.setenv("AGENT_CREW_MAIN_BRANCH", "develop")
    assert resolve_base_branch({}, str(clone)) == "master"            # repo beats env
    assert resolve_base_branch({}) == "develop"                       # env beats main
    assert fallback_base_branch() == "develop"
    assert resolve_base_branch({"base_branch": "release"}, str(clone)) == "release"
    assert resolve_base_branch({"base_branch": "  "}, str(clone)) == "master"
    assert resolve_base_branch(None) == "develop"


def test_env_is_the_fallback_when_the_repo_default_is_unreadable(tmp_path, monkeypatch):
    clone, _ = _repo(tmp_path, default="trunk", feature=None)
    _git("-C", clone, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    monkeypatch.setenv("AGENT_CREW_MAIN_BRANCH", "develop")

    assert remote_default_branch(str(clone)) == ""
    assert resolve_base_branch({}, str(clone)) == "develop"
    assert server._remote_default_branch(str(clone)) == "develop"


# --- (a) the test stage merges against the repo default ----------------------

def _stub_script_run(monkeypatch):
    monkeypatch.setattr(script_tester, "head_has_workflows", lambda *args: False)
    monkeypatch.setattr(script_tester, "run_script_test",
                        lambda *args, **kwargs: {"outcome": script_tester.PASSED,
                                                 "counts": {"passed": 1}})


def _test_task(**context):
    return TaskRequest(task_id="test-1070", task_type="test", description="test",
                       branch="feature", context={"risk_tier": 1, **context})


@pytest.mark.usefixtures("no_env")
def test_test_stage_merges_against_master_not_stale_main(tmp_path, monkeypatch):
    """(a) repo default ``master``, no context base, no env → base is ``master``."""
    clone, tips = _repo(tmp_path, default="master", stale="main")
    _stub_script_run(monkeypatch)

    result = script_tester.execute_test_task(
        _test_task(), repo=str(clone), state=None, cache_dir=str(tmp_path / "cache"))

    assert result.status == "completed", result.summary
    assert result.error_info["script_test"]["base_branch"] == "master"
    fetched_base = _git("-C", clone, "rev-parse", "refs/agent_crew/script_test/base")
    assert fetched_base == tips["master"]
    assert fetched_base != tips["main"]


def test_test_stage_context_base_branch_wins(tmp_path, monkeypatch):
    """(b) a declared base is used even when it is behind the repo default."""
    clone, tips = _repo(tmp_path, default="master", stale="main")
    monkeypatch.setenv("AGENT_CREW_MAIN_BRANCH", "master")
    _stub_script_run(monkeypatch)

    result = script_tester.execute_test_task(
        _test_task(base_branch="main"), repo=str(clone), state=None,
        cache_dir=str(tmp_path / "cache"))

    assert result.error_info["script_test"]["base_branch"] == "main"
    assert _git("-C", clone, "rev-parse", "refs/agent_crew/script_test/base") == tips["main"]


@pytest.mark.usefixtures("no_env")
def test_main_default_repo_is_unchanged(tmp_path, monkeypatch):
    """(c) a repo whose default is ``main`` resolves, preps, syncs and tests on ``main``."""
    clone, tips = _repo(tmp_path, default="main")
    _stub_script_run(monkeypatch)

    assert resolve_base_branch({}, str(clone)) == "main"
    result = script_tester.execute_test_task(
        _test_task(), repo=str(clone), state=None, cache_dir=str(tmp_path / "cache"))
    assert result.error_info["script_test"]["base_branch"] == "main"
    assert _prepare_worktree_for_task(
        str(clone), "impl-1070", "agent/out", "implementer") == tips["main"]
    assert _sync_worktrees_to_main({"codex": str(clone)}) == {"codex": {
        "requested_ref": "origin/main", "actual_ref": "origin/main",
        "sha": tips["main"], "status": "known"}}


@pytest.mark.usefixtures("no_env")
def test_prep_and_sync_use_master_when_a_stale_main_exists(tmp_path):
    """The trader shape for the implementer: ``origin/main`` exists but is stale."""
    clone, tips = _repo(tmp_path, default="master", stale="main")

    assert _prepare_worktree_for_task(
        str(clone), "impl-1070", "agent/out", "implementer") == tips["master"]
    landed = _sync_worktrees_to_main({"codex": str(clone)})
    assert landed["codex"] == {"requested_ref": "origin/master", "actual_ref": "origin/master",
                               "sha": tips["master"], "status": "known"}


# --- (d) admission fills base_branch from the lineage or the repo ------------

def _app(tmp_db, clone):
    return create_app(tmp_db, project="demo", pane_map={},
                      worktree_map={"implementer": str(clone)},
                      watchdog_disabled=True, anomaly_disabled=True)


def _post(app, payload):
    with TestClient(app) as client:
        return client.post("/tasks", json=payload, headers={"X-Agent-Crew-Project": "demo"})


@pytest.mark.usefixtures("no_env")
def test_admission_fills_base_branch_from_lineage_then_repo(tmp_db, tmp_path):
    clone, _ = _repo(tmp_path, default="master", stale="main")
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="impl-root", task_type="implement", description="impl",
                              branch="feature", context={"base_branch": "release"}))
    queue.enqueue(TaskRequest(task_id="review-mid", task_type="review", description="rev",
                              branch="feature", context={"prev_task_id": "impl-root",
                                                         "pr_number": 41}))
    app = _app(tmp_db, clone)

    # A test task two hops below the declared base inherits it.
    inherited = _post(app, {"task_id": "test-lineage", "task_type": "test",
                            "description": "test", "branch": "feature",
                            "context": {"prev_task_id": "review-mid", "pr_number": 41}})
    assert inherited.status_code == 201, inherited.text
    assert queue.get_task("test-lineage").context["base_branch"] == "release"

    # No lineage: the repo's own default, not the stale `main`.
    fresh = _post(app, {"task_id": "review-fresh", "task_type": "review",
                        "description": "review", "branch": "feature",
                        "context": {"pr_number": 42}})
    assert fresh.status_code == 201, fresh.text
    assert queue.get_task("review-fresh").context["base_branch"] == "master"

    # A declared base is never overwritten.
    declared = _post(app, {"task_id": "test-declared", "task_type": "test",
                           "description": "test", "branch": "feature",
                           "context": {"pr_number": 43, "base_branch": "main"}})
    assert declared.status_code == 201, declared.text
    assert queue.get_task("test-declared").context["base_branch"] == "main"


@pytest.mark.usefixtures("no_env")
def test_admission_without_a_checkout_or_lineage_writes_nothing(tmp_db):
    """The fleet fallback is not persisted into a task at admission."""
    app = create_app(tmp_db, project="demo", pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True)
    posted = _post(app, {"task_id": "review-bare", "task_type": "review",
                         "description": "review", "branch": "feature",
                         "context": {"pr_number": 44}})
    assert posted.status_code == 201, posted.text
    assert "base_branch" not in TaskQueue(tmp_db).get_task("review-bare").context


# --- (e) guard: nothing outside the resolver reads the env or a literal main --

_BASE_NAMES = {"base", "base_branch", "main_branch", "declared_base", "default_branch"}


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _is_main(node) -> bool:
    return isinstance(node, ast.Constant) and node.value == "main"


def _violations(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and node.value == "AGENT_CREW_MAIN_BRANCH"
                and id(node) not in docstrings):
            found.append(f"L{node.lineno}: reads AGENT_CREW_MAIN_BRANCH")
        # `<something> or "main"` — a literal default for a base.
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and _is_main(node.values[-1]):
            found.append(f"L{node.lineno}: `... or \"main\"`")
        # `os.environ.get(NAME, "main")` / `os.getenv(NAME, "main")`.
        if isinstance(node, ast.Call) and len(node.args) == 2 and _is_main(node.args[1]):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("get", "getenv") and isinstance(node.args[0], ast.Constant) \
                    and str(node.args[0].value).isupper():
                found.append(f"L{node.lineno}: env lookup defaulting to \"main\"")
        # `base="main"` keyword, or a base-named assignment / parameter default.
        if isinstance(node, ast.keyword) and node.arg in _BASE_NAMES and _is_main(node.value):
            found.append(f"L{node.lineno}: {node.arg}=\"main\"")
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_main(node.value):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in _BASE_NAMES:
                    found.append(f"L{node.lineno}: {target.id} = \"main\"")
        if isinstance(node, ast.arguments):
            params = node.posonlyargs + node.args
            for arg, default in zip(params[len(params) - len(node.defaults):], node.defaults):
                if arg.arg in _BASE_NAMES and _is_main(default):
                    found.append(f"L{node.lineno if hasattr(node, 'lineno') else '?'}: "
                                 f"param {arg.arg}=\"main\"")
            for arg, default in zip(node.kwonlyargs, node.kw_defaults):
                if arg.arg in _BASE_NAMES and _is_main(default):
                    found.append(f"param {arg.arg}=\"main\"")
    return found


def test_no_module_reads_the_env_or_a_literal_main_as_a_base_outside_the_resolver():
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        if path == RESOLVER:
            continue
        hits = _violations(path)
        if hits:
            offenders[str(path.relative_to(SRC))] = hits
    assert offenders == {}, (
        "base-branch resolution must go through worktree_default_branch.resolve_base_branch "
        f"(#1070): {offenders}")


def test_the_resolver_is_the_only_env_reader():
    assert "AGENT_CREW_MAIN_BRANCH" in RESOLVER.read_text()
    assert _violations(RESOLVER) == [] or all(
        "AGENT_CREW_MAIN_BRANCH" in hit for hit in _violations(RESOLVER))
