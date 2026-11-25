"""#1082 slice a: host-load admission and an xdist worker cap for full suites.

No test here really sleeps: the gate's reader, sleep and clock are injected.
"""

import os
import sqlite3
import subprocess

import pytest

from agent_crew import script_tester
from tests.unit.test_issue_772_script_test_stage import (  # noqa: F401 — fixtures
    GREEN, PYTEST, _run_through_push_path, _task, project, server)

pytestmark = pytest.mark.usefixtures("admit_crew_run")  # #1006: subject is not admission


class FakeHost:
    """A load sequence on a fake clock; ``sleep`` only advances the clock."""

    def __init__(self, loads):
        self.loads = list(loads)
        self.now = 1000.0
        self.sleeps = []

    def read(self):
        return self.loads.pop(0) if len(self.loads) > 1 else self.loads[0]

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now


def _gate(host, **kwargs):
    return script_tester.HostLoadGate(read_load=host.read, sleep=host.sleep,
                                      clock=host.clock, **kwargs)


def test_load_under_threshold_starts_at_once():
    host, waits = FakeHost([3.5]), []
    admission = _gate(host, on_wait_start=waits.append).admit()
    assert host.sleeps == [] and waits == []
    assert admission["load_contended"] is False
    assert admission["readings"] == [{"load_5m": 3.5, "waited_s": 0.0}]


def test_load_over_threshold_waits_then_starts_when_it_drops():
    host, waits, checks = FakeHost([31.0, 25.0, 12.0]), [], []
    admission = _gate(host, on_wait_start=waits.append, on_check=checks.append).admit()
    assert host.sleeps == [120.0, 120.0]
    assert len(waits) == 1          # one event per wait start, not per re-check
    assert waits[0] == {"load_5m": 31.0, "max_load": 20.0, "recheck_s": 120.0,
                        "max_wait_s": 3600.0}
    assert [c["load_5m"] for c in checks] == [31.0, 25.0, 12.0]
    assert admission["load_contended"] is False and admission["waited_s"] == 240.0


def test_still_over_threshold_at_3600s_runs_contended():
    host, waits = FakeHost([40.0]), []
    admission = _gate(host, on_wait_start=waits.append).admit()
    assert sum(host.sleeps) == 3600.0 and max(host.sleeps) == 120.0
    assert len(waits) == 1
    assert admission["load_contended"] is True and admission["waited_s"] == 3600.0
    assert len(admission["readings"]) == 31
    assert all(r["load_5m"] == 40.0 for r in admission["readings"])


def test_unreadable_load_does_not_hold_the_suite():
    host = FakeHost([None])
    admission = _gate(host).admit()
    assert host.sleeps == [] and admission["load_unavailable"] is True


def test_read_load_5m_parses_proc_loadavg(tmp_path, monkeypatch):
    path = tmp_path / "loadavg"
    path.write_text("36.70 32.02 28.39 29/4850 2620244\n")
    monkeypatch.undo()  # conftest replaced the reader; test the real one
    monkeypatch.setattr(script_tester, "_LOADAVG_PATH", str(path))
    assert script_tester.read_load_5m() == 32.02
    path.write_text("garbage")
    assert script_tester.read_load_5m() is None
    monkeypatch.setattr(script_tester, "_LOADAVG_PATH", str(tmp_path / "missing"))
    assert script_tester.read_load_5m() is None


@pytest.mark.parametrize(("command", "expected"), [
    ("python3 -m pytest -q --timeout=180 -p no:cacheprovider --numprocesses=8 tests",
     "python3 -m pytest -q --timeout=180 -p no:cacheprovider --numprocesses=4 tests"),
    ("pytest -n 8 tests", "pytest -n 4 tests"),
    ("pytest -n8 tests", "pytest -n4 tests"),
    ("pytest --numprocesses auto tests", "pytest --numprocesses 4 tests"),
    ("pytest -n logical tests", "pytest -n 4 tests"),
    ("pytest -n 2 tests", "pytest -n 2 tests"),
    ("pytest -q tests", "pytest -q tests"),
    ("make test -n 8", "make test -n 8"),
])
def test_cap_xdist_workers(command, expected):
    assert script_tester.cap_xdist_workers(command, 4) == expected


