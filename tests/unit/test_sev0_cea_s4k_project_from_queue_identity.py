"""SEV-0 CEA step 4k — the project comes from the queue, not from the request.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P2/P4, §3, §7).
Receipt schema: ``tests/cea_contract/receipt.schema.json`` (closed; every
receipt read back here is validated against it).

s4j made every §7 adapter *name* a project and turned an empty one into a
``PROJECT_REQUIRED`` BLOCK receipt instead of an exception. That left one gap,
which Alfred's isolated proof at ``c18e092`` measured: ``POST /tasks`` with no
top-level ``project`` key still reached admission with ``project=''`` — so the
HTTP ingress answered 403 ``PROJECT_REQUIRED`` against a queue that knew
perfectly well which project it was (``~/.agent_crew/agent_crew/state.json``).

s4k closes it on :meth:`agent_crew.queue.TaskQueue.enqueue`, the one admission
entry all fourteen adapters share, so the rule reaches MCP, the CLI, cron/watch,
the pipeline cascades, the loop, the discussion panel and the
retry/requeue/recovery paths without any of them restating it:

* a request that names **no** project is admitted under the queue's identity;
* a request that names a **different** project than the queue's declared
  identity is refused — an ingress translates a transport, it does not choose
  the project (and therefore the rollout mode) it is admitted under;
* ``context.project`` is not a source and never becomes one.

Provenance: written against agent_crew ``c18e092`` (sev0/cea-lineage).
"""
from __future__ import annotations

import ast
import dataclasses
import json
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

from agent_crew.cea.schema import validate_receipt
from agent_crew.queue import AdmissionRefused, TaskQueue

from tests.unit.sev0_cea_acceptance_helpers import (
    SRC, AuthorityState, LiveState, inject_cea, receipt_by_id, receipt_for_task, task)

PROJECT = "agent_crew"


def _project_dir(tmp_path, *, declared: str | None = PROJECT):
    """A queue laid out the way ``crew setup`` lays one out.

    ``<base>/<project>/`` holding ``state.json`` and ``tasks.db`` — the identity
    ``/health`` reports and the one s4k resolves from. ``declared=None`` leaves
    the state file out, which is the case where the queue has only the
    directory-name *guess* to go on.
    """
    root = tmp_path / PROJECT
    root.mkdir()
    if declared is not None:
        (root / "state.json").write_text(json.dumps({"project": declared, "port": 0}),
                                         encoding="utf-8")
    return root / "tasks.db"


def _app(db):
    from agent_crew.server import create_app
    return create_app(db_path=str(db), pane_map={}, port=9999, watchdog_disabled=True,
                      anomaly_disabled=True)


def _post(db, body: dict):
    with TestClient(_app(db), raise_server_exceptions=False) as c:
        return c.post("/tasks", json=body)


def _body(**over) -> dict:
    """A ``POST /tasks`` body with **no** ``project`` key at all — the shape the
    dispatcher, the MCP workers and every hand-written ``curl`` actually send."""
    out = dataclasses.asdict(task("t1"))
    out.pop("project")
    out.update(over)
    return out


def _receipts(db) -> list[dict]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [json.loads(r["receipt_json"]) for r in
                conn.execute("SELECT receipt_json FROM authorization_receipts ORDER BY rowid")]
    finally:
        conn.close()


def _row_project(db, task_id: str):
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT project FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@pytest.fixture
def live(monkeypatch):
    state = LiveState(AuthorityState("active"))
    inject_cea(monkeypatch, state)
    return state


# ═══════════════════════════════════════════════════════════════════════════
# the HTTP ingress — the one the s4k task measured
# ═══════════════════════════════════════════════════════════════════════════

def test_http_post_without_a_project_is_admitted_under_the_queue_project(tmp_path, live):
    """The gap s4j left: 201, and the receipt names the queue's project.

    Before s4k this was 403 ``PROJECT_REQUIRED`` — the body had no ``project``
    key, ``TaskRequest.project`` defaulted to ``''``, and admission refused it.
    """
    db = _project_dir(tmp_path)
    resp = _post(db, _body())
    assert resp.status_code == 201, resp.text
    got = [r for r in _receipts(db) if r["task_id"] == "t1"][-1]
    assert not validate_receipt(got), validate_receipt(got)
    assert got["project"] == PROJECT
    # not BLOCK: this fixture's implement intent lands on REVIEW
    # (IDENTITY_UNVERIFIED_REVIEW_REQUIRED, P2a) — what s4k changed is that the
    # project no longer refuses it before any of that is reached.
    assert got["decision"] != "BLOCK", got["reason"]
    # the row and the receipt agree about which project this task is
    assert _row_project(db, "t1") == PROJECT


