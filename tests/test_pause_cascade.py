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
        # #314 §3/§4: 억제 durable 기록은 cascade_outbox(state=pending) — result 저장과 원자적.
        ob = self.q.outbox_get(t.task_id)
        self.assertIsNotNone(ob)
        self.assertEqual(ob["state"], "pending")

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


class TestDeterministicRace(Base):
    def test_in_transaction_recheck_is_load_bearing(self):
        # 사전체크는 통과(False)하지만 임계구역 진입 후 STOP이 authoritative(True)가 되는
        # 결정론적 interleaving → dequeue는 None이어야(사전체크만으론 안 됨을 증명).
        self.q.enqueue(mk(1, "implement"))   # 미pause 상태서 enqueue
        calls = {"n": 0}
        orig = pause.is_paused
        def flip(*a, **k):
            calls["n"] += 1
            return calls["n"] > 1            # 1st(사전) False, 2nd+(임계구역내) True
        pause.is_paused = flip
        try:
            self.assertIsNone(self.q.dequeue(role="implementer"),
                              "사전체크 통과 후 STOP → 임계구역 recheck가 claim을 막아야")
            self.assertGreaterEqual(calls["n"], 2, "임계구역 내부 recheck가 실제 호출돼야")
        finally:
            pause.is_paused = orig

    def test_enqueue_race_raises_pausederror(self):
        from agent_crew.queue import PausedError
        pause.set_pause(self.sd, True, source="test")  # 임계구역서 pause 감지
        with self.assertRaises(PausedError):
            self.q.enqueue(mk(2, "implement"))

    def test_review_request_changes_under_pause_no_fix(self):
        # cascade family: review request_changes → fix. pause면 fix 미enqueue.
        self.q.enqueue(mk(3, "review"))
        t = self.q.dequeue(role="reviewer")
        pause.set_pause(self.sd, True, source="test")
        c = self._client()
        r = c.post(f"/tasks/{t.task_id}/result",
                   json={"task_id": t.task_id, "status": "completed", "verdict": "request_changes",
                         "summary": "needs work", "findings": ["x"]})
        self.assertTrue(r.json().get("suppressed_by_pause"))
        self.assertEqual(len([x for x in self.q.list_tasks() if x.task_type == "implement"]), 0)


class TestResumeReplay(Base):
    def test_replay_after_resume_creates_successor_once(self):
        # in-flight impl → pause → result 억제(record) → resume → replay → review 1회 생성 → 재replay no-op
        self.q.enqueue(mk(1, "implement"))
        t = self.q.dequeue(role="implementer")
        pause.set_pause(self.sd, True, source="test", incident="alfred#39")
        c = self._client()
        r = c.post(f"/tasks/{t.task_id}/result",
                   json={"task_id": t.task_id, "status": "completed", "summary": "done"})
        self.assertTrue(r.json().get("suppressed_by_pause"))
        self.assertEqual(len([x for x in self.q.list_tasks() if x.task_type == "review"]), 0)
        # resume(더 높은 generation) — #314: DB 권위(runtime_stop)까지 해제해야 STOP이 풀린다.
        # 서버 TaskQueue가 paused 상태에서 부팅되며 boot reconcile이 runtime_stop을 paused로 seeding하므로,
        # pause.json만 푸는 것으로는 부족하다(fail-closed로 DB paused 승). 프로덕션 cli resume은
        # resume_stop(DB)+pause.json 미러를 함께 수행한다 — 여기서도 동일하게 둘 다 해제한다.
        cur = pause._load(os.path.join(self.sd, "pause.json"))
        pause.resume(self.sd, generation=cur["generation"] + 1, source="test")
        self.q.resume_stop(generation=self.q.get_stop_epoch()["epoch"] + 1, who="owner:test", decision_id="D-51-9")
        self.assertFalse(pause.is_paused(self.sd))
        self.assertFalse(self.q.get_stop_epoch()["paused"])
        # replay
        rr = c.post("/admin/replay-suppressed")
        self.assertEqual(rr.status_code, 200)
        self.assertIn(t.task_id, rr.json().get("replayed", []))
        n_review = len([x for x in self.q.list_tasks() if x.task_type == "review"])
        self.assertEqual(n_review, 1, "resume 후 억제된 successor가 1회 생성")
        # 재replay → 중복 생성 없음(at most once)
        rr2 = c.post("/admin/replay-suppressed")
        self.assertEqual(rr2.json().get("replayed", []), [])
        self.assertEqual(len([x for x in self.q.list_tasks() if x.task_type == "review"]), 1)


