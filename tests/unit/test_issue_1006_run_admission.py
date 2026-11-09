"""#1006: crew run re-reads #44 authority before its first implement POST."""

import pytest
from click.testing import CliRunner

from agent_crew import cli
from agent_crew.cli import crew
from agent_crew.protocol import TaskResult
from agent_crew.queue import TaskQueue

FULL_EVIDENCE = {"ssot_revision": "r1", "authority_revision": "a1", "ssot_age_s": 3}


class _Trigger:
    def __init__(self, verdict):
        self.verdict = verdict
        self.candidates = []

    def priority_check(self, candidate):
        self.candidates.append(candidate)
        return self.verdict


def _run(monkeypatch, tmp_path, trigger, *extra):
    db = tmp_path / "tasks.db"
    monkeypatch.setattr(cli, "_load_admission_trigger", lambda: trigger)
    monkeypatch.setattr(
        TaskQueue, "get_result",
        lambda _queue, task_id: TaskResult(
            task_id=task_id, status="failed", summary="fixture stops after enqueue"),
    )
    result = CliRunner().invoke(
        crew, ["run", "implement issue work", "--db", str(db), "--risk-tier", "1",
               "--branch", "fix/issue", "--no-tester", *extra])
    tasks = TaskQueue(str(db)).list_tasks() if db.exists() else []
    return result, tasks


ISSUE = ("--repo", "o/r", "--issue", "44")


@pytest.mark.parametrize("verdict, code", [
    ({"decision": "DENY", "evidence": {"reason": "P0-only hold"}}, "ADMIT_DENY: P0-only hold"),
    ({"decision": "PARK", "evidence": {"reason": "parked"}}, "ADMIT_PARK: parked"),
    ({"decision": "REFRESH_REQUIRED", "evidence": {"reason": "stale"}},
     "ADMIT_REFRESH_REQUIRED: stale"),
    ({"decision": "ALLOW", "evidence": {"authority_revision": "a1", "ssot_age_s": 3}},
     "ADMIT_REFRESH_REQUIRED"),
    ({"decision": "ALLOW", "evidence": {"ssot_revision": "r1", "ssot_age_s": 3}},
     "ADMIT_REFRESH_REQUIRED"),
    ({"decision": "ALLOW", "evidence": {"ssot_revision": "r1", "authority_revision": "a1"}},
     "ADMIT_REFRESH_REQUIRED"),
    ({"decision": "NOT_EXERCISED", "evidence": dict(FULL_EVIDENCE)}, "ADMIT_REFRESH_REQUIRED"),
    ({"decision": "ALLOW"}, "ADMIT_REFRESH_REQUIRED"),
    (None, "ADMIT_REFRESH_REQUIRED"),
])
def test_refused_verdict_enqueues_nothing(monkeypatch, tmp_path, verdict, code):
    result, tasks = _run(monkeypatch, tmp_path, _Trigger(verdict), *ISSUE)

    assert result.exit_code != 0
    assert code in result.output
    assert tasks == []


def test_trigger_import_failure_refuses(monkeypatch, tmp_path):
    result, tasks = _run(monkeypatch, tmp_path, None, *ISSUE)

    assert result.exit_code != 0
    assert "ADMIT_REFRESH_REQUIRED" in result.output
    assert tasks == []


def test_trigger_exception_refuses(monkeypatch, tmp_path):
    class Boom:
        def priority_check(self, candidate):
            raise RuntimeError("ssot unreadable")

    result, tasks = _run(monkeypatch, tmp_path, Boom(), *ISSUE)

    assert result.exit_code != 0
    assert "ADMIT_REFRESH_REQUIRED" in result.output
    assert tasks == []


def test_missing_issue_refuses(monkeypatch, tmp_path):
    trigger = _Trigger({"decision": "ALLOW", "evidence": dict(FULL_EVIDENCE)})
    result, tasks = _run(monkeypatch, tmp_path, trigger, "--repo", "o/r")

    assert result.exit_code != 0
    assert "ADMIT_REFRESH_REQUIRED" in result.output
    assert tasks == []
    assert trigger.candidates == []


def test_full_allow_enqueues_exactly_one_implement(monkeypatch, tmp_path):
    trigger = _Trigger({"decision": "ALLOW", "evidence": dict(FULL_EVIDENCE)})
    result, tasks = _run(monkeypatch, tmp_path, trigger, *ISSUE)

    assert result.exit_code == 0, result.output
    assert [t.task_type for t in tasks] == ["implement"]
    assert trigger.candidates == [{"repo": "o/r", "issue": 44, "scoped_target": True,
                                   "new_implementation": True, "task_type": "implement"}]


def test_loader_missing_file_and_broken_file_return_none(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CREW_ADMISSION_TRIGGER", str(tmp_path / "absent.py"))
    assert cli._load_admission_trigger() is None

    broken = tmp_path / "trigger.py"
    broken.write_text("import no_such_module_for_1006\n")
    monkeypatch.setenv("AGENT_CREW_ADMISSION_TRIGGER", str(broken))
    assert cli._load_admission_trigger() is None
    assert str(tmp_path) not in __import__("sys").path


def test_loader_resolves_sibling_imports(monkeypatch, tmp_path):
    (tmp_path / "sibling_1006.py").write_text("VALUE = 'ok'\n")
    trig = tmp_path / "trigger.py"
    trig.write_text("import sibling_1006\n"
                    "def priority_check(candidate):\n"
                    "    return sibling_1006.VALUE\n")
    monkeypatch.setenv("AGENT_CREW_ADMISSION_TRIGGER", str(trig))

    module = cli._load_admission_trigger()

    assert module.priority_check({}) == "ok"
