"""`/tasks/expire-stale` 는 전역이다 — 범위를 말하지 않으면 미리보기만 한다.

왜: 2026-09-23, coordinator 가 정체된 리뷰 **1건**을 치우려고 이 엔드포인트를 호출했는데
전역 스윕이라 **3건**이 취소됐고, 그중 둘은 오너가 방금 계속하라고 지시한 라이브 레인이었다.
요청에도 '전부' 라는 말이 없었고 응답도 무엇을 잃는지 **잃기 전에** 말해주지 않았다.
"""
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.fixture()
def client(tmp_path):
    panes = {"implementer": "%C", "claude": "%C",
             "reviewer": "%X", "codex": "%X",
             "tester": "%G", "gemini": "%G"}
    app = create_app(
        db_path=str(tmp_path / "t.db"),
        pane_map=panes,
        port=8299,
        push_fn=lambda pane_id, text: None,
        watchdog_disabled=True,
    )
    # ⛔`q()` 는 `state["queue"]` 를 읽는다 — lifespan 이 돌아야 채워진다.
    #   `TestClient(app)` 를 그냥 쓰면 startup 이 안 돌아 KeyError('queue') 가 난다.
    with TestClient(app) as c:
        c.db_path = str(tmp_path / "t.db")
        yield c


def _seed_in_progress(client, task_id: str, task_type: str, idle_s: float) -> None:
    """Put a task in ``in_progress`` with ``last_activity_at`` ``idle_s`` ago."""
    import sqlite3
    import time
    r = client.post("/tasks", json={"task_id": task_id, "task_type": task_type,
                                    "description": "seed", "branch": "", "priority": 3,
                                    "project": "t", "context": {}})
    assert r.status_code in (200, 201), r.text
    con = sqlite3.connect(client.db_path)
    n = con.execute(
        "UPDATE tasks SET status = 'in_progress', last_activity_at = ? WHERE task_id = ?",
        (time.time() - idle_s, task_id),
    ).rowcount
    con.commit(); con.close()
    assert n == 1, f"seed {task_id}: rows updated={n}"