class TestReplaySideEffectBoundary(Base):
    def test_replay_creates_successor_but_skips_push(self):
        """#314 §4 P0: replay는 durable cascade transition(successor enqueue)만 재실행하고
        non-idempotent side effect(queue push)는 skip한다 — replay가 다른 pending task를
        claim/start하는 중복을 막는다."""
        from fastapi.testclient import TestClient
        from agent_crew.server import create_app
        pushes = []
        app = create_app(db_path=self.db, push_fn=lambda *a, **k: pushes.append(a),
                         project="testproj")
        c = TestClient(app); c.__enter__()
        self.addCleanup(lambda: c.__exit__(None, None, None))

        self.q.enqueue(mk(1, "implement"))
        t = self.q.dequeue(role="implementer")
        pause.set_pause(self.sd, True, source="test", incident="alfred#39")
        r = c.post(f"/tasks/{t.task_id}/result",
                   json={"task_id": t.task_id, "status": "completed", "summary": "done"})
        self.assertTrue(r.json().get("suppressed_by_pause"))
        pushes_at_suppress = len(pushes)   # 억제 경로도 push 안 함

        # resume (pause.json + DB 권위)
        cur = pause._load(os.path.join(self.sd, "pause.json"))
        pause.resume(self.sd, generation=cur["generation"] + 1, source="test")
        self.q.resume_stop(generation=self.q.get_stop_epoch()["epoch"] + 1, who="owner:test", decision_id="D-51-9")

        rr = c.post("/admin/replay-suppressed")
        self.assertEqual(rr.status_code, 200)
        # durable transition: review successor 1개 생성
        self.assertEqual(len([x for x in self.q.list_tasks() if x.task_type == "review"]), 1)
        # side-effect boundary: replay 동안 queue push는 발생하지 않음
        self.assertEqual(len(pushes), pushes_at_suppress,
                         "replay는 queue push를 하지 않아야(다른 task claim/start 방지)")


