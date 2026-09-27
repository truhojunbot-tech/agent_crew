"""Fast checks for row 1.5 measurement accounting."""
import json
import sqlite3

from scripts.measure_row15_shadow import capture_completeness, select_tasks


def test_capture_completeness_reports_layers_and_scope_without_test_ids():
    tasks = [{"project": "agent_crew", "task_id": "impl-1", "status": "completed"},
             {"project": "agent_crew", "task_id": "impl-2", "status": "failed"}]
    scope = {"project": "agent_crew", "task_id": "impl-1", "issue": "431",
             "provider_session": "session", "context_generation": 2, "worktree": "/tmp/w"}
    rows = [{"layer": layer, "key": f"task:impl-1:{layer}", "scope": scope}
            for layer in ("episodic", "decision")]
    result = capture_completeness(tasks, rows)
    assert result["terminal_tasks"] == 2
    assert result["complete_tasks"] == 1
    assert result["missing"] == [{"project": "agent_crew", "task_id": "impl-2",
                                  "reason": "missing_capture_layers",
                                  "layers": ["episodic", "decision", "failure_pattern"]}]
    assert all(value == {"filled": 1, "denominator": 1}
               for value in result["scope_fill"].values())


def test_select_tasks_requires_claim_build_and_excludes_fixture_ids():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE tasks (task_id TEXT, project TEXT, status TEXT, description TEXT, "
               "context TEXT, claim_build_commit TEXT, result_posted_at REAL)")
    for task_id, build, status in (("impl-real", "a138976abc", "completed"),
                                   ("test-local", "a138976abc", "completed"),
                                   ("impl-other", "different", "completed"),
                                   ("impl-active", "a138976abc", "in_progress")):
        db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                   (task_id, "agent_crew", status, "desc", json.dumps({}), build, 1.0))
    assert [task["task_id"] for task in select_tasks(db)] == ["impl-real"]
