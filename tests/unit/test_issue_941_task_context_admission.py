"""#941: reject malformed task context at the HTTP write boundary."""

import ast
import json
from pathlib import Path

from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app
from agent_crew.task_context import TASK_CONTEXT_KEYS


def _payload(task_id, context=None, **extra):
    return {"task_id": task_id, "task_type": "implement", "description": "work",
            "branch": "feature/941", "context": {"risk_tier": 1, **(context or {})},
            **extra}


def _post(app, payload):
    with TestClient(app) as client:
        return client.post("/tasks", json=payload, headers={"X-Agent-Crew-Project": "demo"})


def _app(tmp_db, **kwargs):
    return create_app(tmp_db, project="demo", pane_map={}, worktree_map={},
                      watchdog_disabled=True, anomaly_disabled=True, **kwargs)


def test_unknown_key_refused_and_known_key_accepted(tmp_db):
    app = _app(tmp_db)
    bad = _post(app, _payload("unknown", {"reviwed_sha": "a" * 40}))
    assert bad.status_code == 422
    assert bad.json()["detail"] == "unknown_context_key:reviwed_sha"
    good = _post(app, _payload("known", {"reviewed_sha": "a" * 40}))
    assert good.status_code == 201, good.text


def test_rebase_requires_repo_default_branch(tmp_db, monkeypatch):
    monkeypatch.setattr(server, "_task_context_default_branch", lambda repo: "main", raising=False)
    monkeypatch.setattr(server, "_task_context_open_pr", lambda branch, repo: 947, raising=False)
    app = _app(tmp_db)
    bad = _post(app, _payload("rebase-bad", {"artifact_kind": "rebase", "repo": "owner/repo"}))
    assert bad.status_code == 422
    assert "rebase_onto" in bad.text
    good = _post(app, _payload("rebase-good", {"artifact_kind": "rebase", "repo": "owner/repo",
                                             "rebase_onto": "main"}))
    assert good.status_code == 201, good.text
    directive = _post(app, _payload("rebase-directive", {"repo": "owner/repo"},
                                    description="Rebase the existing PR branch"))
    assert directive.status_code == 422
    assert "rebase_onto" in directive.text


def test_cloud_existing_pr_requires_matching_number(tmp_db, tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"project": "demo", "role_agents": {
        "implementer": "claude_cloud", "reviewer": "codex", "tester": "script"}}))
    monkeypatch.setattr(server, "_task_context_open_pr", lambda branch, repo: 947, raising=False)
    app = _app(tmp_db, state_path=str(state))
    bad = _post(app, _payload("cloud-bad", {"repo": "owner/repo"}))
    assert bad.status_code == 422
    assert "pr_number" in bad.text
    mismatch = _post(app, _payload("cloud-mismatch", {"repo": "owner/repo", "pr_number": 948},
                                       pr_number=948))
    assert mismatch.status_code == 422
    split = _post(app, _payload("cloud-split", {"repo": "owner/repo", "pr_number": 948},
                                    pr_number=947))
    assert split.status_code == 422
    good = _post(app, _payload("cloud-good", {"repo": "owner/repo", "pr_number": 947},
                                   pr_number=947))
    assert good.status_code == 201, good.text
    monkeypatch.setattr(server, "_task_context_open_pr",
                        lambda branch, repo: (_ for _ in ()).throw(RuntimeError("gh unavailable")))
    unavailable = _post(app, _payload("cloud-unavailable", {"repo": "owner/repo"}))
    assert unavailable.status_code == 422
    assert unavailable.json()["detail"] == "open_pr_lookup_unavailable"


def test_directive_requires_operation_id(tmp_db, monkeypatch):
    monkeypatch.setattr(TaskQueue, "_bind_work_intent_on", lambda *args, **kwargs: None)
    app = _app(tmp_db)
    bad = _post(app, _payload("directive-bad", {"directive": True}))
    assert bad.status_code == 422
    assert "external_operation_id" in bad.text
    good = _post(app, _payload("directive-good", {"directive": True,
                                                  "external_operation_id": "op-941"}))
    assert good.status_code == 201, good.text


def test_internal_context_writers_are_allowlisted():
    """Literal writes to context dictionaries must be reflected in admission."""
    root = Path(__file__).resolve().parents[2] / "src" / "agent_crew"
    written = set()
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                if (isinstance(node.value, ast.Name)
                        and ("ctx" in node.value.id or "context" in node.value.id)
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)):
                    written.add(node.slice.value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"update", "setdefault"}
                    and isinstance(node.func.value, ast.Name)
                    and ("ctx" in node.func.value.id or "context" in node.func.value.id)):
                if node.func.attr == "setdefault" and node.args and isinstance(node.args[0], ast.Constant):
                    written.add(node.args[0].value)
                if (node.func.attr == "update" and node.args
                        and isinstance(node.args[0], ast.Dict)):
                    written.update(key.value for key in node.args[0].keys
                                   if isinstance(key, ast.Constant) and isinstance(key.value, str))
    assert written <= TASK_CONTEXT_KEYS, sorted(written - TASK_CONTEXT_KEYS)
