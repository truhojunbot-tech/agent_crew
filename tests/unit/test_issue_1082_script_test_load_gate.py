"""#1082 slice a: host-load admission and an xdist worker cap for full suites.

No test here really sleeps: the gate's reader, sleep and clock are injected.
"""

import os
import sqlite3
import subprocess
import sys

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
    probed = []
    monkeypatch.setattr(script_tester, "_target_has_xdist",
                        lambda interpreter, env, cwd: probed.append(interpreter) or True)
    return probed


_CMD = f"{sys.executable} -m pytest -q"
_SH = f"{_CMD} tests > out.log"          # shell syntax: best-effort PYTEST_ADDOPTS


@pytest.mark.usefixtures("xdist_present")
@pytest.mark.parametrize(("addopts", "command"), [
    (None, _CMD),
    (None, f"{_CMD} -n 8 --maxprocesses=8"),
    ("-n 8 --maxprocesses=8", f"{_CMD} --maxprocesses 8 tests"),
    (None, "pytest -n 8 'tests/a b.py'"),
])
def test_direct_pytest_launch_appends_the_cap_last(addopts, command, tmp_path):
    """Scope decision on #1082: a direct pytest argv is bounded at launch."""
    (tmp_path / "pytest").write_text(f"#!{sys.executable}\n")
    (tmp_path / "pytest").chmod(0o755)
    env = {"PATH": f"{tmp_path}:/bin", **({"PYTEST_ADDOPTS": addopts} if addopts else {})}
    launched, capped, evidence = script_tester.cap_xdist_workers(command, env, 4, cwd="/")
    assert launched == f"{command} --maxprocesses=4" and capped == env
    assert evidence == {"cap_requested": 4, "mechanism": "argv --maxprocesses",
                        "injection": "appended"}


@pytest.mark.usefixtures("xdist_present")
@pytest.mark.parametrize("command", [
    f"{_CMD} tests > out.log", f"{_CMD} a && {_CMD} b", f"{_CMD} tests | tee log",
    f"{_CMD} tests # note", f"{_CMD} tests; echo done", f"{_CMD} -- tests",
    f"(cd sub && {_CMD})", f"X=1 {_CMD}", f"{_CMD} 'a;b'",
])
def test_shell_commands_are_not_rewritten(command):
    launched, _, evidence = script_tester.cap_xdist_workers(command, {"PATH": "/bin"}, 4,
                                                            cwd="/")
    assert launched == command and evidence["injection"] != "appended"


@pytest.mark.usefixtures("xdist_present")
@pytest.mark.parametrize(("addopts", "command", "expected", "injection"), [
    (None, _SH, "--maxprocesses=4", "injected"),
    ("-n 8 -q", _SH, "-n 8 -q --maxprocesses=4", "injected"),
    ("--maxprocesses=8", _SH, "--maxprocesses=8 --maxprocesses=4", "injected"),
    ("--maxprocesses 2", _SH, "--maxprocesses 2", "already_configured"),
    ("--maxprocesses=4", _SH, "--maxprocesses=4", "already_configured"),
    # Duplicates: the last one is effective, not any low one (review of #1083).
    ("--maxprocesses=2 --maxprocesses=8 -n 8", _SH,
     "--maxprocesses=2 --maxprocesses=8 -n 8 --maxprocesses=4", "injected"),
    ("--maxprocesses=8 --maxprocesses=2", _SH, "--maxprocesses=8 --maxprocesses=2",
     "already_configured"),
    # A command value at or under the cap stays within it after ours.
    (None, f"{_CMD} -n 8 --maxprocesses=3 > out.log", "--maxprocesses=4", "injected"),
])
def test_cap_goes_into_pytest_addopts(addopts, command, expected, injection):
    env = {"PATH": "/bin"} if addopts is None else {"PATH": "/bin", "PYTEST_ADDOPTS": addopts}
    launched, capped, evidence = script_tester.cap_xdist_workers(command, env, 4, cwd="/")
    assert launched == command
    assert capped["PYTEST_ADDOPTS"] == expected and capped["PATH"] == "/bin"
    assert evidence["injection"] == injection and evidence["cap_requested"] == 4
    assert env.get("PYTEST_ADDOPTS") == addopts           # the input is not mutated