def test_env_overrides_and_invalid_env_falls_back(monkeypatch):
    assert (script_tester.max_load(), script_tester.max_workers()) == (20.0, 4)
    monkeypatch.setenv(script_tester.MAX_LOAD_ENV, "8.5")
    monkeypatch.setenv(script_tester.WORKERS_ENV, "2")
    assert (script_tester.max_load(), script_tester.max_workers()) == (8.5, 2)
    assert script_tester.HostLoadGate.from_env().max_load == 8.5
    for bad in ("", "abc", "0", "-3", "inf", "2.5"):
        monkeypatch.setenv(script_tester.MAX_LOAD_ENV, bad)
        monkeypatch.setenv(script_tester.WORKERS_ENV, bad)
        expected_load = 2.5 if bad == "2.5" else 20.0
        assert script_tester.max_load() == expected_load
        assert script_tester.max_workers() == 4


def _recording_runner(calls):
    def runner(command, cwd, timeout_s, *, env=None):
        calls.append(command)
        return script_tester.RunOutcome(exit_code=0, counts={"passed": 1})
    return runner


def test_full_suite_runs_with_capped_workers_and_gate(project, tmp_path, monkeypatch):
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    monkeypatch.setenv(script_tester.WORKERS_ENV, "3")
    calls, host = [], FakeHost([30.0, 5.0])
    result = script_tester.execute_test_task(
        _task(head), repo=repo, state={"test_command": f"{PYTEST} -n 8"},
        cache_dir=str(tmp_path / "cache"), runner=_recording_runner(calls),
        load_gate=_gate(host))
    record = result.error_info["script_test"]
    assert calls == [f"{PYTEST} -n 3"]
    assert record["suite_command"] == f"{PYTEST} -n 3"
    assert record["command"] == f"{PYTEST} -n 8"
    assert host.sleeps == [120.0]
    assert [a["load_contended"] for a in record["host_load"]] == [False]
    assert "load_contended" not in record


def test_isolated_reruns_keep_the_configured_command(project, tmp_path):
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    node, calls = "tests/test_b.py::test_two", []
    command = f"{PYTEST} --numprocesses=8"

    def runner(cmd, cwd, timeout_s, *, env=None):
        calls.append(cmd)
        if cmd.endswith(node):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        if os.path.exists(os.path.join(cwd, "tests", "test_b.py")):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        return script_tester.RunOutcome(exit_code=0)

    record = script_tester.run_script_test(
        repo, base_sha=_git_rev(repo, "origin/main"), head_sha=head,
        state={"test_command": command}, cache_dir=str(tmp_path / "cache"), runner=runner,
        load_gate=_gate(FakeHost([1.0])))
    capped = f"{PYTEST} --numprocesses=4"
    assert calls[:2] == [capped, capped]                       # head + base full suites
    assert calls[2:] == [f"{command} {node}"] * 2              # reruns unchanged
    assert len(record["host_load"]) == 2


def _git_rev(repo, ref):
    return subprocess.run(["git", "-C", repo, "rev-parse", ref], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_server_records_one_wait_event_heartbeats_and_contention(
        project, server, monkeypatch):
    build, pushes, spawns, merges = server
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    app, queue = build(repo, {"test_command": PYTEST})
    host = FakeHost([45.0])
    monkeypatch.setattr(script_tester, "read_load_5m", host.read)
    from_env = script_tester.HostLoadGate.from_env
    monkeypatch.setattr(script_tester.HostLoadGate, "from_env", classmethod(
        lambda cls, **kw: from_env(sleep=host.sleep, clock=host.clock, **kw)))
    heartbeats = []
    record_heartbeat = type(queue).record_heartbeat

    def spy_heartbeat(self, task_id, *, source, ts=None):
        heartbeats.append(source)
        return record_heartbeat(self, task_id, source=source, ts=ts)

    monkeypatch.setattr(type(queue), "record_heartbeat", spy_heartbeat)
    done = _run_through_push_path(app, queue, _task(head))
    assert done.status == "completed"
    record = queue.get_task_context(done.task_id)["script_test"]
    assert record["load_contended"] is True
    assert record["host_load"][0]["waited_s"] == 3600.0
    assert len(record["host_load"][0]["readings"]) == 31
    assert heartbeats == ["host_busy_wait"] * 31
    with sqlite3.connect(queue._db_path) as conn:
        events = conn.execute(
            "SELECT fields FROM task_exec_events WHERE task_id=? AND event=?",
            (done.task_id, "host_busy_wait")).fetchall()
    assert len(events) == 1 and '"load_5m": 45.0' in events[0][0]
