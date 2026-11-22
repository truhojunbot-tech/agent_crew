"""#1050: give a test row the dispatch provenance a real worker's row has.

The worker-facing result entry points (HTTP ``POST /tasks/{id}/result`` and
MCP ``submit_result``) refuse a result on a row that was never dispatched. A
harness that enqueues a row and posts its result straight away — or whose fake
dispatcher never reaches ``record_dispatch`` — calls this first.

It goes through ``TaskQueue.record_dispatch``, the production call that sets
``dispatched_at`` and writes the ``dispatched`` exec event; nothing is written
by hand.
"""

import os


def record_dispatch_provenance(queue, *task_ids, channel="api", agent="test-worker"):
    """Record a dispatch for each of ``task_ids``; returns their nonces.

    ``queue`` is a ``TaskQueue`` or a db path.
    """
    from agent_crew.queue import TaskQueue

    if isinstance(queue, (str, os.PathLike)):
        queue = TaskQueue(str(queue))
    return [queue.record_dispatch(task_id, channel=channel, agent=agent,
                                  target=f"test:{task_id}")
            for task_id in task_ids]
