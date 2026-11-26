"""Host-wide flock admission, with real locks and a fake clock."""
import fcntl
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_crew import script_tester
from agent_crew.host_suite import host_suite_slot
from scripts import premerge_gate


@pytest.fixture(autouse=True)
def slot_path(tmp_path, monkeypatch):
    path = tmp_path / "host.lock"
    monkeypatch.setenv("AGENT_CREW_HOST_SUITE_LOCK", str(path))
    return path


class Clock:
    now = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_second_suite_waits_for_first(slot_path):
    clock = Clock()
    first = host_suite_slot(kind="premerge_gate", port=8105)
    first.__enter__()
    def release(seconds):
        clock.sleep(seconds)
        first.__exit__(None, None, None)
    with host_suite_slot(kind="script_test", port=8106,
                         clock=clock.clock, sleep=release) as receipt:
        assert receipt == {"queue_wait_s": 120.0, "host_suite_contended": False,
                           "holder": {"pid": os.getpid(), "port": 8105,
                                      "kind": "premerge_gate"}}
        assert json.loads(slot_path.read_text())["port"] == 8106


def test_deadline_launch_preserves_holder_and_lock(slot_path):
    clock = Clock()
    with host_suite_slot(kind="script_test", port=8105):
        holder = slot_path.read_text()
        with host_suite_slot(kind="premerge_gate", clock=clock.clock,
                             sleep=clock.sleep) as receipt:
            assert receipt["queue_wait_s"] == 3600
            assert receipt["host_suite_contended"] is True
        assert slot_path.read_text() == holder
        with slot_path.open() as handle, pytest.raises(BlockingIOError):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_exception_releases_slot():
    with pytest.raises(RuntimeError), host_suite_slot(kind="script_test"):
        raise RuntimeError("launch failed")
    with host_suite_slot(kind="premerge_gate") as receipt:
        assert receipt["queue_wait_s"] == 0
        assert not receipt["host_suite_contended"]


@pytest.mark.parametrize("with_record", [False, True])
def test_slot_contended_base_is_not_cached(tmp_path, monkeypatch, with_record):
    clock = Clock()
    runs = []
    record = {} if with_record else None

    @contextmanager
    def checkout(*args):
        yield str(tmp_path)

    monkeypatch.setattr(script_tester, "Checkout", checkout)

    def runner(*args, **kwargs):
        runs.append(clock.now)
        return script_tester.RunOutcome(exit_code=1, failing_ids=["test_a.py::test_a"])

    gate = script_tester.HostLoadGate(read_load=lambda: 0,
                                      clock=clock.clock, sleep=clock.sleep)
    args = (str(tmp_path), "base", "python3 -m pytest -q", 60,
            str(tmp_path / "cache"), {})
    with host_suite_slot(kind="premerge_gate", port=8105):
        outcome, cached = script_tester.base_outcome(
            *args, runner=runner, load_gate=gate, record=record)
        assert not cached and outcome.failing_ids == ["test_a.py::test_a"]
        assert runs == [3600]
        if record is not None:
            assert record["host_suite_contended"] is True
            assert record["host_load"][0]["load_contended"] is False
        assert not list((tmp_path / "cache").glob("*.json"))

    # With the holder gone, the same base must run again and can then be cached.
    _, cached = script_tester.base_outcome(
        *args, runner=runner, load_gate=gate, record=record)
    assert not cached and runs == [3600, 3600]
    _, cached = script_tester.base_outcome(
        *args, runner=runner, load_gate=gate, record=record)
    assert cached and len(runs) == 2


