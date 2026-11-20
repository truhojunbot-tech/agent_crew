"""#1033 consumer round: gate each round on its provider pool's
``admissions_effective`` (quota-ops#378, c7000023553 / c7008610190 /
c7034905225).

A decision-file OPEN appears only in ``numbers.admissions_effective`` (the
legacy ``admissions_rule`` can be null), so reading only the legacy fields
kept crews on the last valid hold. ``admissions_effective[pool]`` is exactly
``open`` or ``hold``; any other present value parks as HOLD. Absent, the
legacy rule applies unchanged.
"""

import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agent_crew import pool_signal
from agent_crew.protocol import TaskRequest
from tests.unit.test_issue_1033_pool_hold import (  # noqa: F401 (notices: fixture)
    CLAUDE_IMPL, _app, _events, _impl, _signal, _wait, notices)

CODEX_IMPL = {"implementer": "codex", "reviewer": "claude", "tester": "script"}
_ABSENT = object()


def _eff_body(effective=_ABSENT, *, rule=None, action="none", age=0.0):
    numbers = {"admissions_rule": rule, "admissions_rule_reason": "legacy rule"}
    if effective is not _ABSENT:
        numbers["admissions_effective"] = effective
        if isinstance(effective, dict):
            numbers["admissions_effective_source"] = {p: "decision" for p in effective}
            numbers["admissions_effective_reason"] = {
                p: f"{p} effective reason" for p in effective}
    generated = datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat()
    return json.dumps({"generated_at": generated,
                       "balance": {"action": action, "reason": f"balance {action}",
                                   "numbers": numbers}})


def _read(tmp_path, pool="claude"):
    return pool_signal.read_pool_signal(state_dir=str(tmp_path / "crew"), pool=pool)


# ── reader ────────────────────────────────────────────────────────────────

def test_effective_wins_over_legacy_fields(tmp_path, monkeypatch):
    _signal(tmp_path, monkeypatch, raw=_eff_body({"claude": "open", "codex": "hold"},
                                                 rule=None, action="hold_admissions"))
    claude, codex = _read(tmp_path, "claude"), _read(tmp_path, "codex")
    assert claude["rule"] == "open" and "source=decision" in claude["reason"]
    assert "claude effective reason" in claude["reason"]
    assert codex["rule"] == "hold" and "codex effective reason" in codex["reason"]


@pytest.mark.parametrize("effective, needle", [
    ({"claude": "weird", "codex": "open"}, "'weird'"),
    ({"claude": None, "codex": "open"}, "None"),
    ({"claude": 1, "codex": "open"}, "1"),
    ({"claude": "OPEN", "codex": "open"}, "'OPEN'"),
    ({"codex": "open"}, "no 'claude' key"),
    (None, "not an object"),
    ("open", "not an object"),
])
def test_invalid_effective_reads_hold(tmp_path, monkeypatch, effective, needle):
    _signal(tmp_path, monkeypatch, raw=_eff_body(effective, rule="open",
                                                 action="reopen_admissions"))
    signal = _read(tmp_path)
    assert signal["rule"] == "hold"
    assert signal["reason"].startswith(pool_signal.INVALID_EFFECTIVE_REASON)
    assert needle in signal["reason"]
    # A synthetic hold is not a last valid state.
    assert pool_signal.load_last_valid(str(tmp_path / "crew")) is None


def test_effective_absent_is_legacy(tmp_path, monkeypatch):
    path = _signal(tmp_path, monkeypatch, raw=_eff_body(rule="open",
                                                        action="hold_admissions"))
    assert _read(tmp_path)["rule"] == "hold"
    path.write_text(_eff_body(rule="open"))
    assert _read(tmp_path)["rule"] == "open"
    path.write_text(_eff_body(rule=None))
    assert _read(tmp_path)["rule"] == "open"   # null keeps the last valid state