def test_http_post_naming_a_foreign_project_is_refused(tmp_path, live):
    """An ingress cannot choose its project: 4xx, with the audit row to read.

    The receipt is filed under the queue that refused — the evidence of a
    cross-project attempt belongs where the refusing queue can find it — and the
    name that was asked for survives in ``reason.text``.
    """
    db = _project_dir(tmp_path)
    resp = _post(db, _body(project="someone-elses-project"))
    assert 400 <= resp.status_code < 500, resp.status_code
    assert resp.status_code == 403, resp.status_code
    assert resp.json()["receipt_id"]
    got = receipt_by_id_from(db, resp.json()["receipt_id"])
    assert not validate_receipt(got), validate_receipt(got)
    assert got["decision"] == "BLOCK"
    assert got["reason"]["code"] == "PROJECT_MISMATCH"
    assert got["project"] == PROJECT
    assert "someone-elses-project" in got["reason"]["text"]
    # refused means refused: no row was written
    assert _row_project(db, "t1") is None


def receipt_by_id_from(db, receipt_id: str):
    from agent_crew.cea import store as receipt_store
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return receipt_store.current_receipt(conn, receipt_id)
    finally:
        conn.close()


def _queue_with_origin(tmp_path, origin: str):
    db = _project_dir(tmp_path)
    worktree = tmp_path / "checkout"
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(worktree), "remote", "add", "origin", origin],
                   check=True, capture_output=True)
    state_path = db.parent / "state.json"
    state = json.loads(state_path.read_text())
    state["worktrees"] = {"codex": str(worktree)}
    state_path.write_text(json.dumps(state))
    return db


@pytest.mark.parametrize(("origin", "target"), [
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo.git", "git@github.com:owner/repo.git"),
    ("git@github.com:owner/repo.git", "https://github.com/owner/repo"),
])
def test_matching_target_repo_is_admitted(tmp_path, live, origin, target):
    db = _queue_with_origin(tmp_path, origin)
    resp = _post(db, _body(context={"target_repo": target}))
    assert resp.status_code == 201, resp.text
    assert _row_project(db, "t1") == PROJECT


@pytest.mark.parametrize("target", [
    "elsewhere/other",
    "https://elsewhere.example/path/github.com/owner/repo",
])
def test_mismatched_target_repo_is_403_with_block_receipt_and_no_task(
        tmp_path, live, target):
    db = _queue_with_origin(tmp_path, "git@github.com:owner/repo.git")
    resp = _post(db, _body(context={"target_repo": target}))
    assert resp.status_code == 403, resp.text
    got = receipt_by_id_from(db, resp.json()["receipt_id"])
    assert got["decision"] == "BLOCK"
    assert got["reason"]["code"] == "PROJECT_MISMATCH"
    assert target in got["reason"]["text"]
    assert _row_project(db, "t1") is None


def test_absent_target_repo_remains_admitted(tmp_path, live):
    db = _queue_with_origin(tmp_path, "https://github.com/owner/repo.git")
    resp = _post(db, _body())
    assert resp.status_code == 201, resp.text
    assert _row_project(db, "t1") == PROJECT


def test_target_repo_refuses_when_queue_origin_is_unknown(tmp_path, live):
    db = _project_dir(tmp_path)
    resp = _post(db, _body(context={"target_repo": "owner/repo"}))
    assert resp.status_code == 403, resp.text
    got = receipt_by_id_from(db, resp.json()["receipt_id"])
    assert got["decision"] == "BLOCK"
    assert got["reason"]["code"] == "PROJECT_MISMATCH"
    assert "could not determine" in got["reason"]["text"]
    assert _row_project(db, "t1") is None