def test_head_slot_contention_does_not_prevent_clean_base_cache(tmp_path, monkeypatch):
    clock = Clock()
    gate = script_tester.HostLoadGate(read_load=lambda: 0,
                                      clock=clock.clock, sleep=clock.sleep)
    record = {}
    with host_suite_slot(kind="premerge_gate"):
        with script_tester._full_suite_slot(gate, record):
            pass

    @contextmanager
    def checkout(*args):
        yield str(tmp_path)

    monkeypatch.setattr(script_tester, "Checkout", checkout)
    runs = []

    def runner(*args, **kwargs):
        runs.append(True)
        return script_tester.RunOutcome(exit_code=0)

    args = (str(tmp_path), "base", "python3 -m pytest -q", 60,
            str(tmp_path / "cache"), {})
    _, cached = script_tester.base_outcome(
        *args, runner=runner, load_gate=gate, record=record)
    assert not cached and record["host_suite_contended"] is True
    assert record["host_suite"][-1]["host_suite_contended"] is False
    _, cached = script_tester.base_outcome(
        *args, runner=runner, load_gate=gate, record=record)
    assert cached and len(runs) == 1


def test_head_base_take_slot_after_load_rerun_and_cached_base_do_not(tmp_path, monkeypatch, slot_path):
    clock = Clock()
    events = []
    @contextmanager
    def checkout(*args):
        yield str(tmp_path)
    monkeypatch.setattr(script_tester, "Checkout", checkout)
    monkeypatch.setattr(script_tester, "cap_xdist_workers",
                        lambda command, env, cap, **kw: (command, env, {}))
    monkeypatch.setattr(script_tester, "worker_cap_receipt", lambda *a: {"observed_workers": None})
    def runner(*args, **kwargs):
        events.append("run")
        with slot_path.open() as handle:
            if len(events) in (2, 4):
                with pytest.raises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if len(events) == 2:
            return script_tester.RunOutcome(exit_code=1, failing_ids=["tests/test_a.py::test_a"])
        return script_tester.RunOutcome(exit_code=0)
    gate = script_tester.HostLoadGate(read_load=lambda: events.append("load") or 0,
                                      clock=clock.clock, sleep=clock.sleep)
    record = script_tester.run_script_test(str(tmp_path), base_sha="base", head_sha="head",
        state={"test_command": "python3 -m pytest -q"}, cache_dir=str(tmp_path / "cache"),
        runner=runner, load_gate=gate)
    assert events == ["load", "run", "load", "run", "run"]
    assert len(record["host_suite"]) == 2
    assert record["queue_wait_s"] == 0
    assert record["host_suite_contended"] is False
    _, cached = script_tester.base_outcome(str(tmp_path), "base", "python3 -m pytest -q",
        record["timeout_s"], str(tmp_path / "cache"), {"test_command": "python3 -m pytest -q"},
        runner=runner, load_gate=gate, worker_cap=4)
    assert cached and len(events) == 5


def test_premerge_uses_same_slot(tmp_path, monkeypatch, slot_path, capsys):
    monkeypatch.setattr(premerge_gate, "BASETEMP_ROOT", tmp_path)
    def run(args, **kwargs):
        if "pytest" in args:
            assert json.loads(slot_path.read_text())["kind"] == "premerge_gate"
            with slot_path.open() as handle, pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return SimpleNamespace(returncode=0, stdout="1 passed", stderr="")
    monkeypatch.setattr(premerge_gate, "run", run)
    record = {}
    assert premerge_gate.test_pr(7, "head", "base", tmp_path, record) == ("1 passed", 0)
    body = premerge_gate.gate_comment("head", "base", "1 passed", 0, record)
    assert premerge_gate.gate_payload(7, body)["host_suite"] == record["host_suite"]
    assert '"queue_wait_s": 0' in capsys.readouterr().out


def test_script_receipt_uses_load_gate_clock_and_keeps_heartbeats():
    clock = Clock()
    checks = []
    gate = script_tester.HostLoadGate(clock=clock.clock, sleep=clock.sleep,
                                      on_check=checks.append)
    record = {}
    with host_suite_slot(kind="premerge_gate", port=8105):
        with script_tester._full_suite_slot(gate, record):
            assert record["host_suite_contended"] is True
            assert record["queue_wait_s"] == 3600
            assert record["host_suite"][0]["holder"]["port"] == 8105
    assert len(checks) == 31
    assert checks[-1]["queue_wait_s"] == 3600
