"""디스패처의 동시성 키가 **실행 자원**인지 지키는 회귀 테스트.

## 무엇을 막는가 (2026-09-23 실측)

`:8101`(alpha_engine)에서 pane 세 개가 전부 비어 있는데 `implement` 두 건이
직렬로 돌았다. 서버 로그가 이유를 그대로 찍었다:

    _try_push_next: task_type implement already in progress

디스패처 루프도 같은 결함을 갖고 있었다 — `for role in (...)` 가 **역할 슬롯**을
잠갔고 `agent_override` 는 그 뒤 실행 대상을 고를 때만 읽혔다. 그래서 override 로
다른 worker 에 보낸 두 번째 implement 가 **스케줄 단계에서** 막혔다.
실효 병렬도가 3이 아니라 1이었다.

## 무엇을 단언하는가

1. 서로 다른 worker + 서로 다른 worktree 면 **같은 task_type 도 시간상 겹쳐서** 돈다
2. 같은 worker 에는 동시에 두 task 가 붙지 않는다
3. 같은 worktree 에는 동시 writer 가 없다
4. `task_type` 은 잠금 키가 아니다(소스에 그 술어가 남아 있지 않다)
"""
import asyncio
import json
import os
import time
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from agent_crew.server import create_app

RUN_S = 0.35


def _state(tmp_path) -> str:
    """운영과 같은 `roles` 스키마 — worker 마다 **다른 worktree**."""
    paths = {}
    for agent in ("claude", "codex", "gemini"):
        p = tmp_path / f"wt_{agent}"
        p.mkdir()
        (p / ".git").mkdir()
        paths[agent] = str(p)
    state = {
        "project": "t",
        "roles": [
            {"role": "implementer", "agent": "codex", "worktree": paths["codex"]},
            {"role": "reviewer", "agent": "claude", "worktree": paths["claude"]},
            {"role": "tester", "agent": "gemini", "worktree": paths["gemini"]},
        ],
    }
    f = tmp_path / "state.json"
    f.write_text(json.dumps(state))
    return str(f)


def _agent_of(cmd0: str) -> str:
    if "gemini" in cmd0 or cmd0.endswith("/agy") or cmd0 == "agy":
        return "gemini"
    if "codex" in cmd0:
        return "codex"
    return "claude"


def _run_dispatcher(tmp_db, tmp_path, payloads, settle_s: float = 1.6):
    """태스크를 넣고 디스패처를 돌린 뒤, worker 별 (시작, 끝) 구간을 돌려준다."""
    spans: list[tuple[str, float, float]] = []

    async def fake_subprocess(*args, **kwargs):
        agent = _agent_of(str(args[0]) if args else "")
        t0 = time.monotonic()
        proc = MagicMock()
        proc.returncode = 0
        proc.kill = MagicMock()

        async def _wait():
            await asyncio.sleep(RUN_S)
            spans.append((agent, t0, time.monotonic()))
            return 0

        proc.wait = _wait
        return proc

    state_file = _state(tmp_path)
    # 프로토콜 재생성이 port file 을 요구한다(_ensure_role_protocol). 없으면
    # dispatch 가 그 자리에서 끊겨 subprocess 가 아예 안 뜬다.
    port_file = os.path.join(os.path.dirname(tmp_db), "port")
    with open(port_file, "w") as f:
        f.write("18991\n")
    with patch.dict(os.environ, {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
    }):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            with patch("subprocess.run",
                       return_value=MagicMock(returncode=0, stdout="", stderr="")):
                app = create_app(
                    db_path=tmp_db, pane_map={}, port=18991,
                    state_path=state_file,
                    watchdog_disabled=True, anomaly_disabled=True,
                )
                with TestClient(app) as client:
                    for p in payloads:
                        assert client.post("/tasks", json=p).status_code == 201
                    time.sleep(settle_s)
    return spans


def _payload(task_id: str, override: str = "", task_type: str = "implement") -> dict:
    ctx: dict = {}
    if override:
        ctx["agent_override"] = override
    return {
        "task_id": task_id,
        "task_type": task_type,
        "description": "work",
        "branch": "main",
        "priority": 3,
        "context": ctx,
        "project": "t",
    }


def _overlap_s(a: tuple[str, float, float], b: tuple[str, float, float]) -> float:
    return min(a[2], b[2]) - max(a[1], b[1])


