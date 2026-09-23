"""Regenerate `pre_g12_tasks.db`: a tasks DB written by the pre-G12 schema.

The fixture must come from the OLD code, not a hand-written CREATE TABLE, or
the migration test only proves the migration against our idea of the old
schema. From a checkout of this repo:

    git archive 5efea31 src | tar -x -C /tmp/g12_base
    PYTHONPATH=/tmp/g12_base/src python3 tests/fixtures/make_pre_g12_tasks_db.py

5efea31 is the base of the G12 branch; its `tasks` table has 17 columns, as in
every preserved SEV-0 DB (RECONCILIATION.md F9). All data is synthetic.
"""

import os
import sys

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pre_g12_tasks.db")
if os.path.exists(out):
    os.remove(out)
q = TaskQueue(out)
q.enqueue(TaskRequest(task_id="legacy-done", task_type="implement", description="finished",
                      branch="main", priority=2, context={"k": "v"}, project="legacy"))
q.enqueue(TaskRequest(task_id="legacy-running", task_type="review", description="running",
                      branch="feat", priority=3, context={}, project="legacy"))
q.enqueue(TaskRequest(task_id="legacy-pending", task_type="test", description="waiting",
                      branch="main", priority=1, context={}, project="legacy"))
assert q.dequeue(role="implementer").task_id == "legacy-done"
q.submit_result("legacy-done", TaskResult(task_id="legacy-done", status="completed",
                                          summary="ok", verdict=None, findings=[],
                                          pr_number=None))
assert q.dequeue(role="reviewer").task_id == "legacy-running"
q.set_push_at("legacy-running", 1234.5)
sys.stdout.write(f"wrote {out}\n")
