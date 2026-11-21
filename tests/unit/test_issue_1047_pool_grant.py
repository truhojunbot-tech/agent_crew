"""#1047: a Quota grant lets one named task through a pool hold.

Under ``admissions_effective[claude]=hold`` Quota can decide one task should
still run (alpha_engine ``retry-impl-48142056-a1``, Athena's 11-24 00Z
deadline). Quota writes ``pool_grants.json``; the crew only reads it. A grant
matches on the server identity (never ``task.project``), the round's pool, the
row id or (``include_lineage``) its lineage root, a non-empty
``authority_ref``, an unexpired epoch ``expires_at`` and the count of
``pool_grant_used`` events below ``max_dispatches``. Everything else fails
closed to today's park.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from agent_crew import pool_signal
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from tests.unit.test_issue_1033_effective_pool import _eff_body
from tests.unit.test_issue_1033_pool_hold import (  # noqa: F401 (notices: fixture)
    CLAUDE_IMPL, _app, _events, _impl, _signal, _wait, notices)

ROOT = "retry-impl-48142056-a1"
FIX = "fix-review-retry-impl-48142056-a1-r0-r1"


def _grant(**over):
    grant = {"grant_id": "g-1", "authority_ref": "bb 20261121t065931z",
             "project": "agent_crew", "task_id": ROOT, "pool": "claude",
             "expires_at": time.time() + 3600, "max_dispatches": 1,
             "include_lineage": False}
    grant.update(over)
    return grant


def _grants(tmp_path, monkeypatch, *grants, raw=None):
    path = tmp_path / "quota" / "pool_grants.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None
                    else json.dumps({"schema": 1, "grants": list(grants)}))
    monkeypatch.setenv(pool_signal.POOL_GRANTS_ENV, str(path))
    return path


def _hold(tmp_path, monkeypatch):
    _signal(tmp_path, monkeypatch, raw=_eff_body({"claude": "hold", "codex": "open"}))


def _fix(queue, task_id=FIX, root=ROOT):
    # Auto fix rounds are ``implement`` rows carrying the lineage root.
    queue.enqueue(TaskRequest(task_id, "implement", "fix", branch="main",
                              project="agent_crew",
                              context={"risk_tier": 1, "lineage_root_task_id": root}))
    return task_id


def _used(queue, task_id):
    return _events(queue, task_id, pool_signal.GRANT_USED_EVENT)


def _parks(tmp_path, monkeypatch, *grants, raw=None, task_id=ROOT, project="agent_crew"):
    """Under a Claude hold the row parks with zero dispatches and zero uses."""
    _hold(tmp_path, monkeypatch)
    _grants(tmp_path, monkeypatch, *grants, raw=raw)
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    _impl(queue, task_id, project=project)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.2)
    assert dispatched == []
    assert _used(queue, task_id) == []
    assert len(_events(queue, task_id, "pool_hold_parked")) == 1


# ── reader ────────────────────────────────────────────────────────────────

def test_conftest_never_reads_the_live_grants_file():
    assert "alfred/quota" not in pool_signal.pool_grants_path()


def test_load_keeps_valid_entries_only(tmp_path, monkeypatch):
    bad = [_grant(authority_ref=""), _grant(authority_ref=None), _grant(pool="gemini"),
           _grant(expires_at="2026-11-24T00:00:00Z"), _grant(expires_at=True),
           _grant(max_dispatches=-1), _grant(max_dispatches=1.5),
           _grant(include_lineage="yes"), _grant(task_id=""), _grant(grant_id=7),
           "not-an-object"]
    missing = _grant()
    del missing["authority_ref"]
    path = _grants(tmp_path, monkeypatch, *bad, missing, _grant(grant_id="ok"))
    assert [g["grant_id"] for g in pool_signal.load_pool_grants()] == ["ok"]
    assert pool_signal.load_pool_grants(str(path))[0]["authority_ref"] == "bb 20261121t065931z"


@pytest.mark.parametrize("raw", ["{not json", "", json.dumps([]),
                                 json.dumps({"schema": 2, "grants": [_grant()]}),
                                 json.dumps({"schema": True, "grants": [_grant()]}),
                                 json.dumps({"grants": [_grant()]}),
                                 json.dumps({"schema": 1, "grants": {"g-1": _grant()}})],
                         ids=["not-json", "empty", "list", "schema-2", "schema-bool",
                              "no-schema", "grants-object"])
def test_corrupt_or_unknown_schema_file_is_no_grants(tmp_path, monkeypatch, raw):
    _grants(tmp_path, monkeypatch, raw=raw)
    assert pool_signal.load_pool_grants() == []


def test_missing_file_is_no_grants(tmp_path, monkeypatch):
    monkeypatch.setenv(pool_signal.POOL_GRANTS_ENV, str(tmp_path / "absent.json"))
    assert pool_signal.load_pool_grants() == []


def test_record_use_stops_at_the_cap(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    grant = {"grant_id": "g-2", "authority_ref": "bb x", "pool": "claude",
             "max_dispatches": 2}
    assert queue.record_pool_grant_use("a", grant) == 1
    assert queue.record_pool_grant_use("b", grant) == 2
    assert queue.record_pool_grant_use("c", grant) is None
    assert queue.count_pool_grant_uses("g-2") == 2
    assert queue.count_pool_grant_uses("other") == 0


# ── gate ──────────────────────────────────────────────────────────────────

def test_matching_grant_under_hold_dispatches_once_and_records_use(
        tmp_path, monkeypatch, notices):
    _hold(tmp_path, monkeypatch)
    _grants(tmp_path, monkeypatch, _grant())
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    _impl(queue, ROOT)
    other = _impl(queue, "impl-ungranted")
    with TestClient(app):
        assert _wait(lambda: dispatched == [ROOT]
                     and queue.get_task_status(other) == "waiting_pool")
        time.sleep(0.3)
    assert dispatched == [ROOT]   # the ungranted row on the same crew stays parked
    used = _used(queue, ROOT)
    assert len(used) == 1
    assert {k: used[0][k] for k in ("grant_id", "authority_ref", "pool", "task_id",
                                    "used", "max")} == {
        "grant_id": "g-1", "authority_ref": "bb 20261121t065931z", "pool": "claude",
        "task_id": ROOT, "used": 1, "max": 1}
    assert _events(queue, ROOT, "pool_hold_parked") == []


def test_cap_reached_next_round_parks(tmp_path, monkeypatch, notices):
    _hold(tmp_path, monkeypatch)
    _grants(tmp_path, monkeypatch, _grant(include_lineage=True, max_dispatches=1))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    _impl(queue, ROOT)
    with TestClient(app):
        assert _wait(lambda: dispatched == [ROOT])
        _fix(queue)
        assert _wait(lambda: queue.get_task_status(FIX) == "waiting_pool")
        time.sleep(0.2)
    assert dispatched == [ROOT]
    assert queue.count_pool_grant_uses("g-1") == 1
    assert _used(queue, FIX) == []


def test_expired_grant_parks(tmp_path, monkeypatch, notices):
    _parks(tmp_path, monkeypatch, _grant(expires_at=time.time() - 1))


def test_project_mismatch_parks(tmp_path, monkeypatch, notices):
    """Row payload project=athena on a non-Athena server: a grant for athena
    never matches, because only the server identity counts."""
    _parks(tmp_path, monkeypatch, _grant(project="athena"), project="athena")


def test_pool_mismatch_parks(tmp_path, monkeypatch, notices):
    _parks(tmp_path, monkeypatch, _grant(pool="codex"))


def test_task_mismatch_parks(tmp_path, monkeypatch, notices):
    _parks(tmp_path, monkeypatch, _grant(task_id="impl-someone-else"))


@pytest.mark.parametrize("raw", ["{not json",
                                 json.dumps({"schema": 2, "grants": [_grant()]})],
                         ids=["corrupt", "schema-2"])
def test_corrupt_or_wrong_schema_parks(tmp_path, monkeypatch, notices, raw):
    _parks(tmp_path, monkeypatch, raw=raw)


@pytest.mark.parametrize("authority", ["missing", "", "   "])
def test_missing_or_empty_authority_ref_parks(tmp_path, monkeypatch, notices, authority):
    grant = _grant(authority_ref=authority)
    if authority == "missing":
        del grant["authority_ref"]
    _parks(tmp_path, monkeypatch, grant)


@pytest.mark.parametrize("include_lineage", [True, False])
def test_include_lineage_covers_the_fix_round(tmp_path, monkeypatch, notices,
                                              include_lineage):
    _hold(tmp_path, monkeypatch)
    _grants(tmp_path, monkeypatch, _grant(include_lineage=include_lineage))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    _fix(queue)
    with TestClient(app):
        if include_lineage:
            assert _wait(lambda: dispatched == [FIX])
        else:
            assert _wait(lambda: queue.get_task_status(FIX) == "waiting_pool")
        time.sleep(0.2)
    assert dispatched == ([FIX] if include_lineage else [])
    assert len(_used(queue, FIX)) == (1 if include_lineage else 0)


def test_parked_row_resumes_when_a_grant_appears(tmp_path, monkeypatch, notices):
    _hold(tmp_path, monkeypatch)
    path = _grants(tmp_path, monkeypatch, raw=json.dumps({"schema": 1, "grants": []}))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    _impl(queue, ROOT)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(ROOT) == "waiting_pool")
        time.sleep(0.2)
        assert dispatched == []
        path.write_text(json.dumps({"schema": 1, "grants": [_grant()]}))
        assert _wait(lambda: dispatched == [ROOT])
        time.sleep(0.3)
    assert dispatched == [ROOT]
    resumed = _events(queue, ROOT, "pool_hold_resumed")
    assert len(resumed) == 1 and "g-1" in resumed[0]["reason"]
    assert len(_used(queue, ROOT)) == 1


def test_tick_sweep_spends_no_use(tmp_path, monkeypatch, notices):
    """The tick sweep leaves a granted pending row pending without spending a
    use; only the dispatcher's spawn records ``pool_grant_used``."""
    _hold(tmp_path, monkeypatch)
    _grants(tmp_path, monkeypatch, _grant(include_lineage=True))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    _fix(queue)
    with TestClient(app):
        out = app.state.apply_pool_signal()
    assert out["parked"] == [] and queue.get_task_status(FIX) == "pending"
    assert dispatched == []
    assert queue.count_pool_grant_uses("g-1") == 0