def test_target_repo_mismatch_uses_same_refusal_for_every_ingress(tmp_path, live):
    db = _queue_with_origin(tmp_path, "https://github.com/owner/repo.git")
    q = TaskQueue(str(db))
    for ingress in sorted(__import__("agent_crew.cea.adapters",
                                     fromlist=["BY_ID"]).BY_ID):
        request = task(f"repo-{ingress}", project=PROJECT)
        request.context = {"target_repo": "another/project"}
        with pytest.raises(AdmissionRefused) as exc:
            q.enqueue(request, ingress=ingress)
        got = receipt_by_id(q, exc.value.receipt_id)
        assert got["decision"] == "BLOCK", ingress
        assert got["reason"]["code"] == "PROJECT_MISMATCH", ingress
        assert _row_project(db, request.task_id) is None


def test_enforce_without_a_credential_is_403_with_a_receipt_naming_the_queue_project(
        tmp_path, monkeypatch):
    """P7: ``enforce`` embedded refuses — and the refusal is a *receipt*, not a crash.

    This is the interaction s4k has to get right: the enforce refusal is minted
    from the intent, so a project that was never filled in would have put an
    empty ``$.project`` into a closed schema and turned the refusal into an
    ``EngineError``. It names the queue's project because the fill happened
    first.
    """
    inject_cea(monkeypatch, LiveState(AuthorityState("active")), mode="enforce")
    db = _project_dir(tmp_path)
    resp = _post(db, _body())
    assert resp.status_code == 403, resp.text
    got = receipt_by_id_from(db, resp.json()["receipt_id"])
    assert not validate_receipt(got), validate_receipt(got)
    assert got["decision"] == "BLOCK"
    assert got["reason"]["code"] == "CREDENTIAL_BOUNDARY_UNAVAILABLE"
    assert got["project"] == PROJECT


def test_context_project_is_ignored_as_a_source(tmp_path, live):
    """``context.project`` is not a second spelling of the project.

    A free-form dict a caller fills in is exactly the place a project must not
    come from: it would be a second steering wheel on the rollout mode.
    """
    db = _project_dir(tmp_path)
    resp = _post(db, _body(context={"project": "smuggled"}))
    assert resp.status_code == 201, resp.text
    got = [r for r in _receipts(db) if r["task_id"] == "t1"][-1]
    assert got["project"] == PROJECT
    assert _row_project(db, "t1") == PROJECT


