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
from agent_crew.protocol import TaskRequest  # noqa: E402
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