@pytest.mark.usefixtures("xdist_present")
@pytest.mark.parametrize(("command", "env", "reason"), [
    # pytest reads the command after PYTEST_ADDOPTS, so a larger value wins.
    (f"{_CMD} -n 8 --maxprocesses=8 > out.log", {}, "command_overrides_cap"),
    (f"{_CMD} -n 8 --maxprocesses 3 --maxprocesses 8 > out.log", {},
     "command_overrides_cap"),
    (f"{_CMD} --maxprocesses=$N", {}, "command_expands_at_runtime"),
    ("make test", {}, "pytest_launcher_unresolved"),
    ("missing-python3 -m pytest", {}, "pytest_launcher_unresolved"),
    (f"{_CMD} -p no:xdist", {}, "xdist_disabled"),
    (_CMD, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, "xdist_disabled"),
    (f"{_CMD} -k 'unterminated", {}, "unparseable"),
    (_CMD, {"PYTEST_ADDOPTS": "-k 'unterminated"}, "unparseable"),
])
def test_unresolvable_invocations_skip_the_injection(command, env, reason):
    env = {"PATH": "/bin", **env}
    launched, capped, evidence = script_tester.cap_xdist_workers(command, env, 4, cwd="/")
    assert (launched, capped) == (command, env)                                  # nothing injected
    assert evidence == {"cap_requested": 4, "mechanism": "PYTEST_ADDOPTS --maxprocesses",
                        "injection": "skipped", "reason": reason}


def test_xdist_is_probed_in_the_target_interpreter_not_the_crew(tmp_path):
    """The crew has xdist; a venv pytest without it must not get the option."""
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    bare = venv / "python3"           # -I -S: no user site, so no xdist
    bare.write_text('#!/bin/sh\nexec %s -I -S "$@"\n' % sys.executable)
    bare.chmod(0o755)
    (venv / "pytest").write_text(f"#!{bare}\nimport pytest\n")
    (venv / "py.test").write_text("#!/bin/sh\nexec python3 -m pytest\n")
    for launcher in ("pytest", "py.test"):
        (venv / launcher).chmod(0o755)
    env = {"PATH": f"{venv}:/bin"}
    for command, reason in [(f"{bare} -m pytest tests", "xdist_unavailable"),
                            ("pytest tests", "xdist_unavailable"),
                            ("venv/bin/pytest tests", "xdist_unavailable"),
                            (f"{_CMD} unit && pytest e2e", "xdist_unavailable"),
                            ("venv/bin/py.test", "pytest_launcher_unresolved")]:
        _, capped, evidence = script_tester.cap_xdist_workers(command, env, 4,
                                                              cwd=str(tmp_path))
        assert (evidence["injection"], evidence["reason"]) == ("skipped", reason), command
        assert capped == env
    assert script_tester._target_has_xdist(str(tmp_path / "nope"), env, str(tmp_path)) is False


def test_every_pytest_launcher_in_the_command_is_probed(xdist_present, tmp_path):
    launcher = tmp_path / "pytest"
    launcher.write_text("#!/usr/bin/env python3\n")
    script_tester.cap_xdist_workers(f"{_CMD} unit && ./pytest e2e", {"PATH": "/usr/bin:/bin"},
                                    4, cwd=str(tmp_path))
    assert xdist_present[0] == sys.executable and len(xdist_present) == 2
    assert os.path.basename(xdist_present[1]).startswith("python3")


_WORKER_COUNT_TEST = """import os

def test_record_workers():
    with open(os.environ["WORKERS_OUT"], "w") as f:
        f.write(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "serial"))
"""