def test_codex_without_effective_is_not_gated(tmp_path, monkeypatch):
    path = _signal(tmp_path, monkeypatch, raw=_eff_body(rule="hold"))
    codex = _read(tmp_path, "codex")
    assert codex["rule"] == "open" and codex.get("ungated")
    path.write_text("{not json")
    assert _read(tmp_path, "codex")["rule"] == "open"
    assert pool_signal.load_last_valid(str(tmp_path / "crew"), "codex") is None


def test_stale_effective_file_is_unknown(tmp_path, monkeypatch):
    _signal(tmp_path, monkeypatch, raw=_eff_body({"claude": "open", "codex": "open"},
                                                 age=61 * 60))
    signal = _read(tmp_path)
    assert signal["rule"] == "unknown" and "stale" in signal["reason"]


def test_last_valid_state_is_per_pool(tmp_path, monkeypatch):
    _signal(tmp_path, monkeypatch, raw=_eff_body({"claude": "open", "codex": "hold"}))
    _read(tmp_path, "claude")
    _read(tmp_path, "codex")
    state = json.loads((tmp_path / "crew" / pool_signal.STATE_FILENAME).read_text())
    assert state["pools"]["claude"]["rule"] == "open"
    assert state["pools"]["codex"]["rule"] == "hold"


def test_old_single_rule_state_file_applies_to_both_pools(tmp_path, monkeypatch):
    crew = tmp_path / "crew"
    crew.mkdir()
    (crew / pool_signal.STATE_FILENAME).write_text(json.dumps(
        {"rule": "hold", "reason": "old shape", "generated_at": "2026-11-19T00:00:00+00:00"}))
    assert pool_signal.load_last_valid(str(crew), "claude")["rule"] == "hold"
    assert pool_signal.load_last_valid(str(crew), "codex")["rule"] == "hold"
    _signal(tmp_path, monkeypatch, raw=_eff_body(rule=None))
    assert _read(tmp_path)["rule"] == "hold"   # legacy null reads the old file
    # A per-pool write keeps the other pool's old-shape state.
    _signal(tmp_path, monkeypatch, raw=_eff_body({"claude": "open", "codex": "open"}))
    assert _read(tmp_path, "claude")["rule"] == "open"
    assert pool_signal.load_last_valid(str(crew), "claude")["rule"] == "open"
    assert pool_signal.load_last_valid(str(crew), "codex")["reason"] == "old shape"


# ── dispatcher gate ───────────────────────────────────────────────────────

def test_claude_open_codex_hold_dispatches_claude_parks_codex(tmp_path, monkeypatch,
                                                              notices):
    _signal(tmp_path, monkeypatch, raw=_eff_body({"claude": "open", "codex": "hold"}))
    (tmp_path / "c").mkdir()
    (tmp_path / "x").mkdir()
    app, queue, dispatched = _app(tmp_path / "c", monkeypatch, CLAUDE_IMPL)
    claude_id = _impl(queue, "impl-claude")
    with TestClient(app):
        assert _wait(lambda: dispatched == [claude_id])
    assert _events(queue, claude_id, "pool_hold_parked") == []

    app, queue, dispatched = _app(tmp_path / "x", monkeypatch, CODEX_IMPL)
    codex_id = _impl(queue, "impl-codex")
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(codex_id) == "waiting_pool")
        time.sleep(0.3)
    assert dispatched == []
    parked = _events(queue, codex_id, "pool_hold_parked")
    assert len(parked) == 1 and parked[0]["rule"] == "hold" and parked[0]["pool"] == "codex"
    assert [n[:2] for n in notices] == [(codex_id, "pool_hold_parked")]


def test_effective_claude_hold_parks_with_one_notice(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, raw=_eff_body({"claude": "hold"}, rule="open",
                                                 action="reopen_admissions"))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.3)
    assert dispatched == []
    parked = _events(queue, task_id, "pool_hold_parked")
    assert len(parked) == 1 and parked[0]["code"] == "POOL_HOLD"
    assert parked[0]["pool"] == "claude"
    assert notices == [(task_id, "pool_hold_parked",
                        "reason=admissions_effective[claude]=hold source=decision: "
                        "claude effective reason")]


