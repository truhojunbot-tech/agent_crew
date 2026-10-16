"""Cloud cost needs a settled credit snapshot and complete failure attribution."""

import json
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def test_credit_reader_keeps_snapshot_timestamp(tmp_path, monkeypatch):
    fetched_at = time.time() - 5
    credit = tmp_path / "credit.json"
    credit.write_text(json.dumps({"fetched_at": datetime.fromtimestamp(
        fetched_at, timezone.utc).isoformat(),
        "cloud_credit": {"used_dollars": 12.5}}))
    monkeypatch.setenv("AGENT_CREW_CLOUD_CREDIT_FILE", str(credit))
    snapshot = cloud.read_cloud_credit_used()
    assert snapshot.used_dollars == 12.5
    assert snapshot.fetched_at == pytest.approx(fetched_at)


def _task(queue, *, shadow=False):
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
    return attribution_id


@pytest.mark.parametrize("shadow", [False, True], ids=["normal", "shadow"])
def test_lagging_credit_stays_pending_then_measures_settled_delta(tmp_path, monkeypatch, shadow):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    attribution_id = _task(queue, shadow=shadow)
    now = [1010.0]
    monkeypatch.setattr(cloud, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_SETTLE_SECONDS", "900")
    snapshot = [SimpleNamespace(used_dollars=10.0, fetched_at=1010.0)]
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: snapshot[0])

    if shadow:
        cloud._record_shadow_review_cost(queue, "target")
    else:
        cloud.record_terminal_cloud_cost(queue, "target")
    attr = queue.get_attribution(attribution_id)
    assert attr["cloud_usd"] is None
    assert attr["cloud_cost_status"] == "PENDING_SETTLE"

    now[0] = 1910.0
    snapshot[0] = SimpleNamespace(used_dollars=10.8, fetched_at=1910.0)
    if shadow:
        cloud.reconcile_shadow_reviews(queue, pr_comments_fn=lambda *a, **k: [])
    else:
        cloud.reconcile_all_cloud_tasks(queue)
    attr = queue.get_attribution(attribution_id)
    assert attr["cloud_cost_status"] == "MEASURED"
    assert attr["cloud_usd"] == pytest.approx(0.8)


@pytest.mark.parametrize("shadow", [False, True], ids=["normal", "shadow"])
def test_settle_window_overlap_is_unknown(tmp_path, monkeypatch, shadow):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    attribution_id = _task(queue, shadow=shadow)
    queue.enqueue(TaskRequest(task_id="other", task_type="implement",
                              description="Other cloud task", branch="agent/other",
                              context={"cloud_attempt_started_at": 1020.0,
                                       "cloud_ended_at": 1030.0}))
    now = [1010.0]
    monkeypatch.setattr(cloud, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_SETTLE_SECONDS", "900")
    monkeypatch.setattr(cloud, "read_cloud_credit_used",
                        lambda: SimpleNamespace(used_dollars=10.8, fetched_at=1910.0))
    if shadow:
        cloud._record_shadow_review_cost(queue, "target")
    else:
        cloud.record_terminal_cloud_cost(queue, "target")
    now[0] = 1910.0
    if shadow:
        cloud.reconcile_shadow_reviews(queue, pr_comments_fn=lambda *a, **k: [])
    else:
        cloud.reconcile_all_cloud_tasks(queue)
    attr = queue.get_attribution(attribution_id)
    assert attr["cloud_cost_status"] == "UNKNOWN_OVERLAP"
    assert attr["cloud_usd"] is None
    assert json.loads(attr["cloud_cost_overlaps"]) == ["other"]


@pytest.mark.parametrize("shadow", [False, True], ids=["normal", "shadow"])
def test_no_fresh_snapshot_after_settle_is_unknown(tmp_path, monkeypatch, shadow):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    attribution_id = _task(queue, shadow=shadow)
    now = [1010.0]
    monkeypatch.setattr(cloud, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_SETTLE_SECONDS", "900")
    monkeypatch.setattr(cloud, "read_cloud_credit_used",
                        lambda: SimpleNamespace(used_dollars=10.0, fetched_at=1909.0))
    if shadow:
        cloud._record_shadow_review_cost(queue, "target")
    else:
        cloud.record_terminal_cloud_cost(queue, "target")
    now[0] = 1910.0
    if shadow:
        cloud.reconcile_shadow_reviews(queue, pr_comments_fn=lambda *a, **k: [])
    else:
        cloud.reconcile_all_cloud_tasks(queue)
    attr = queue.get_attribution(attribution_id)
    assert attr["cloud_cost_status"] == "UNKNOWN_NO_FRESH_READING"
    assert attr["cloud_usd"] is None


def test_before_snapshot_newer_than_start_cannot_measure(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue)
    queue.patch_context("target", {"cloud_credit_before_fetched_at": 1001.0})
    now = [1010.0]
    monkeypatch.setattr(cloud, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_SETTLE_SECONDS", "900")
    monkeypatch.setattr(cloud, "read_cloud_credit_used",
                        lambda: SimpleNamespace(used_dollars=10.8, fetched_at=1910.0))
    cloud.record_terminal_cloud_cost(queue, "target")
    now[0] = 1910.0
    cloud.reconcile_all_cloud_tasks(queue)
    assert queue.get_attribution("target")["cloud_cost_status"] == "UNKNOWN_NO_FRESH_READING"


def test_reconcile_relabels_legacy_zero_without_snapshot_proof(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="old", task_type="implement",
                              description="Old cloud task", branch="agent/old",
                              context={"cloud_attempt_started_at": 1000.0,
                                       "cloud_ended_at": 1010.0,
                                       "cloud_credit_used_before": 10.0,
                                       "cloud_credit_used_after": 10.0,
                                       "cloud_usd": 0.0,
                                       "cloud_cost_finalized_at": 1010.0}))
    queue.record_attribution(task_id="old", agent="claude_cloud", role="implementer",
                             task_type="implement", started_at=1000.0)
    queue.record_cloud_cost("old", 10.0, 10.0, 0.0, status="MEASURED", overlaps=[])
    queue.enqueue(TaskRequest(task_id="other", task_type="implement",
                              description="Overlapping task", branch="agent/other",
                              context={"cloud_attempt_started_at": 1020.0,
                                       "cloud_ended_at": 1030.0}))
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_SETTLE_SECONDS", "900")
    cloud.reconcile_all_cloud_tasks(queue)
    attr = queue.get_attribution("old")
    assert attr["cloud_usd"] is None
    assert attr["cloud_cost_status"] == "UNKNOWN_OVERLAP"


def test_preflight_failure_writes_one_unknown_attribution_row(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="preflight", task_type="implement",
                              description="Implement", branch="main",
                              project="agent_crew", context={"repo": "owner/repo"}))
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setattr("agent_crew.pipeline.auto_fallback_failed_task",
                        lambda *a, **k: None)

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        raise AssertionError("preflight launched cloud")

    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer",
                                            task_type="implement", run_fn=run)
    assert outcome.launch_error == "cloud_branch_unpublishable"
    with sqlite3.connect(queue._db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM task_attribution WHERE task_id='preflight'").fetchone()[0] == 1
    attr = queue.get_attribution("preflight")
    assert (attr["agent"], attr["role"], attr["outcome"]) == (
        "claude_cloud", "implementer", "failed")
    assert attr["cloud_cost_status"] == "UNKNOWN_NO_FRESH_READING"
    assert attr["uncached_input_tokens"] is None
    assert attr["output_tokens"] is None
