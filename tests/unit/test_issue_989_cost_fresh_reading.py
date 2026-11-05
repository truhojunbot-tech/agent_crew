"""#989: a stale after-reading at settle_end keeps the cost pending, not UNKNOWN."""

import json
from types import SimpleNamespace

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue

SETTLE_END = 1910.0  # ended 1010 + settle 900


def _setup(tmp_path, monkeypatch, shadow):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task_type = "review" if shadow else "implement"
    queue.enqueue(TaskRequest(task_id="target", task_type=task_type,
                              description="Cloud task", branch="agent/target",
                              project="agent_crew", context={"repo": "owner/repo"}))
    prefix = "cloud_shadow_" if shadow else "cloud_"
    queue.patch_context("target", {
        prefix + "attempt_started_at": 1000.0,
        prefix + "ended_at": 1010.0,
        prefix + "credit_used_before": 10.0,
        prefix + "credit_before_fetched_at": 999.0,
    })
    attribution_id = "cloud-shadow:target" if shadow else "target"
    queue.record_attribution(task_id=attribution_id, agent="claude_cloud",
                             role="reviewer" if shadow else "implementer",
                             task_type=task_type, started_at=1000.0)
    if not shadow:
        queue.cancel("target")
    now = [1010.0]
    snapshot = [SimpleNamespace(used_dollars=10.0, fetched_at=1005.0)]
    monkeypatch.setattr(cloud, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: snapshot[0])
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_SETTLE_SECONDS", "900")
    monkeypatch.delenv("AGENT_CREW_CLOUD_COST_FRESH_WAIT_SECONDS", raising=False)
    monkeypatch.setenv("AGENT_CREW_CLOUD_UNKNOWN_SESSION_USD", "2.0")

    def tick():
        if shadow:
            cloud._record_shadow_review_cost(queue, "target")
        else:
            cloud.record_terminal_cloud_cost(queue, "target")

    finalized_key = prefix + "cost_finalized_at"
    tick()  # opens the PENDING_SETTLE row
    return queue, attribution_id, now, snapshot, tick, finalized_key


@pytest.mark.parametrize("shadow", [False, True], ids=["implement", "shadow"])
def test_stale_after_reading_keeps_row_pending_then_measures(tmp_path, monkeypatch, shadow):
    queue, attr_id, now, snapshot, tick, finalized_key = _setup(tmp_path, monkeypatch, shadow)
    # (1) live impl-986b shape: reading fetched before settle_end
    now[0] = SETTLE_END + 1
    snapshot[0] = SimpleNamespace(used_dollars=11.0, fetched_at=SETTLE_END - 330)
    tick()
    attr = queue.get_attribution(attr_id)
    assert attr["cloud_cost_status"] == "PENDING_SETTLE"
    assert attr["cloud_usd"] is None
    assert not queue.get_task_context("target").get(finalized_key)
    # (4) cap counts the pending row at the estimate
    assert cloud.cloud_usd_spent_today(queue, 0.0) == (0.0, 2.0)

    # (2) a later tick sees a refreshed cache -> MEASURED after - before
    now[0] = SETTLE_END + 1000
    snapshot[0] = SimpleNamespace(used_dollars=12.33, fetched_at=SETTLE_END + 990)
    tick()
    attr = queue.get_attribution(attr_id)
    assert attr["cloud_cost_status"] == "MEASURED"
    assert attr["cloud_usd"] == pytest.approx(2.33)
    assert queue.get_task_context("target").get(finalized_key)
    # (4) ...then at the measured value
    measured, estimated = cloud.cloud_usd_spent_today(queue, 0.0)
    assert (measured, estimated) == (pytest.approx(2.33), 0.0)


@pytest.mark.parametrize("shadow", [False, True], ids=["implement", "shadow"])
def test_still_stale_past_bound_is_unknown(tmp_path, monkeypatch, shadow):
    queue, attr_id, now, snapshot, tick, finalized_key = _setup(tmp_path, monkeypatch, shadow)
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_FRESH_WAIT_SECONDS", "600")
    snapshot[0] = SimpleNamespace(used_dollars=11.0, fetched_at=SETTLE_END - 1)
    now[0] = SETTLE_END + 599
    tick()
    assert queue.get_attribution(attr_id)["cloud_cost_status"] == "PENDING_SETTLE"
    # (3) past settle_end + bound: finalize as before
    now[0] = SETTLE_END + 600
    tick()
    attr = queue.get_attribution(attr_id)
    assert attr["cloud_cost_status"] == "UNKNOWN_NO_FRESH_READING"
    assert attr["cloud_usd"] is None
    assert queue.get_task_context("target").get(finalized_key)
    assert cloud.cloud_usd_spent_today(queue, 0.0) == (0.0, 2.0)


@pytest.mark.parametrize("shadow", [False, True], ids=["implement", "shadow"])
def test_missing_after_reading_also_waits(tmp_path, monkeypatch, shadow):
    queue, attr_id, now, snapshot, tick, _ = _setup(tmp_path, monkeypatch, shadow)
    snapshot[0] = None  # cache file older than the reader's max age
    now[0] = SETTLE_END + 1
    tick()
    assert queue.get_attribution(attr_id)["cloud_cost_status"] == "PENDING_SETTLE"


def test_overlap_still_finalizes_at_settle_end(tmp_path, monkeypatch):
    queue, attr_id, now, snapshot, tick, _ = _setup(tmp_path, monkeypatch, False)
    queue.enqueue(TaskRequest(task_id="other", task_type="implement",
                              description="Other", branch="agent/other",
                              context={"cloud_attempt_started_at": 1020.0,
                                       "cloud_ended_at": 1030.0}))
    snapshot[0] = SimpleNamespace(used_dollars=11.0, fetched_at=SETTLE_END - 300)
    now[0] = SETTLE_END + 1
    tick()
    assert queue.get_attribution(attr_id)["cloud_cost_status"] == "UNKNOWN_OVERLAP"


@pytest.mark.parametrize("raw,expected", [("", 1800.0), ("60", 60.0), ("-1", 1800.0),
                                          ("x", 1800.0), ("inf", 1800.0)])
def test_fresh_wait_env(monkeypatch, raw, expected):
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_FRESH_WAIT_SECONDS", raw)
    assert cloud.cloud_cost_fresh_wait_seconds() == expected


@pytest.mark.parametrize("shadow", [False, True], ids=["implement", "shadow"])
def test_session_launched_during_fresh_wait_is_overlap(tmp_path, monkeypatch, shadow):
    """Review of PR #995: the delayed after-reading covers sessions that ran
    after settle_end, so overlap must be checked up to that reading."""
    queue, attr_id, now, snapshot, tick, _ = _setup(tmp_path, monkeypatch, shadow)
    snapshot[0] = SimpleNamespace(used_dollars=11.0, fetched_at=SETTLE_END - 300)
    now[0] = SETTLE_END + 1
    tick()
    assert queue.get_attribution(attr_id)["cloud_cost_status"] == "PENDING_SETTLE"
    queue.enqueue(TaskRequest(task_id="later", task_type="implement",
                              description="Later", branch="agent/later",
                              context={"cloud_attempt_started_at": 2000.0,
                                       "cloud_ended_at": 2100.0}))
    now[0] = 3000.0
    snapshot[0] = SimpleNamespace(used_dollars=15.0, fetched_at=2990.0)
    tick()
    attr = queue.get_attribution(attr_id)
    assert attr["cloud_cost_status"] == "UNKNOWN_OVERLAP"
    assert attr["cloud_usd"] is None
    assert json.loads(attr["cloud_cost_overlaps"]) == ["later"]