# ---------------------------------------------------------------------------
# 요구사항 ①: 서로 다른 worker + 서로 다른 worktree 면 같은 task_type 도 병렬
# ---------------------------------------------------------------------------

def test_two_implements_on_different_workers_overlap_in_time(tmp_db, tmp_path):
    """`implement` 두 건 — 하나는 기본(codex), 하나는 `agent_override=claude`.

    ⭐단언은 "둘 다 디스패치됐다" 가 아니라 **시간상 겹쳤다** 이다. 순차 실행도
      결국 둘 다 디스패치되므로, 겹침을 안 재면 이 회귀를 못 잡는다.
    """
    spans = _run_dispatcher(tmp_db, tmp_path, [
        _payload("impl_default"),
        _payload("impl_override", override="claude"),
    ])
    agents = sorted(s[0] for s in spans)
    assert agents == ["claude", "codex"], f"두 worker 로 안 갈렸다: {spans}"
    a, b = spans[0], spans[1]
    assert _overlap_s(a, b) > 0, (
        f"두 implement 가 시간상 겹치지 않았다 — 직렬로 돌았다: {spans}"
    )


# ---------------------------------------------------------------------------
# 요구사항 ②: 같은 worker 에는 동시에 두 task 금지
# ---------------------------------------------------------------------------

def test_same_worker_runs_its_two_tasks_serially(tmp_db, tmp_path):
    """둘 다 codex 로 가면 겹치면 안 된다 — 한 `--continue` 세션이다."""
    spans = _run_dispatcher(tmp_db, tmp_path, [
        _payload("impl_a"),
        _payload("impl_b"),
    ], settle_s=2.2)
    codex = [s for s in spans if s[0] == "codex"]
    assert len(codex) == 2, f"codex 태스크 2건이 다 안 돌았다: {spans}"
    assert _overlap_s(codex[0], codex[1]) <= 0, (
        f"같은 worker 에서 두 task 가 겹쳤다: {codex}"
    )


# ---------------------------------------------------------------------------
# 요구사항 ③: 같은 worktree 에 동시 writer 금지
# ---------------------------------------------------------------------------

def test_two_roles_resolving_to_one_worktree_do_not_overlap(tmp_db, tmp_path):
    """`review`(기본 claude)와 `implement`(override=claude)는 같은 worktree 다."""
    spans = _run_dispatcher(tmp_db, tmp_path, [
        _payload("rev_c", task_type="review"),
        _payload("impl_c", override="claude"),
    ], settle_s=2.2)
    claude = [s for s in spans if s[0] == "claude"]
    assert len(claude) == 2, f"claude 태스크 2건이 다 안 돌았다: {spans}"
    assert _overlap_s(claude[0], claude[1]) <= 0, (
        f"같은 worktree 에 동시 writer 가 붙었다: {claude}"
    )


# ---------------------------------------------------------------------------
# 요구사항 ④: task_type 은 잠금 키가 아니다
# ---------------------------------------------------------------------------

def test_task_type_is_not_used_as_a_concurrency_lock_key():
    """⛔`has_in_progress(<task_type>)` 로 스케줄을 막던 술어가 남아 있으면 red.

    구현이 다시 task_type 잠금으로 돌아가면 위 겹침 테스트는 타이밍에 따라
    조용히 통과할 수 있다 — 술어 자체를 못박는다.
    """
    import inspect

    import agent_crew.server as srv

    src = "\n".join(
        l for l in inspect.getsource(srv).splitlines()
        if not l.lstrip().startswith("#")
    )
    push = src[src.index("def _try_push_next("):]
    push = push[: push.index("\n    def ", 10)]
    assert "has_in_progress(task_type)" not in push, (
        "_try_push_next 가 다시 전역 task_type 잠금을 쓴다"
    )
    loop = src[src.index("async def _dispatcher_loop("):]
    loop = loop[: loop.index("\n    app.state.dispatcher_enabled")]
    assert 'for role in ("implementer", "reviewer", "tester")' not in loop, (
        "디스패처 루프가 다시 역할 슬롯을 잠금 키로 쓴다"
    )
    assert "active_workers" in loop and "active_worktrees" in loop, (
        "실행 슬롯/worktree lease 가 사라졌다"
    )