class TestReviewCommentReconciliation(Base):
    def test_crash_after_post_before_done_no_double_post(self):
        """#314 재리뷰: review comment 게시 성공→external_op done 기록 전 crash로 reserved만 남은 뒤
        동일 result 재진입 시, GitHub에 이미 댓글(stable marker)이 있으면 재게시하지 않고 done으로 화해."""
        import agent_crew.github as gh
        posts = []
        orig_post, orig_has = gh.post_review_comment, gh.pr_has_comment_containing
        gh.post_review_comment = lambda **k: (posts.append(k), True)[1]
        gh.pr_has_comment_containing = lambda *a, **k: True   # GitHub엔 이미 댓글 존재(게시됐었음)
        try:
            c = self._client()
            self.q.enqueue(TaskRequest(task_id="rev-x", task_type="review", description="r",
                                       branch="main", priority=1,
                                       # #330: repo identity is now resolved explicitly
                                       # (ctx.repo, else reviewer worktree) and fails
                                       # closed otherwise; supply it so this test keeps
                                       # exercising reconciliation, not the new gate.
                                       context={"pr_number": 77, "repo": "truhojunbot-tech/agent_crew"},
                                       project="testproj"))
            self.q.dequeue(role="reviewer")
            # crash 시뮬레이션: 게시는 됐지만 done 기록 전 죽어 reserved만 남은 상태
            self.q.external_op_reserve("comment:review:rev-x", pr_number=77)
            r = c.post("/tasks/rev-x/result",
                       json={"task_id": "rev-x", "status": "completed", "verdict": "approve",
                             "summary": "lgtm", "pr_number": 77})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(posts), 0, "이미 있는 댓글 → 재게시 안 함(reconciliation)")
            self.assertEqual(self.q.external_op_get("comment:review:rev-x")["state"], "done",
                             "화해 후 done으로 마감")
        finally:
            gh.post_review_comment, gh.pr_has_comment_containing = orig_post, orig_has

    def test_reconcile_unknown_fails_closed_no_post(self):
        """존재 확인 unknown(None)이면 feedback mutation 특성상 재게시하지 않는다(fail-closed)."""
        import agent_crew.github as gh
        posts = []
        orig_post, orig_has = gh.post_review_comment, gh.pr_has_comment_containing
        gh.post_review_comment = lambda **k: (posts.append(k), True)[1]
        gh.pr_has_comment_containing = lambda *a, **k: None    # 확인 불가
        try:
            c = self._client()
            self.q.enqueue(TaskRequest(task_id="rev-y", task_type="review", description="r",
                                       branch="main", priority=1,
                                       # #330: see test_crash_after_post_before_done_no_double_post
                                       context={"pr_number": 78, "repo": "truhojunbot-tech/agent_crew"},
                                       project="testproj"))
            self.q.dequeue(role="reviewer")
            self.q.external_op_reserve("comment:review:rev-y", pr_number=78)  # 기존 reserved
            r = c.post("/tasks/rev-y/result",
                       json={"task_id": "rev-y", "status": "completed", "verdict": "approve",
                             "summary": "lgtm", "pr_number": 78})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(posts), 0, "unknown → fail-closed 미게시")
            self.assertEqual(self.q.external_op_get("comment:review:rev-y")["state"], "reserved",
                             "미게시 → done 안 됨(다음 재확인 대기)")
        finally:
            gh.post_review_comment, gh.pr_has_comment_containing = orig_post, orig_has


