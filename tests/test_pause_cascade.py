"""agent_crew#313 — result-cascade STOP 게이트 + atomic claim + resume-once 회귀테스트.
부모 incident alfred#39. 실제 서버 result 핸들러를 TestClient로 구동해 후속 stage 억제를 검증.
"""
import os, sys, json, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agent_crew.queue import TaskQueue  # noqa: E402
from agent_crew.protocol import TaskRequest  # noqa: E402
from agent_crew import pause  # noqa: E402


def mk(i, tt="implement"):
    return TaskRequest(task_id=f"t{i}", task_type=tt, description=f"d{i}",
                       branch="main", priority=1, context={}, project="testproj")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "tasks.db")
        self.sd = self.tmp
        pause.GLOBAL_PAUSE_FILE = os.path.join(self.tmp, "GLOBAL_PAUSE.json")
        self.q = TaskQueue(self.db)

    def _client(self):
        from fastapi.testclient import TestClient
        from agent_crew.server import create_app
        app = create_app(db_path=self.db, push_fn=lambda *a, **k: None, project="testproj")
        c = TestClient(app)
        c.__enter__()          # lifespan startup 실행(state["queue"] 채움)
        self.addCleanup(lambda: c.__exit__(None, None, None))
        return c

    def _review_count(self):
        return len([t for t in self.q.list_tasks() if t.task_type == "review"])


class TestResultCascadeGate(Base):
    def test_impl_result_under_pause_no_review(self):
        # in-flight impl → pause → POST result completed → review 미enqueue(억제), result는 저장
        self.q.enqueue(mk(1, "implement"))
        t = self.q.dequeue(role="implementer")   # in_progress (아직 미pause)
        self.assertIsNotNone(t)
        pause.set_pause(self.sd, True, source="test", incident="alfred#39")
        c = self._client()
        r = c.post(f"/tasks/{t.task_id}/result",
                   json={"task_id": t.task_id, "status": "completed", "summary": "done", "pr_number": 42})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("suppressed_by_pause"), r.json())
        self.assertEqual(self._review_count(), 0, "pause 중 auto-review가 enqueue되면 안 됨")
        # 억제 durable 기록 존재(재개 replay용)
        self.assertTrue(any(s["task_id"] == t.task_id for s in pause.list_suppressed(self.sd)))

    def test_control_no_pause_not_suppressed(self):
        # 대조군: pause가 없으면 게이트가 result를 통과시킨다(over-suppress 안 함).
        # (실제 review enqueue 여부는 pause와 무관한 cascade 전제조건에 좌우되므로
        #  여기서는 '게이트가 억제하지 않음'만 검증한다.)
        self.q.enqueue(mk(2, "implement"))
        t = self.q.dequeue(role="implementer")
        c = self._client()
        r = c.post(f"/tasks/{t.task_id}/result",
                   json={"task_id": t.task_id, "status": "completed", "summary": "done"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json().get("suppressed_by_pause"), "pause 없으면 억제되면 안 됨")
        self.assertFalse(r.json().get("cascade_suppressed"))
        # 억제 기록도 없어야
        self.assertEqual(len(pause.list_suppressed(self.sd)), 0)

    def test_corrupt_pause_during_result_fails_closed(self):
        self.q.enqueue(mk(3, "implement"))
        t = self.q.dequeue(role="implementer")
        with open(os.path.join(self.sd, "pause.json"), "w") as f:
            f.write("{ broken ")
        c = self._client()
        r = c.post(f"/tasks/{t.task_id}/result",
                   json={"task_id": t.task_id, "status": "completed", "summary": "x", "pr_number": 1})
        self.assertTrue(r.json().get("suppressed_by_pause"), "손상 pause state → fail-closed 억제")
        self.assertEqual(self._review_count(), 0)


class TestResumeOnce(Base):
    def test_suppressed_replay_once_no_duplicate(self):
        pause.record_suppressed(self.sd, task_id="tX", task_type="implement",
                                status="completed", pr_number=1, generation=1)
        # 중복 기록 방지
        pause.record_suppressed(self.sd, task_id="tX", task_type="implement",
                                status="completed", pr_number=1, generation=1)
        pend = pause.list_suppressed(self.sd, only_pending=True)
        self.assertEqual(len([s for s in pend if s["task_id"] == "tX"]), 1)
        # replay 1회 → mark → 다시 pending에 없음(중복 replay 불가)
        pause.mark_replayed(self.sd, ["tX"])
        self.assertEqual(len(pause.list_suppressed(self.sd, only_pending=True)), 0)


class TestAtomicClaim(Base):
    def test_pause_set_blocks_claim_in_transaction(self):
        # 사전체크를 우회하더라도 임계구역 재확인이 claim을 막는다(간이 검증: pause 상태서 dequeue None)
        self.q.enqueue(mk(4, "implement"))
        pause.set_pause(self.sd, True, source="test")
        self.assertIsNone(self.q.dequeue(role="implementer"))
        # 트랜잭션 내부 재확인 코드 경로 존재 확인
        import inspect
        src = inspect.getsource(TaskQueue.dequeue)
        self.assertIn("atomic pause recheck", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