def test_default_is_a_preview_and_cancels_nothing(client):
    r = client.post("/tasks/expire-stale", params={"older_than": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert "would_cancel" in body, "미리보기 응답에 cancelled 키가 있으면 실행으로 오독된다"
    assert "cancelled" not in body


def test_preview_names_its_scope(client):
    body = client.post("/tasks/expire-stale", params={"older_than": 0}).json()
    assert body["scope"] == "global"


def test_scoped_call_names_the_single_task(client):
    body = client.post("/tasks/expire-stale",
                       params={"older_than": 0, "task_id": "only-this-one"}).json()
    assert body["scope"] == "only-this-one"
    # 그 id 가 후보에 없으면 아무것도 취소하지 않고 이유를 말한다.
    assert body["cancelled"] == []
    assert "reason" in body


def test_global_sweep_requires_saying_so(client):
    body = client.post("/tasks/expire-stale",
                       params={"older_than": 0, "dry_run": "false"}).json()
    assert body["dry_run"] is False
    assert body["scope"] == "global"
    assert "cancelled" in body


def test_a_preview_can_never_be_mistaken_for_a_completed_sweep(client):
    """⛔미리보기와 실행의 응답 키가 겹치면 호출자가 구분을 못 한다."""
    preview = client.post("/tasks/expire-stale", params={"older_than": 0}).json()
    real = client.post("/tasks/expire-stale",
                       params={"older_than": 0, "dry_run": "false"}).json()
    assert set(preview) & {"cancelled"} == set()
    assert "cancelled" in real


def test_preview_applies_older_than_so_a_fresh_task_is_not_listed(client):
    """⛔codex 리뷰(2026-09-23): 폴백이 ``older_than`` 을 무시하고 모든 in_progress 를
    후보로 삼았다 — 미리보기가 라이브 레인을 '취소 예정' 으로 보고한다."""
    _seed_in_progress(client, "stale-one", "implement", idle_s=7200)
    _seed_in_progress(client, "fresh-one", "review", idle_s=0)
    body = client.post("/tasks/expire-stale", params={"older_than": 3600}).json()
    assert body["would_cancel"] == ["stale-one"], body
    # 둘 다 in_progress 로 남아 있다 — 미리보기는 아무것도 바꾸지 않는다.
    assert client.get("/tasks/stale-one").json()["status"] == "in_progress"
    assert client.get("/tasks/fresh-one").json()["status"] == "in_progress"


def test_scoped_real_cancel_cancels_exactly_that_task(client):
    """⛔codex 리뷰(2026-09-23): scoped 실행이 존재하지 않는 ``cancel_task`` 를 불러 500 이었다."""
    _seed_in_progress(client, "stale-one", "implement", idle_s=7200)
    _seed_in_progress(client, "fresh-one", "review", idle_s=0)
    r = client.post("/tasks/expire-stale",
                    params={"older_than": 3600, "task_id": "stale-one", "dry_run": "false"})
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] == ["stale-one"]
    assert client.get("/tasks/stale-one").json()["status"] == "cancelled"
    assert client.get("/tasks/fresh-one").json()["status"] == "in_progress"


def test_scoped_cancel_signals_the_bound_worker(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    app = create_app(db_path=db_path, pane_map={"implementer": "%101"},
                     port=8299, push_fn=lambda *_: None, watchdog_disabled=True)
    with TestClient(app) as client:
        client.db_path = db_path
        _seed_in_progress(client, "stale-one", "implement", idle_s=7200)
        TaskQueue(db_path).set_push_at("stale-one", pane_id="%101")
        with patch("agent_crew.server._pane_alive_for_push", side_effect=[True, False]), \
             patch("agent_crew.server.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            response = client.post("/tasks/expire-stale", params={
                "older_than": 3600, "task_id": "stale-one", "dry_run": "false",
            })

    assert response.status_code == 200, response.text
    assert response.json()["cancel_signal_outcome"] == "pane_exited"
    assert any(call.args[0] == ["tmux", "send-keys", "-t", "%101", "C-c"]
               for call in run.call_args_list)


def test_global_sweep_acts_only_on_its_previewed_candidates(client, monkeypatch):
    _seed_in_progress(client, "stale-one", "implement", idle_s=7200)
    _seed_in_progress(client, "fresh-one", "review", idle_s=0)
    original = TaskQueue.expire_stale
    calls = []

    def preview_only(self, older_than_seconds=600.0, dry_run=False):
        calls.append(dry_run)
        assert dry_run, "global sweep must not re-query after selecting candidates"
        return original(self, older_than_seconds=older_than_seconds, dry_run=True)

    monkeypatch.setattr(TaskQueue, "expire_stale", preview_only)
    response = client.post("/tasks/expire-stale", params={
        "older_than": 3600, "dry_run": "false",
    })

    assert response.status_code == 200, response.text
    assert response.json()["cancelled"] == ["stale-one"]
    assert response.json()["cancel_signal_outcomes"] == {"stale-one": "unreachable"}
    assert calls == [True]
    assert client.get("/tasks/fresh-one").json()["status"] == "in_progress"


def test_backend_without_preview_fails_closed(client, monkeypatch):
    _seed_in_progress(client, "stale-one", "implement", idle_s=7200)

    def no_preview(self, older_than_seconds=600.0):
        raise AssertionError("a backend without preview must never sweep")

    monkeypatch.setattr(TaskQueue, "expire_stale", no_preview)
    response = client.post("/tasks/expire-stale", params={
        "older_than": 3600, "task_id": "stale-one", "dry_run": "false",
    })

    assert response.status_code == 501
    assert client.get("/tasks/stale-one").json()["status"] == "in_progress"


def test_scoped_call_on_a_fresh_task_refuses(client):
    """범위를 지정해도 ``older_than`` 을 못 넘긴 task 는 취소하지 않는다."""
    _seed_in_progress(client, "fresh-one", "review", idle_s=0)
    body = client.post("/tasks/expire-stale",
                       params={"older_than": 3600, "task_id": "fresh-one", "dry_run": "false"}).json()
    assert body["cancelled"] == [] and "reason" in body
    assert client.get("/tasks/fresh-one").json()["status"] == "in_progress"