@pytest.mark.skipif(not script_tester._target_has_xdist(sys.executable, dict(os.environ), "."),
                    reason="needs pytest-xdist")
@pytest.mark.parametrize("implicit", ["ini", "env", "ini_cap"])
def test_real_shell_command_keeps_its_redirect_and_implicit_workers_are_capped(
        tmp_path, implicit):
    """A redirecting shell command, with ``-n 8`` from ini addopts or from a
    configured PYTEST_ADDOPTS (not the command), runs with at most 2 workers.
    Its output went to the file, so the observed worker count stays unknown."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_w.py").write_text(_WORKER_COUNT_TEST)
    out = tmp_path / "workers.txt"
    test_env = {"WORKERS_OUT": str(out)}
    if implicit == "ini":
        (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -n 8\n")
    elif implicit == "ini_cap":        # a looser ini cap is read before ours
        (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -n 8 --maxprocesses=8\n")
    else:
        test_env["PYTEST_ADDOPTS"] = "-n 8"
    command = "python3 -m pytest -p no:cacheprovider tests > result.log 2>&1 && echo shell-ok"
    _, env, evidence = script_tester.cap_xdist_workers(
        command, script_tester.test_environment(str(tmp_path), {"test_env": test_env}), 2,
        cwd=str(tmp_path))
    assert evidence["injection"] == "injected"
    outcome = script_tester.run_command(command, str(tmp_path), 120, env=env)
    assert outcome.exit_code == 0, outcome.output_tail
    assert "shell-ok" in outcome.output_tail                 # && still chains
    assert "1 passed" in (tmp_path / "result.log").read_text()  # > still redirects
    assert out.read_text() == "2"
    receipt = script_tester.worker_cap_receipt(evidence, command, outcome)
    assert (receipt["cap_requested"], receipt["observed_workers"]) == (2, None)


@pytest.mark.skipif(not script_tester._target_has_xdist(sys.executable, dict(os.environ), "."),
                    reason="needs pytest-xdist")
def test_real_hidden_parallel_invocation_leaves_observed_workers_unknown(tmp_path):
    """Review of #1083: an 8-worker run redirected to a log, then a serial one.
    The visible output is a plain serial summary; that proves nothing."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_w.py").write_text(_WORKER_COUNT_TEST)
    out = tmp_path / "workers.txt"
    command = ("PYTEST_ADDOPTS='-n 8' python3 -m pytest -p no:cacheprovider tests"
               " > hidden.log 2>&1; python3 -m pytest -p no:cacheprovider -p no:xdist tests")
    _, env, evidence = script_tester.cap_xdist_workers(
        command, script_tester.test_environment(
            str(tmp_path), {"test_env": {"WORKERS_OUT": str(out)}}), 2, cwd=str(tmp_path))
    outcome = script_tester.run_command(command, str(tmp_path), 120, env=env)
    assert outcome.exit_code == 0 and "1 passed" in outcome.output_tail, outcome.output_tail
    assert "8 workers" in (tmp_path / "hidden.log").read_text()   # the hidden run had 8
    receipt = script_tester.worker_cap_receipt(evidence, command, outcome)
    assert receipt["observed_workers"] is None
    assert "status" not in receipt and not any("enforced" in k for k in receipt)


@pytest.mark.skipif(not script_tester._target_has_xdist(sys.executable, dict(os.environ), "."),
                    reason="needs pytest-xdist")
