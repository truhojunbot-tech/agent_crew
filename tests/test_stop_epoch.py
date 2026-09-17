#!/usr/bin/env python3
"""#314 §1/§2: DB-backed STOP epoch — 원자성/권위/boot reconcile/CAS resume 결정론 테스트.

핵심 증명:
- 원자 STOP 판단의 권위는 runtime_stop 단일행이다(pause.json 파일읽기 아님).
- 사전확인(pre-check)이 unpaused를 봐도, in-txn 게이트가 DB paused를 잡아 enqueue/claim이 거부된다.
- 부팅 reconcile은 더 높은 epoch가 승자, 같은 epoch 상태불일치는 fail-closed(paused).
- resume은 DB CAS로 stale(현재 epoch 이하)이면 거부 — 최신 STOP을 못 덮는다.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_crew.queue import TaskQueue, PausedError  # noqa: E402
from agent_crew.protocol import TaskRequest, TaskResult  # noqa: E402
from agent_crew import pause as pausemod  # noqa: E402


def _task(tid="t1", ttype="implement"):
    return TaskRequest(task_id=tid, task_type=ttype, description="x",
                       branch="", priority=3, context={}, project="p")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "tasks.db")
        # 전역 pause 파일이 테스트에 새지 않도록 없는 경로로 격리
        self._orig_global = pausemod.GLOBAL_PAUSE_FILE
        pausemod.GLOBAL_PAUSE_FILE = os.path.join(self.dir, "NO_GLOBAL.json")

    def tearDown(self):
        pausemod.GLOBAL_PAUSE_FILE = self._orig_global


class TestEpochBasics(Base):
    def test_default_unpaused_epoch0(self):
        q = TaskQueue(self.db)
        st = q.get_stop_epoch()
        self.assertFalse(st["paused"])
        self.assertEqual(st["epoch"], 0)

    def test_set_stop_epoch_monotonic(self):
        q = TaskQueue(self.db)
        e1 = q.set_stop_epoch(True, incident="alfred#39")
        e2 = q.set_stop_epoch(False)
        e3 = q.set_stop_epoch(True, incident="alfred#39")
        self.assertEqual([e1, e2, e3], [1, 2, 3])
        self.assertTrue(q.get_stop_epoch()["paused"])
        self.assertEqual(q.get_stop_epoch()["incident"], "alfred#39")


class TestAtomicGate(Base):
    def test_enqueue_blocked_when_db_paused(self):
        q = TaskQueue(self.db)
        q.set_stop_epoch(True, incident="alfred#39")
        with self.assertRaises(PausedError):
            q.enqueue(_task())

    def test_enqueue_ok_when_unpaused(self):
        q = TaskQueue(self.db)
        self.assertEqual(q.enqueue(_task()), "t1")

    def test_dequeue_blocked_when_db_paused(self):
        q = TaskQueue(self.db)
        q.enqueue(_task())              # 먼저 넣고
        q.set_stop_epoch(True, incident="alfred#39")
        self.assertIsNone(q.dequeue(role="implementer"))

    def test_discuss_blocked_when_db_paused(self):
        q = TaskQueue(self.db)
        q.enqueue(TaskRequest(task_id="d1", task_type="discuss", description="x",
                              branch="", priority=3, context={"agent": "claude"}, project="p"))
        q.set_stop_epoch(True, incident="alfred#39")
        self.assertIsNone(q.dequeue_discuss_for_agent("claude"))

    def test_interleave_stale_precheck_still_blocked(self):
        """사전확인(pre-check)이 unpaused를 봤다고 가정해도(모킹), DB가 paused면 in-txn 게이트가 잡는다.
        → 권위가 file-read pre-check가 아니라 in-txn runtime_stop임을 결정론적으로 증명."""
        q = TaskQueue(self.db)
        q.set_stop_epoch(True, incident="alfred#39")
        # pre-check가 거짓으로 통과했다고 가정
        q._stop_active_precheck = lambda: False
        # enqueue: in-txn 게이트가 여전히 차단
        with self.assertRaises(PausedError):
            q.enqueue(_task("t2"))
        # dequeue: in-txn 게이트가 여전히 None
        q.enqueue.__self__  # noop, keep q referenced
        self.assertIsNone(q.dequeue(role="implementer"))

    def test_gate_failclosed_on_table_read_error(self):
        """runtime_stop 읽기 자체가 불가하면 fail-closed(차단)."""
        q = TaskQueue(self.db)

        class _BadConn:
            def execute(self, *a, **k):
                raise RuntimeError("table gone")
        self.assertTrue(q._stop_active_in_txn(_BadConn()))


class TestBootReconcile(Base):
    def test_reconcile_picks_up_armed_pausejson(self):
        """restart-while-paused: 기존 armed pause.json(gen5) → 새 코드 부팅 시 runtime_stop paused."""
        pausemod.set_pause(self.dir, True, scope="project", reason="incident",
                           incident="alfred#39", generation=5)
        q = TaskQueue(self.db)          # 부팅 reconcile 발생
        st = q.get_stop_epoch()
        self.assertTrue(st["paused"])
        self.assertGreaterEqual(st["epoch"], 5)
        self.assertEqual(st["incident"], "alfred#39")
        # 그리고 즉시 게이트가 작동
        with self.assertRaises(PausedError):
            q.enqueue(_task())

    def test_reconcile_higher_db_epoch_wins(self):
        q = TaskQueue(self.db)
        q.set_stop_epoch(True, incident="alfred#39")   # db epoch1 paused
        q.set_stop_epoch(True, incident="alfred#39")   # db epoch2 paused
        # pause.json은 낮은 gen1 unpaused로 뒤처짐
        pausemod.set_pause(self.dir, False, scope="project", generation=1)
        q2 = TaskQueue(self.db)         # 재부팅 reconcile
        st = q2.get_stop_epoch()
        self.assertTrue(st["paused"], "더 높은 db epoch(paused)가 승자")
        self.assertEqual(st["epoch"], 2)

    def test_reconcile_same_epoch_conflict_failclosed(self):
        """같은 epoch인데 db=unpaused, pause.json=paused → 화해불가 → fail-closed paused."""
        q = TaskQueue(self.db)
        # db를 epoch3 unpaused로 만든다
        q.set_stop_epoch(True)          # 1
        q.set_stop_epoch(False)         # 2 unpaused
        q.set_stop_epoch(False)         # 3 unpaused
        # pause.json을 같은 epoch3 paused로
        pausemod.set_pause(self.dir, True, scope="project", incident="alfred#39", generation=3)
        q2 = TaskQueue(self.db)
        st = q2.get_stop_epoch()
        self.assertTrue(st["paused"], "같은 epoch 상태불일치는 fail-closed로 paused")
        self.assertIn("CONFLICT", (st["note"] or ""))


class TestResumeCAS(Base):
    def test_resume_stale_rejected(self):
        q = TaskQueue(self.db)
        e = q.set_stop_epoch(True, incident="alfred#39")   # epoch1 paused
        # stale: generation <= 현재 epoch → 거부
        res = q.resume_stop(generation=e)
        self.assertFalse(res["resumed"])
        self.assertTrue(q.get_stop_epoch()["paused"])

    def test_resume_newer_generation_succeeds(self):
        q = TaskQueue(self.db)
        e = q.set_stop_epoch(True, incident="alfred#39")   # epoch1
        res = q.resume_stop(generation=e + 1)
        self.assertTrue(res["resumed"])
        self.assertEqual(res["epoch"], e + 1)
        self.assertFalse(q.get_stop_epoch()["paused"])
        # resume 후 enqueue 가능
        self.assertEqual(q.enqueue(_task()), "t1")

    def test_resume_does_not_override_newer_stop(self):
        """오래된 resume이 그 사이 올라온 새 STOP을 덮지 못한다."""
        q = TaskQueue(self.db)
        q.set_stop_epoch(True, incident="alfred#39")       # epoch1
        # 관측자는 epoch1을 보고 resume gen2를 시도하려는데, 그 사이 새 STOP이 epoch2로 올라감
        q.set_stop_epoch(True, incident="alfred#39")       # epoch2 (newer STOP)
        res = q.resume_stop(generation=2)                  # gen2 <= 현재 epoch2 → 거부
        self.assertFalse(res["resumed"])
        self.assertTrue(q.get_stop_epoch()["paused"])


class TestCascadeOutbox(Base):
    """§3/§4: result 저장과 cascade_outbox 원자 기록 + lease/CAS executor primitive."""

    def _seed(self, q, tid="t1"):
        q.enqueue(_task(tid))
        q.dequeue(role="implementer")   # pending→in_progress
        return TaskResult(task_id=tid, status="completed", summary="done")

    def test_outbox_applied_when_unpaused(self):
        q = TaskQueue(self.db)
        q.submit_result("t1", self._seed(q))
        ob = q.outbox_get("t1")
        self.assertIsNotNone(ob)
        self.assertEqual(ob["state"], "applied")   # 라이브 처리
        self.assertIn("completed", ob["result_json"])   # result 전체 보존

    def test_outbox_pending_when_paused(self):
        q = TaskQueue(self.db)
        r = self._seed(q)
        q.set_stop_epoch(True, incident="alfred#39")
        q.submit_result("t1", r)
        ob = q.outbox_get("t1")
        self.assertEqual(ob["state"], "pending", "STOP 중이면 억제(pending) — executor가 재개 후 drain")
        self.assertEqual(ob["stop_epoch"], q.get_stop_epoch()["epoch"])

    def test_outbox_atomic_with_result(self):
        """result 저장과 outbox 기록은 같은 txn — 하나 있으면 반드시 다른 하나도 있다."""
        q = TaskQueue(self.db)
        r = self._seed(q)
        q.set_stop_epoch(True)
        q.submit_result("t1", r)
        # 결과가 저장됐으면 outbox도 반드시 존재(원자성)
        self.assertIsNotNone(q.get_result("t1"))
        self.assertIsNotNone(q.outbox_get("t1"))

    def test_outbox_claim_lease_and_mark(self):
        q = TaskQueue(self.db)
        r = self._seed(q); q.set_stop_epoch(True); q.submit_result("t1", r)
        c1 = q.outbox_claim("t1", "owner-a")
        self.assertIsNotNone(c1)
        self.assertEqual(c1["state"], "replaying")
        # 아직 만료 안 된 lease → 다른 owner claim 불가(동시 replay dedup)
        self.assertIsNone(q.outbox_claim("t1", "owner-b"))
        # 다른 owner의 mark_applied는 무효(자기 lease 아님)
        self.assertFalse(q.outbox_mark_applied("t1", "owner-b"))
        # 정당 owner mark → applied
        self.assertTrue(q.outbox_mark_applied("t1", "owner-a"))
        self.assertEqual(q.outbox_get("t1")["state"], "applied")

    def test_outbox_reopen_for_enqueue_race(self):
        """라이브 cascade 중 STOP으로 successor enqueue가 거부되면 부모 outbox(applied)를 reopen →
        pending → 재개 replay가 저장된 result로 전체 cascade 멱등 재실행(거부된 successor 복구)."""
        q = TaskQueue(self.db)
        q.submit_result("t1", self._seed(q))   # unpaused → applied(라이브 처리)
        self.assertEqual(q.outbox_get("t1")["state"], "applied")
        self.assertTrue(q.outbox_reopen("t1"))
        self.assertEqual(q.outbox_get("t1")["state"], "pending")
        self.assertIn("t1", [r["parent_task_id"] for r in q.outbox_pending()])
        # result_json은 보존돼 result-carrying replay 가능(리뷰어 지적 해소)
        self.assertIn("completed", q.outbox_get("t1")["result_json"])
        # 이미 pending이면 reopen no-op(applied/replaying만 대상)
        self.assertFalse(q.outbox_reopen("t1"))

    def test_outbox_stale_lease_reclaim(self):
        """crash한 replaying(만료 lease)은 다른 owner가 안전하게 reclaim."""
        q = TaskQueue(self.db)
        r = self._seed(q); q.set_stop_epoch(True); q.submit_result("t1", r)
        q.outbox_claim("t1", "dead-owner", ttl=-1)   # 즉시 만료된 lease(=crash 가정)
        c = q.outbox_claim("t1", "owner-b")
        self.assertIsNotNone(c, "만료 lease는 reclaim 가능")
        self.assertEqual(c["lease_owner"], "owner-b")
        # dead-owner는 더 이상 mark 못 함(회수됨)
        self.assertFalse(q.outbox_mark_applied("t1", "dead-owner"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
