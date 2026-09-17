"""agent_crew#311 — 런타임 STOP pause 전파 테스트 (부모 incident alfred#39).

Acceptance:
- queue N개 → pause → 추가 claim/start/dispatch 없음
- in-flight + queued successor → STOP 후 successor stage 시작 안 됨(=dequeue None)
- 재시작(새 TaskQueue 인스턴스)에도 pause 유지 → 드레인 계속 차단
- stale resume generation 거부
- 현재 generation resume은 lineage 이어감(중복 생성 없음)
- telemetry가 차단 이유 노출
- PORTABLE_CORE: pause 모듈은 Alfred/사설 상태 import 없음

stdlib unittest. `python3 -m pytest tests/test_pause_stop.py` 또는 직접 실행.
"""
import os, sys, tempfile, importlib, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agent_crew.queue import TaskQueue  # noqa: E402
from agent_crew.protocol import TaskRequest  # noqa: E402
from agent_crew import pause  # noqa: E402


def mk_task(i, task_type="implement"):
    return TaskRequest(task_id=f"t{i}", task_type=task_type,
                       description=f"task {i}", branch="main", priority=1,
                       context={}, project="testproj")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "tasks.db")
        self.state_dir = self.tmp  # dirname(db) == state_dir
        # global pause 파일 격리
        self._gp = os.path.join(self.tmp, "GLOBAL_PAUSE.json")
        pause.GLOBAL_PAUSE_FILE = self._gp
        self.q = TaskQueue(self.db)

    def _enqueue(self, n, task_type="implement"):
        for i in range(n):
            self.q.enqueue(mk_task(i, task_type))


class TestPauseBlocksDrain(Base):
    def test_pause_blocks_dequeue(self):
        self._enqueue(3)
        self.assertIsNotNone(self.q.dequeue(role="implementer"))  # 정상 시 claim
        pause.set_pause(self.state_dir, True, reason="incident", source="test", incident="alfred#39")
        self.assertIsNone(self.q.dequeue(role="implementer"),
                          "pause 활성이면 추가 claim/start 없어야")
        # 남은 task는 여전히 pending(완료로 조작 안 함)
        pend = [t for t in self.q.list_tasks(status="pending")]
        self.assertGreaterEqual(len(pend), 1)

    def test_successor_stage_does_not_start_under_pause(self):
        # in-flight 하나 + 후속 review가 큐에 있어도 pause면 review dequeue 안 됨
        self._enqueue(1, "implement")
        self.q.dequeue(role="implementer")  # in-flight로 전환
        self.q.enqueue(mk_task(99, "review"))  # 후속 stage 큐잉
        pause.set_pause(self.state_dir, True, source="test")
        self.assertIsNone(self.q.dequeue(role="reviewer"),
                          "pause면 successor review stage 시작 안 됨")

    def test_restart_preserves_pause(self):
        self._enqueue(2)
        pause.set_pause(self.state_dir, True, source="test", incident="alfred#39")
        # 서버 재시작 = 새 TaskQueue 인스턴스(같은 db). pause.json은 state_dir에 persisted.
        q2 = TaskQueue(self.db)
        self.assertTrue(pause.is_paused(self.state_dir))
        self.assertIsNone(q2.dequeue(role="implementer"),
                          "재시작 후에도 pause 유지 → 드레인 계속 차단")

    def test_global_pause_blocks_any_project(self):
        self._enqueue(1)
        pause.set_pause("", True, scope="global", source="external-manager", incident="alfred#39")
        self.assertTrue(pause.is_paused(self.state_dir))
        self.assertIsNone(self.q.dequeue(role="implementer"))


class TestResume(Base):
    def test_stale_resume_rejected(self):
        self._enqueue(1)
        rec = pause.set_pause(self.state_dir, True, source="test")  # gen N
        res = pause.resume(self.state_dir, generation=rec["generation"], source="test")  # same gen
        self.assertFalse(res["resumed"])
        self.assertTrue(pause.is_paused(self.state_dir), "stale resume은 STOP을 덮지 못함")
        self.assertIsNone(self.q.dequeue(role="implementer"))

    def test_current_generation_resume_continues_lineage(self):
        self._enqueue(2)
        rec = pause.set_pause(self.state_dir, True, source="test")
        self.assertIsNone(self.q.dequeue(role="implementer"))
        res = pause.resume(self.state_dir, generation=rec["generation"] + 1, source="test")
        self.assertTrue(res["resumed"])
        # resume 후 기존 lineage 그대로 dequeue(중복 task 생성 없음)
        t = self.q.dequeue(role="implementer")
        self.assertIsNotNone(t)
        # 총 task 수는 enqueue한 2개 그대로(resume이 복제하지 않음)
        allt = self.q.list_tasks()
        self.assertEqual(len([x for x in allt]), 2)


class TestFailClosed(Base):
    def test_corrupt_project_pause_fails_closed(self):
        self._enqueue(1)
        # pause.json이 존재하나 손상 → is_paused True(차단)여야. 예전 fail-open은 안전결함.
        with open(os.path.join(self.state_dir, "pause.json"), "w") as f:
            f.write("{ broken json ")
        self.assertTrue(pause.is_paused(self.state_dir), "손상된 pause.json은 fail-closed=paused")
        self.assertIsNone(self.q.dequeue(role="implementer"), "손상 상태에서 드레인 차단")
        self.assertIn("FAIL-CLOSED", pause.blocked_reason(self.state_dir))

    def test_corrupt_global_pause_fails_closed(self):
        self._enqueue(1)
        with open(self._gp, "w") as f:
            f.write("not a dict")
        self.assertTrue(pause.is_paused(self.state_dir))
        self.assertIsNone(self.q.dequeue(role="implementer"))

    def test_dequeue_fails_closed_on_pause_error(self):
        # pause.is_paused가 예외를 던져도 dequeue는 fail-closed(None)여야
        self._enqueue(1)
        orig = pause.is_paused
        pause.is_paused = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertIsNone(self.q.dequeue(role="implementer"),
                              "pause 판정 예외 시 dequeue는 차단(fail-closed)")
        finally:
            pause.is_paused = orig

    def test_missing_pause_is_not_paused(self):
        # 파일 부재는 정상 미pause(손상과 구분)
        self.assertFalse(pause.is_paused(self.state_dir))


class TestTelemetryAndPortability(Base):
    def test_status_shows_reason(self):
        pause.set_pause(self.state_dir, True, reason="autonomous-loop runaway",
                        source="alfred", incident="alfred#39")
        st = pause.pause_state(self.state_dir)
        self.assertTrue(st["paused"])
        why = pause.blocked_reason(self.state_dir)
        self.assertIn("alfred#39", why)
        self.assertIn("autonomous-loop runaway", why)

    def test_pause_module_is_portable_core(self):
        # PORTABLE_CORE: pause 모듈 소스에 Alfred/사설 fleet import가 없어야
        src = importlib.util.find_spec("agent_crew.pause").origin
        text = open(src, encoding="utf-8").read()
        for forbidden in ("import alfred", "from alfred", "blackboard", "safety_gate"):
            self.assertNotIn(forbidden, text, f"PORTABLE_CORE 위반: {forbidden}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