class TestAtomicClaim(Base):
    def test_pause_set_blocks_claim_in_transaction(self):
        # 사전체크를 우회하더라도 임계구역 재확인이 claim을 막는다(간이 검증: pause 상태서 dequeue None)
        self.q.enqueue(mk(4, "implement"))
        pause.set_pause(self.sd, True, source="test")
        self.assertIsNone(self.q.dequeue(role="implementer"))
        # 트랜잭션 내부 재확인 코드 경로 존재 확인 (#314: runtime_stop 권위 in-txn 게이트)
        import inspect
        src = inspect.getsource(TaskQueue.dequeue)
        self.assertIn("_stop_active_in_txn", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRequiredRegressionsNotYetNamed(Base):
    """#313's required regressions 4, 5 and 9, which had no named counterpart.

    ⛔Written as probes FIRST, against unmodified main: all three passed, which
      is the evidence that #313's behaviour was already delivered by #314's
      outbox/epoch work rather than an assumption that it was. They are kept
      because the issue asks for them by name — a behaviour nobody tests is one
      refactor from being lost.
    """

    def _inflight(self, tid="t1", tt="implement", ctx=None):
        self.q.enqueue(TaskRequest(task_id=tid, task_type=tt, description="d",
                                   branch="main", priority=1,
                                   context=ctx or {}, project="testproj"))
        got = self.q.dequeue(role={"implement": "implementer",
                                   "review": "reviewer"}.get(tt, "implementer"))
        self.assertIsNotNone(got, "fixture did not claim the task")
        return got

    def test_4_failed_result_under_pause_creates_no_replacement(self):
        """retry / provider-fallback are replacement work — requirement 2."""
        self._inflight()
        pause.set_pause(self.sd, True, reason="incident", incident="alfred#39")
        before = {t.task_id for t in self.q.list_tasks()}
        r = self._client().post("/tasks/t1/result", json={
            "task_id": "t1", "status": "failed", "summary": "rate limit"})
        self.assertEqual(r.status_code, 200, r.text)
        after = {t.task_id for t in TaskQueue(self.db).list_tasks()}
        self.assertEqual(after, before, f"replacement work created under STOP: {after - before}")

    def test_5_discuss_result_under_pause_creates_no_successor(self):
        """The discuss path is an alternate lane; one ungated lane is a bypass."""
        self.q.enqueue(TaskRequest(task_id="d1", task_type="discuss", description="topic",
                                   branch="main", priority=1,
                                   context={"agent": "claude"}, project="testproj"))
        self.assertIsNotNone(self.q.dequeue_discuss_for_agent("claude"))
        pause.set_pause(self.sd, True, reason="incident")
        before = {t.task_id for t in self.q.list_tasks()}
        r = self._client().post("/tasks/d1/result", json={
            "task_id": "d1", "status": "completed", "summary": "done"})
        self.assertEqual(r.status_code, 200, r.text)
        after = {t.task_id for t in TaskQueue(self.db).list_tasks()}
        self.assertEqual(after, before, f"discuss created a successor under STOP: {after - before}")

    def test_9_restart_while_paused_keeps_the_cascade_suppressed(self):
        """Distinct from restart-preserves-pause: this asserts the CASCADE stays
        suppressed across the restart, not merely that the claim gate does."""
        self._inflight()
        pause.set_pause(self.sd, True, reason="incident")
        first = self._client()
        first.__exit__(None, None, None)          # restart boundary
        r = self._client().post("/tasks/t1/result", json={
            "task_id": "t1", "status": "completed", "summary": "done"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._review_count(), 0,
                         "cascade ran after a restart while paused")


class TestMcpTransportParity(Base):
    """★★The gap this task actually found. #313 requires EVERY alternate path.

    The queue refuses a successor enqueue atomically with `PausedError`. HTTP
    registers an exception handler that reopens the parent's outbox row and
    answers 200 with `suppressed_by_pause`. MCP had no handling at all, so the
    same STOP raised out of `submit_result`: the durable state was already safe
    — result stored, outbox continuation present, no successor — but the worker
    saw a failed submission rather than a suppression and could not tell them
    apart.

    ⛔The guard was there; the CONTRACT was not. Same transport-parity rule this
      repo keeps relearning (#123, #302, #305).
    """

    def _mcp(self, tool, **kw):
        import asyncio

        from agent_crew.mcp_server import build_mcp_server

        fn = build_mcp_server(self.db)._tool_manager._tools[tool].fn
        return asyncio.run(fn(**kw)) if asyncio.iscoroutinefunction(fn) else fn(**kw)

    def test_mcp_result_under_pause_acknowledges_instead_of_raising(self):
        self.q.enqueue(TaskRequest(task_id="t1", task_type="implement", description="d",
                                   branch="main", priority=1, context={}, project="testproj"))
        self.assertIsNotNone(self.q.dequeue(role="implementer"))
        pause.set_pause(self.sd, True, reason="incident", incident="alfred#39")

        ack = self._mcp("submit_result", task_id="t1", status="completed", summary="done")
        self.assertTrue(ack.get("acknowledged"), ack)
        self.assertTrue(ack.get("suppressed_by_pause"), ack)
        self.assertEqual(self._review_count(), 0, "MCP cascade ran under STOP")

    def test_mcp_suppression_keeps_the_lineage_replayable(self):
        """⛔The reason the contract matters: the parent's outbox row must stay
        pending so resume replays the stored result exactly once."""
        self.q.enqueue(TaskRequest(task_id="t1", task_type="implement", description="d",
                                   branch="main", priority=1, context={}, project="testproj"))
        self.q.dequeue(role="implementer")
        pause.set_pause(self.sd, True, reason="incident")
        self._mcp("submit_result", task_id="t1", status="completed", summary="done")

        q2 = TaskQueue(self.db)
        self.assertIsNotNone(q2.get_result("t1"), "the result itself was lost")
        pending = [r.get("parent_task_id")
                   for r in q2.outbox_pending(include_replaying=True)]
        self.assertIn("t1", pending, "the suppressed cascade is not replayable")

    def _outbox_state(self, parent):
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT state FROM cascade_outbox WHERE parent_task_id=?",
                               (parent,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_mcp_mid_cascade_stop_reopens_the_applied_outbox_row(self):
        """★★The actual production race, and the only case where `outbox_reopen`
        is load-bearing. Caught by review of PR #335.

        ⛔My first attempt at this test was worthless and I drew a false
          conclusion from it. It called `outbox_claim` BEFORE `submit_result` —
          but `submit_result` is the operation that INSERTs the cascade_outbox
          row (queue.py #314 §3), so there was nothing to claim, and the
          "row left pending" assertion passed because no row existed at all.
          It then set the pause before submitting, so the row was BORN
          `pending`, and `outbox_reopen` — whose WHERE clause only matches
          `state IN ('applied','replaying')` — could never do anything. From
          that I concluded the reopen was redundant defence-in-depth and wrote
          it into the docstring. That conclusion was wrong.

        The row's state is decided atomically from the STOP observed at
        result-commit time: unpaused → `applied` (a live cascade handles it
        synchronously), paused → `pending` (the executor drains it later). So
        the dangerous interleaving is the one where the result commits while
        UNPAUSED — row `applied` — and STOP only becomes authoritative before
        the successor enqueue. Without the reopen that `applied` row is never
        replayed, and the refused successor is lost permanently.

        Forced deterministically by making STOP authoritative inside the
        cascade, between the result commit and the enqueue. The `PausedError`
        is then raised by the real queue, not simulated.
        """
        from unittest import mock

        import agent_crew.mcp_server as mcp_server

        self.q.enqueue(TaskRequest(task_id="t1", task_type="implement", description="d",
                                   branch="main", priority=1, context={}, project="testproj"))
        self.q.dequeue(role="implementer")

        _real = mcp_server.auto_enqueue_review

        def _stop_lands_mid_cascade(queue, task_id, **kw):
            queue.set_stop_epoch(True, incident="alfred#39")
            return _real(queue, task_id, **kw)

        with mock.patch.object(mcp_server, "auto_enqueue_review", _stop_lands_mid_cascade):
            ack = self._mcp("submit_result", task_id="t1", status="completed", summary="done")

        self.assertTrue(ack.get("acknowledged"), ack)
        self.assertTrue(ack.get("suppressed_by_pause"), ack)
        self.assertEqual(self._review_count(), 0, "a successor survived the STOP")
        self.assertEqual(self._outbox_state("t1"), "pending",
                         "the applied outbox row was not reopened — the refused "
                         "successor can never be replayed")

        # Valid resume → the stored result replays and the lineage advances ONCE.
        q2 = TaskQueue(self.db)
        q2.resume_stop(generation=int(q2.get_stop_epoch()["epoch"]) + 1,
                       who="owner:test", decision_id="D-51-9")
        c = self._client()
        self.assertEqual(c.post("/admin/replay-suppressed").status_code, 200)
        self.assertEqual(self._review_count(), 1,
                         "the suppressed lineage did not advance after resume")

        # ⛔Exactly once: a second drain must not duplicate the successor.
        c.post("/admin/replay-suppressed")
        self.assertEqual(self._review_count(), 1, "replay duplicated the successor")

    def test_mcp_claim_is_blocked_under_pause(self):
        self.q.enqueue(TaskRequest(task_id="t2", task_type="implement", description="d",
                                   branch="main", priority=1, context={}, project="testproj"))
        pause.set_pause(self.sd, True, reason="incident")
        got = self._mcp("get_next_task", role="implementer")
        self.assertFalse(got and got.get("task_id"), f"MCP claimed under STOP: {got}")

    def test_mcp_cascade_runs_normally_when_not_paused(self):
        """⛔The control. Swallowing PausedError must not become swallowing the
        cascade."""
        self.q.enqueue(TaskRequest(task_id="t1", task_type="implement", description="d",
                                   branch="main", priority=1, context={}, project="testproj"))
        self.q.dequeue(role="implementer")
        ack = self._mcp("submit_result", task_id="t1", status="completed", summary="done")
        self.assertTrue(ack.get("acknowledged"), ack)
        self.assertFalse(ack.get("suppressed_by_pause"))
        self.assertEqual(self._review_count(), 1, "the normal MCP cascade stopped working")