@pytest.mark.parametrize("effective", [{"claude": "weird", "codex": "open"},
                                       {"claude": None, "codex": "open"},
                                       {"codex": "open"}])
def test_invalid_effective_parks_as_hold(tmp_path, monkeypatch, notices, effective):
    _signal(tmp_path, monkeypatch, raw=_eff_body(effective, rule="open"))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.3)
    assert dispatched == []
    parked = _events(queue, task_id, "pool_hold_parked")
    assert len(parked) == 1 and parked[0]["rule"] == "hold"
    assert parked[0]["reason"].startswith(pool_signal.INVALID_EFFECTIVE_REASON)
    assert [n[:2] for n in notices] == [(task_id, "pool_hold_parked")]


def test_effective_absent_action_hold_parks(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, raw=_eff_body(rule="open", action="hold_admissions"))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.3)
    assert dispatched == []
    assert _events(queue, task_id, "pool_hold_parked")[0]["reason"] == "balance hold_admissions"


@pytest.mark.parametrize("last_valid, expect_dispatch", [("open", True), ("hold", False)])
def test_effective_absent_null_rule_keeps_last_valid(tmp_path, monkeypatch, notices,
                                                     last_valid, expect_dispatch):
    path = _signal(tmp_path, monkeypatch, raw=_eff_body(rule=last_valid))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    with TestClient(app):
        assert _wait(lambda: (pool_signal.load_last_valid(str(tmp_path)) or {}).get(
            "rule") == last_valid)
        path.write_text(_eff_body(rule=None))
        task_id = _impl(queue)
        if expect_dispatch:
            assert _wait(lambda: dispatched == [task_id])
        else:
            assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
            time.sleep(0.2)
            assert dispatched == []


def test_stale_effective_file_parks_unknown(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, raw=_eff_body({"claude": "open", "codex": "open"},
                                                 age=61 * 60))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.3)
    assert dispatched == []
    parked = _events(queue, task_id, "pool_hold_parked")
    assert len(parked) == 1 and parked[0]["code"] == pool_signal.UNKNOWN_REASON


def test_fresh_claude_open_resumes_claude_row_only(tmp_path, monkeypatch, notices):
    path = _signal(tmp_path, monkeypatch, raw=_eff_body({"claude": "hold", "codex": "hold"}))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    claude_id = _impl(queue, "impl-claude")
    codex_id = "impl-codex"   # routed to Codex by agent_override (#188)
    queue.enqueue(TaskRequest(codex_id, "implement", "work", branch="main",
                              project="agent_crew",
                              context={"risk_tier": 1, "agent_override": "codex"}))
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(claude_id) == "waiting_pool"
                     and queue.get_task_status(codex_id) == "waiting_pool")
        time.sleep(0.2)
        assert dispatched == []
        path.write_text(_eff_body({"claude": "open", "codex": "hold"}))
        assert _wait(lambda: dispatched == [claude_id])
        time.sleep(0.4)
        assert queue.get_task_status(codex_id) == "waiting_pool"
    assert dispatched == [claude_id]
    assert len(_events(queue, claude_id, "pool_hold_resumed")) == 1
    assert _events(queue, codex_id, "pool_hold_resumed") == []
    assert sorted(n[:2] for n in notices) == [(claude_id, "pool_hold_parked"),
                                              (codex_id, "pool_hold_parked")]


@pytest.mark.parametrize("effective", [{"claude": "hold", "codex": "hold"},
                                       {"claude": "weird"}])
def test_athena_server_still_exempt(tmp_path, monkeypatch, notices, effective):
    _signal(tmp_path, monkeypatch, raw=_eff_body(effective))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL, project="athena")
    task_id = _impl(queue, project="athena")
    with TestClient(app):
        assert _wait(lambda: dispatched == [task_id])
    assert _events(queue, task_id, "pool_hold_parked") == []
    assert notices == []
