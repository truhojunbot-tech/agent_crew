"""`/tasks/expire-stale` 는 전역이다 — 범위를 말하지 않으면 미리보기만 한다.

왜: 2026-09-23, coordinator 가 정체된 리뷰 **1건**을 치우려고 이 엔드포인트를 호출했는데
전역 스윕이라 **3건**이 취소됐고, 그중 둘은 오너가 방금 계속하라고 지시한 라이브 레인이었다.
요청에도 '전부' 라는 말이 없었고 응답도 무엇을 잃는지 **잃기 전에** 말해주지 않았다.
"""
import pytest
from fastapi.testclient import TestClient

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
        yield c


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