def test_real_cli_cap_after_addopts_is_bounded(tmp_path):
    """A direct launch with its own --maxprocesses=8 (and -n 8 in PYTEST_ADDOPTS)
    gets --maxprocesses=4 last, and really runs with 4 workers (review of #1083)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_w.py").write_text(_WORKER_COUNT_TEST)
    out = tmp_path / "workers.txt"
    command = "python3 -m pytest -p no:cacheprovider -n 8 --maxprocesses=8 tests"
    base = script_tester.test_environment(
        str(tmp_path), {"test_env": {"WORKERS_OUT": str(out), "PYTEST_ADDOPTS": "-n 8"}})
    launched, env, evidence = script_tester.cap_xdist_workers(command, base, 4,
                                                              cwd=str(tmp_path))
    assert launched.endswith(" --maxprocesses=4") and evidence["injection"] == "appended"
    outcome = script_tester.run_command(launched, str(tmp_path), 120, env=env)
    assert outcome.exit_code == 0, outcome.output_tail
    assert out.read_text() == "4"
    receipt = script_tester.worker_cap_receipt(evidence, launched, outcome)
    assert receipt["observed_workers"] == 4, outcome.output_tail


def _reported(output, exit_code=0):
    """A fake run that printed ``output``, parsed as run_command parses it."""
    counts, ids = script_tester.parse_test_output(output)
    return script_tester.RunOutcome(
        exit_code=exit_code, counts=counts, failing_ids=ids, output_tail=output,
        observed_workers=script_tester.observed_xdist_workers(output))


_XDIST_8 = "created: 8/8 workers\n8 workers [12 items]\n\n....\n12 passed in 3.1s\n"
_XDIST_2 = "created: 2/2 workers\n2 workers [12 items]\n\n....\n12 passed in 3.1s\n"


@pytest.mark.usefixtures("xdist_present")
@pytest.mark.parametrize(("command", "output", "observed"), [
    # A shell-local assignment overrides the injected env; the line shows it.
    (f"PYTEST_ADDOPTS='-n 8 --maxprocesses=8' {_CMD} tests", _XDIST_8, 8),
    (f"{_CMD} tests", "created: 4/4 workers\n12 passed in 3.1s\n", 4),
    (f"{_CMD} tests", _XDIST_2, 2),
    (f"{_CMD} tests", "....\n12 passed in 3.1s\n", None),     # no banner: unknown, not 1
    (f"{_CMD} tests", "bringing up nodes...\n12 passed in 3.1s\n", None),
    (f"{_CMD} tests > out.log", "", None),                       # redirected
    # Two invocations: the visible line may belong to either one.
    (f"{_CMD} unit > unit.log; {_CMD} e2e", _XDIST_2, None),
    (f"{_CMD} unit && {_CMD} e2e", _XDIST_2, None),
    ("make test", _XDIST_2, None),                               # no visible pytest
])
def test_observed_workers_only_from_an_explicit_line_of_one_invocation(
        command, output, observed):
    launched, _, injection = script_tester.cap_xdist_workers(command, {"PATH": "/bin"}, 4,
                                                             cwd="/")
    receipt = script_tester.worker_cap_receipt(injection, launched, _reported(output))
    assert (receipt["cap_requested"], receipt["observed_workers"]) == (4, observed)
    assert "status" not in receipt


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


def _recording_env_runner(calls, output=_XDIST_2):
    def runner(command, cwd, timeout_s, *, env=None):
        calls.append((command, (env or {}).get("PYTEST_ADDOPTS")))
        return _reported(output)
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
    assert record["worker_cap"] == {
        "cap_requested": 3, "mechanism": "PYTEST_ADDOPTS --maxprocesses",
        "injection": "injected", "observed_workers": 2}
    assert record["command"] == f"{PYTEST} -n 8 > out.log"
    assert host.sleeps == [120.0]
    assert [a["load_contended"] for a in record["host_load"]] == [False]
    assert "load_contended" not in record


def _rerun_runner(calls, node, base_workers):
    def runner(cmd, cwd, timeout_s, *, env=None):
        calls.append((cmd, (env or {}).get("PYTEST_ADDOPTS")))
        if cmd.endswith(node):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        if os.path.exists(os.path.join(cwd, "tests", "test_b.py")):
            return _reported(f"created: 4/4 workers\nFAILED {node}\n1 failed in 1s\n", 1)
        return _reported(f"created: {base_workers}/{base_workers} workers\n1 passed in 1s\n")
    return runner


@pytest.mark.usefixtures("xdist_present")
def test_isolated_reruns_keep_the_configured_command(project, tmp_path):
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    node, calls = "tests/test_b.py::test_two", []
    command, cache = f"{PYTEST} --numprocesses=8", str(tmp_path / "cache")
    base_sha, state = _git_rev(repo, "origin/main"), {"test_command": command}
    runner = _rerun_runner(calls, node, 3)
    record = script_tester.run_script_test(
        repo, base_sha=base_sha, head_sha=head, state=state, cache_dir=cache, runner=runner,
        load_gate=_gate(FakeHost([1.0])))
    capped = (f"{command} --maxprocesses=4", None)             # bounded at launch
    assert calls[:2] == [capped, capped]                       # head + base full suites
    assert calls[2:] == [(f"{command} {node}", None)] * 2      # reruns unchanged
    assert record["suite_command"] == capped[0] and len(record["host_load"]) == 2
    assert record["worker_cap"]["observed_workers"] == 4
    assert record["base"]["worker_cap"]["observed_workers"] == 3
    assert record["outcome"] == script_tester.FAILED and record["new_failures"] == [node]
    # Keyed by the observed 3 workers: reused under cap 4, never under cap 2.
    cached, was_cached = script_tester.base_outcome(
        repo, base_sha, command, record["timeout_s"], cache, state, runner, worker_cap=4)
    assert was_cached and cached.worker_cap == record["base"]["worker_cap"]
    _, was_cached = script_tester.base_outcome(
        repo, base_sha, command, record["timeout_s"], cache, state, runner, worker_cap=2)
    assert not was_cached


@pytest.mark.usefixtures("xdist_present")
def test_base_with_more_workers_than_the_cap_is_not_used_for_parity(project, tmp_path):
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    node, calls = "tests/test_b.py::test_two", []
    command, cache = f"{PYTEST} --numprocesses=8", str(tmp_path / "cache")
    base_sha, state = _git_rev(repo, "origin/main"), {"test_command": command}
    runner = _rerun_runner(calls, node, 8)
    record = script_tester.run_script_test(
        repo, base_sha=base_sha, head_sha=head, state=state, cache_dir=cache, runner=runner,
        load_gate=_gate(FakeHost([1.0])))
    assert (record["outcome"], record["reason"]) == (
        script_tester.INCOMPLETE, "script_test_base_over_cap")
    assert record["base"]["worker_cap"]["observed_workers"] == 8 and len(calls) == 2
    _, was_cached = script_tester.base_outcome(
        repo, base_sha, command, record["timeout_s"], cache, state, runner, worker_cap=4)
    assert not was_cached                                  # the 8-worker base is not reused


@pytest.mark.parametrize("load, cached_after", [(99.0, False), (1.0, True)])
def test_contended_base_is_not_cached_as_a_clean_baseline(project, tmp_path, load,
                                                          cached_after):
    repo, _ = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    base_sha, cache, state = _git_rev(repo, "origin/main"), str(tmp_path / "cache"), {}
    runs = []

    def runner(cmd, cwd, timeout_s, *, env=None):
        runs.append(cmd)
        return _reported("1 passed in 1s\n")

    record = {}
    gate = _gate(FakeHost([load]), max_load=50.0, max_wait_s=0.0)
    _, was_cached = script_tester.base_outcome(
        repo, base_sha, PYTEST, 60.0, cache, state, runner, load_gate=gate, record=record)
    assert not was_cached and record.get("load_contended", False) is not cached_after
    _, was_cached = script_tester.base_outcome(
        repo, base_sha, PYTEST, 60.0, cache, state, runner, load_gate=_gate(FakeHost([1.0])))
    assert was_cached is cached_after
    assert len(runs) == (1 if cached_after else 2)        # a contended base re-runs


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
