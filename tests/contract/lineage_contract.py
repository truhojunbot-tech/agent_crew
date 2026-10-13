"""Shared assertions for the server-owned task lineage (#773)."""

from agent_crew.queue import TaskQueue


def assert_lineage_contract(db, root_id, intent):
    """Check every descendant's identity, authority and claimable role."""
    rows = {task.task_id: task for task in TaskQueue(str(db)).list_tasks()}
    assert root_id in rows
    lineage = {}
    for task in rows.values():
        seen = set()
        current = task
        while current.task_id not in seen:
            seen.add(current.task_id)
            if current.task_id == root_id:
                lineage[task.task_id] = task
                break
            parent_id = (current.context or {}).get("prev_task_id")
            if not parent_id:
                parent_id = (current.context or {}).get("original_task_id")
            assert parent_id in rows, (task.task_id, parent_id)
            current = rows[parent_id]
    assert lineage
    role_map = intent["role_map"]
    assert intent["reviewer"] != intent["implementer"]
    for task in lineage.values():
        ctx = task.context or {}
        assert task.branch == intent["branch"], task.task_id
        assert ctx.get("repo") == intent["repo"], task.task_id
        assert ctx.get("issue") == intent["issue"], task.task_id
        assert ctx.get("base_branch") == intent["base_branch"], task.task_id
        assert ctx.get("coordinator_managed") == intent["coordinator_managed"], task.task_id
        assert ctx.get("lineage_root_task_id", root_id) == root_id, task.task_id
        assert ctx.get("implementer_agent") == intent["implementer"], task.task_id
        assert ctx.get("reviewer_agent") == intent["reviewer"], task.task_id
        expected_role = {"implement": "implementer", "review": "reviewer",
                         "test": "tester"}[task.task_type]
        expected_agent = (intent["implementer"] if expected_role == "implementer" else
                          intent["reviewer"] if expected_role == "reviewer" else
                          role_map["tester"])
        assert ctx.get("agent_override", role_map[expected_role]) == expected_agent, task.task_id
        assert expected_agent in role_map.values(), task.task_id
        assert task.status != "pending" or expected_agent in role_map.values(), task.task_id
    return lineage