def test_intent_for_task_reads_only_the_task_project(tmp_path, live):
    """The same claim as above, pinned at the source so a future edit cannot
    quietly add ``ctx.get("project")`` back."""
    src = (SRC / "queue.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "intent_for_task")
    body = ast.unparse(fn).replace('"', "'")
    assert "project=task.project or ''" in body
    # ...and `ctx` — the caller's free-form dict — is never asked for one
    assert "ctx.get('project')" not in body and "ctx['project']" not in body
    # the same claim for the product as a whole: no module reads a project out
    # of a task context, under any spelling
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for spelling in ("context.get(\"project\")", "context.get('project')",
                         "context[\"project\"]", "context['project']",
                         "ctx.get(\"project\")", "ctx.get('project')"):
            assert spelling not in text, f"{path}: {spelling} — context.project is not a source"


# ═══════════════════════════════════════════════════════════════════════════
# every ingress, not just HTTP
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("ingress", sorted(
    __import__("agent_crew.cea.adapters", fromlist=["BY_ID"]).BY_ID))
def test_every_registered_ingress_fills_from_the_queue_identity(tmp_path, live, ingress):
    """The rule lives on ``TaskQueue.enqueue``, so all fourteen get it at once.

    Driven through the common admission entry under each adapter's own ingress
    id — the same construction ``test_sev0_cea_i1_property`` uses for its
    dynamic half — because what is being asserted is precisely that no adapter
    has to restate the rule.
    """
    db = _project_dir(tmp_path)
    q = TaskQueue(str(db))
    q.enqueue(task(f"t-{ingress}", project=""), ingress=ingress)
    got = receipt_for_task(q, f"t-{ingress}")
    assert not validate_receipt(got), validate_receipt(got)
    assert got["project"] == PROJECT
    assert got["decision"] != "BLOCK", got["reason"]


def test_a_foreign_project_is_refused_for_every_ingress(tmp_path, live):
    db = _project_dir(tmp_path)
    q = TaskQueue(str(db))
    for ingress in sorted(__import__("agent_crew.cea.adapters",
                                     fromlist=["BY_ID"]).BY_ID):
        with pytest.raises(AdmissionRefused) as exc:
            q.enqueue(task(f"f-{ingress}", project="not-this-queue"), ingress=ingress)
        got = receipt_by_id(q, exc.value.receipt_id)
        assert got["reason"]["code"] == "PROJECT_MISMATCH", ingress
        assert got["project"] == PROJECT, ingress


# ═══════════════════════════════════════════════════════════════════════════
# the asymmetry between the declaration and the guess
# ═══════════════════════════════════════════════════════════════════════════

def test_the_directory_guess_fills_a_gap_but_never_contradicts_a_caller(tmp_path, live):
    """⛔The stated residue, asserted rather than left implicit.

    With no ``state.json`` the queue only has the directory name. That is good
    enough to *fill* a missing project — the alternative is refusing a task over
    a value nobody disputed — and deliberately not good enough to *refuse* one:
    a directory name is an inference, and refusing legitimate work over an
    inference is not recoverable the way a skipped refusal is. The way to close
    this is to give the queue a declaration, not to harden the guess.
    """
    db = _project_dir(tmp_path, declared=None)
    q = TaskQueue(str(db))
    assert q.declared_project == ""
    assert q.queue_project == PROJECT          # the directory name
    q.enqueue(task("filled", project=""), ingress="http.tasks")
    assert receipt_for_task(q, "filled")["project"] == PROJECT
    # ...and a caller who names something else is still admitted under it
    q.enqueue(task("named", project="other-project"), ingress="http.tasks")
    assert receipt_for_task(q, "named")["project"] == "other-project"


def test_with_no_identity_at_all_an_empty_project_is_still_project_required(tmp_path, live):
    """A DB sitting in the ``.agent_crew`` root has no project to be. s4j's
    ``PROJECT_REQUIRED`` receipt is still the honest answer there — s4k did not
    replace it, it gave the queues that *do* have an identity a way past it."""
    root = tmp_path / ".agent_crew"
    root.mkdir()
    q = TaskQueue(str(root / "tasks.db"))
    assert q.queue_project == ""
    with pytest.raises(AdmissionRefused) as exc:
        q.enqueue(task("n1", project=""), ingress="http.tasks")
    got = receipt_by_id(q, exc.value.receipt_id)
    assert got["reason"]["code"] == "PROJECT_REQUIRED"


def test_the_declaration_beats_the_directory(tmp_path, live):
    """``state.json`` is the identity, not the path. A queue whose directory and
    declaration disagree is admitted under the declaration — and a request
    naming the *directory* is refused, because the directory is not who it is."""
    db = _project_dir(tmp_path, declared="renamed-project")
    q = TaskQueue(str(db))
    assert q.queue_project == "renamed-project"
    q.enqueue(task("d1", project=""), ingress="http.tasks")
    assert receipt_for_task(q, "d1")["project"] == "renamed-project"
    with pytest.raises(AdmissionRefused) as exc:
        q.enqueue(task("d2", project=PROJECT), ingress="http.tasks")
    assert receipt_by_id(q, exc.value.receipt_id)["reason"]["code"] == "PROJECT_MISMATCH"


def test_a_foreign_project_cannot_pick_its_own_rollout_mode(tmp_path, live, monkeypatch):
    """Why the mismatch is a refusal and not a warning.

    ``project`` selects the rollout mode
    (``AGENT_CREW_CEA_MODE__<PROJECT>``). If an ingress could name one, a task
    could be admitted under ``shadow`` inside a queue its operator had moved to
    ``enforce`` — by putting a string in a JSON body.
    """
    db = _project_dir(tmp_path)
    q = TaskQueue(str(db))
    q._cea_config_override = None              # resolve from the environment
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__AGENT_CREW", "enforce")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__ELSEWHERE", "shadow")
    assert q._admission_project(task("x", project="elsewhere")) == PROJECT
    assert q.cea_config(q._admission_project(task("x", project="elsewhere"))).mode == "enforce"
