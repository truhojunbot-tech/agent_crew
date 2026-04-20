"""
E2E tests for crew triage / poll CLI — scheduled issue selection.

E-TR01: crew triage --repo mock/repo — stub gh CLI, issue selected, gate created
E-TR02: crew triage --no-confirm — skips gate, enqueues implement task immediately
E-TR03: crew poll --interval 1s --cycles 2 — two triage cycles observed
E-TR04: poll --interval parsing — 10s / 2m / 1h parsed to correct seconds
"""

import json
import subprocess

import pytest
from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.queue import TaskQueue

pytestmark = pytest.mark.e2e

_GH_ISSUES = [
    {"number": 42, "title": "Add OAuth login", "labels": [{"name": "enhancement"}]},
    {"number": 43, "title": "Fix memory leak", "labels": [{"name": "bug"}]},
]

_GH_ISSUES_JSON = json.dumps(_GH_ISSUES)


class _FakeProcess:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.returncode = 0


def _gh_mock(issues_json: str = _GH_ISSUES_JSON):
    """Return a subprocess.run replacement that emits fake gh CLI output."""
    def _run(*args, **kwargs):
        return _FakeProcess(issues_json)
    return _run


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "tasks.db")


# E-TR01: crew triage → gate created, issue selected
def test_e_tr01_triage_creates_gate(monkeypatch, db_path):
    monkeypatch.setattr(subprocess, "run", _gh_mock())

    runner = CliRunner()
    result = runner.invoke(crew, ["triage", "--repo", "mock/repo", "--db", db_path])

    assert result.exit_code == 0, result.output
    assert "gate created" in result.output.lower()
    assert "42" in result.output  # top issue number
    assert "Add OAuth login" in result.output

    queue = TaskQueue(db_path)
    gates = queue.list_gates(status="pending")
    assert len(gates) == 1
    assert gates[0].type == "approval"
    assert "42" in gates[0].message

    # No implement task yet (pending gate, not confirmed)
    tasks = queue.list_tasks()
    impl_tasks = [t for t in tasks if t.task_type == "implement"]
    assert len(impl_tasks) == 0


# E-TR02: crew triage --no-confirm → gate skipped, implement task enqueued immediately
def test_e_tr02_no_confirm_enqueues_task(monkeypatch, db_path):
    monkeypatch.setattr(subprocess, "run", _gh_mock())

    runner = CliRunner()
    result = runner.invoke(crew, ["triage", "--repo", "mock/repo", "--db", db_path, "--no-confirm"])

    assert result.exit_code == 0, result.output
    assert "task enqueued" in result.output.lower()

    queue = TaskQueue(db_path)

    # No gate created
    gates = queue.list_gates()
    assert len(gates) == 0

    # Implement task enqueued
    tasks = queue.list_tasks()
    impl_tasks = [t for t in tasks if t.task_type == "implement"]
    assert len(impl_tasks) == 1
    assert "add oauth login" in impl_tasks[0].description.lower()


# E-TR03: crew poll --cycles 2 --interval 1s → exactly 2 triage cycles observed
def test_e_tr03_poll_two_cycles(monkeypatch, db_path):
    monkeypatch.setattr(subprocess, "run", _gh_mock())

    runner = CliRunner()
    result = runner.invoke(
        crew,
        ["poll", "--repo", "mock/repo", "--db", db_path, "--interval", "1s", "--cycles", "2"],
    )

    assert result.exit_code == 0, result.output
    assert "[cycle 1]" in result.output
    assert "[cycle 2]" in result.output

    # Two triage runs → two gates created
    queue = TaskQueue(db_path)
    gates = queue.list_gates()
    assert len(gates) == 2


# E-TR04: interval string parsing — 10s / 2m / 1h → correct sleep seconds
@pytest.mark.parametrize("interval,expected_secs", [
    ("10s", 10),
    ("2m", 120),
    ("1h", 3600),
])
def test_e_tr04_poll_interval_parsing(monkeypatch, db_path, interval, expected_secs):
    monkeypatch.setattr(subprocess, "run", _gh_mock())

    slept: list[float] = []

    import time as _time_mod

    def _fake_sleep(secs: float):
        slept.append(secs)

    monkeypatch.setattr(_time_mod, "sleep", _fake_sleep)

    runner = CliRunner()
    # --cycles 2 so sleep is called once between cycle 1 and 2
    result = runner.invoke(
        crew,
        ["poll", "--repo", "mock/repo", "--db", db_path, "--interval", interval, "--cycles", "2"],
    )

    assert result.exit_code == 0, result.output
    assert "[cycle 1]" in result.output
    assert "[cycle 2]" in result.output
    # sleep must have been called exactly once with the correctly parsed seconds
    assert slept == [expected_secs]
