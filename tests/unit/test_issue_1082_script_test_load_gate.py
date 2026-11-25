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


@pytest.fixture
def xdist_present(monkeypatch):
    monkeypatch.setattr(script_tester, "_xdist_available", lambda: True)


@pytest.mark.usefixtures("xdist_present")
@pytest.mark.parametrize(("addopts", "expected", "status"), [
    (None, "--maxprocesses=4", "capped"),
    ("-n 8 -q", "-n 8 -q --maxprocesses=4", "capped"),
    ("--maxprocesses=8", "--maxprocesses=8 --maxprocesses=4", "capped"),
    ("--maxprocesses 2", "--maxprocesses 2", "already_capped"),
    ("--maxprocesses=4", "--maxprocesses=4", "already_capped"),
])
def test_cap_goes_into_pytest_addopts(addopts, expected, status):
    env = {"PATH": "/bin"} if addopts is None else {"PATH": "/bin", "PYTEST_ADDOPTS": addopts}
    capped, evidence = script_tester.cap_xdist_workers(env, 4)
    assert capped["PYTEST_ADDOPTS"] == expected and capped["PATH"] == "/bin"
    assert evidence["status"] == status and evidence["cap"] == 4
    assert env.get("PYTEST_ADDOPTS") == addopts           # the input is not mutated


def test_cap_is_unsupported_without_xdist_or_with_unparseable_addopts(monkeypatch):
    monkeypatch.setattr(script_tester, "_xdist_available", lambda: False)
    env = {"PYTEST_ADDOPTS": "-q"}
    assert script_tester.cap_xdist_workers(env, 4) == (
        env, {"cap": 4, "mechanism": "PYTEST_ADDOPTS --maxprocesses",
              "status": "unsupported", "reason": "xdist_unavailable"})
    monkeypatch.setattr(script_tester, "_xdist_available", lambda: True)
    env = {"PYTEST_ADDOPTS": "-k 'unterminated"}
    capped, evidence = script_tester.cap_xdist_workers(env, 4)
    assert capped == env and evidence["reason"] == "pytest_addopts_unparseable"


_WORKER_COUNT_TEST = """import os

def test_record_workers():
    with open(os.environ["WORKERS_OUT"], "w") as f:
        f.write(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "serial"))
"""


@pytest.mark.skipif(not script_tester._xdist_available(), reason="needs pytest-xdist")
@pytest.mark.parametrize("implicit", ["ini", "env"])
def test_real_shell_command_keeps_its_redirect_and_implicit_workers_are_capped(
        tmp_path, implicit):
    """A redirecting shell command, with ``-n 8`` from ini addopts or from a
    configured PYTEST_ADDOPTS (not the command), runs with at most 2 workers."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_w.py").write_text(_WORKER_COUNT_TEST)
    out = tmp_path / "workers.txt"
    test_env = {"WORKERS_OUT": str(out)}
    if implicit == "ini":
        (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -n 8\n")
    else:
        test_env["PYTEST_ADDOPTS"] = "-n 8"
    env, evidence = script_tester.cap_xdist_workers(
        script_tester.test_environment(str(tmp_path), {"test_env": test_env}), 2)
    assert evidence["status"] == "capped"
    outcome = script_tester.run_command(
        "python3 -m pytest -p no:cacheprovider tests > result.log 2>&1 && echo shell-ok",
        str(tmp_path), 120, env=env)
    assert outcome.exit_code == 0, outcome.output_tail
    assert "shell-ok" in outcome.output_tail                 # && still chains
    assert "1 passed" in (tmp_path / "result.log").read_text()  # > still redirects
    assert out.read_text() == "2"


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


def _recording_env_runner(calls):
    def runner(command, cwd, timeout_s, *, env=None):
        calls.append((command, (env or {}).get("PYTEST_ADDOPTS")))
        return script_tester.RunOutcome(exit_code=0, counts={"passed": 1})
    return runner


@pytest.mark.usefixtures("xdist_present")
def test_full_suite_runs_with_capped_workers_and_gate(project, tmp_path, monkeypatch):
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    monkeypatch.setenv(script_tester.WORKERS_ENV, "3")
    calls, host = [], FakeHost([30.0, 5.0])
    result = script_tester.execute_test_task(
        _task(head), repo=repo, state={"test_command": f"{PYTEST} -n 8 > out.log"},
        cache_dir=str(tmp_path / "cache"), runner=_recording_env_runner(calls),
        load_gate=_gate(host))
    record = result.error_info["script_test"]
    assert calls == [(f"{PYTEST} -n 8 > out.log", "--maxprocesses=3")]
    assert record["worker_cap"]["status"] == "capped" and record["worker_cap"]["cap"] == 3
    assert record["command"] == f"{PYTEST} -n 8 > out.log"
    assert host.sleeps == [120.0]
    assert [a["load_contended"] for a in record["host_load"]] == [False]
    assert "load_contended" not in record


@pytest.mark.usefixtures("xdist_present")
def test_isolated_reruns_keep_the_configured_command(project, tmp_path):
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    node, calls = "tests/test_b.py::test_two", []
    command = f"{PYTEST} --numprocesses=8"

    def runner(cmd, cwd, timeout_s, *, env=None):
        calls.append((cmd, (env or {}).get("PYTEST_ADDOPTS")))
        if cmd.endswith(node):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        if os.path.exists(os.path.join(cwd, "tests", "test_b.py")):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        return script_tester.RunOutcome(exit_code=0)

    record = script_tester.run_script_test(
        repo, base_sha=_git_rev(repo, "origin/main"), head_sha=head,
        state={"test_command": command}, cache_dir=str(tmp_path / "cache"), runner=runner,
        load_gate=_gate(FakeHost([1.0])))
    capped = (command, "--maxprocesses=4")
    assert calls[:2] == [capped, capped]                       # head + base full suites
    assert calls[2:] == [(f"{command} {node}", None)] * 2      # reruns unchanged
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
