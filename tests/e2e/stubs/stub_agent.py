#!/usr/bin/env python3
"""
Stub agent for e2e tests.

Environment variables:
  STUB_PORT      TCP port of the task server (required)
  STUB_ROLE      coder | reviewer | tester  (required)
  STUB_VERDICT   approve | request_changes  (optional, only used by reviewer)
  STUB_STATUS    completed | failed         (default: completed)
  STUB_TIMEOUT   poll timeout in seconds    (default: 30)
  STUB_TASK_ID   expected task ID           (optional; recovers timed-out claims)
"""
import json
import os
import sys
import time
import urllib.request

PORT = int(os.environ["STUB_PORT"])
ROLE = os.environ["STUB_ROLE"]
VERDICT = os.environ.get("STUB_VERDICT", "") or None
STATUS = os.environ.get("STUB_STATUS", "completed")
TIMEOUT = float(os.environ.get("STUB_TIMEOUT", "30"))
TASK_ID = os.environ.get("STUB_TASK_ID", "")
BASE_URL = f"http://127.0.0.1:{PORT}"


def poll_task():
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        if TASK_ID:
            # A timed-out /tasks/next response may still have committed its
            # claim. Recover only the exact task after dispatch is recorded;
            # a second poll cannot return that already-claimed row.
            try:
                with urllib.request.urlopen(
                    f"{BASE_URL}/tasks/{TASK_ID}", timeout=2.0,
                ) as resp:
                    current = json.loads(resp.read())
                execution = current.get("execution") or {}
                if (current.get("status") == "in_progress"
                        and execution.get("claimed_via") == "http_poll"
                        and execution.get("dispatch_channel") == "api"):
                    return current
            except Exception:
                pass
        url = f"{BASE_URL}/tasks/next?role={ROLE}"
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                data = json.loads(resp.read())
        except Exception:
            data = None
        if data is not None:
            if TASK_ID and data.get("task_id") != TASK_ID:
                raise RuntimeError(
                    f"stub {ROLE}: received {data['task_id']}, expected {TASK_ID}")
            return data
        time.sleep(0.1)
    return None


def submit_result(task_id):
    result = {
        "task_id": task_id,
        "status": STATUS,
        "summary": (
            f"Stub {ROLE} completed the assigned task and checked its result."
            if ROLE == "reviewer" else f"stub {ROLE} done"
        ),
        "verdict": VERDICT,
        "findings": (["The implementation needs changes before approval."]
                     if ROLE == "reviewer" and VERDICT == "request_changes" else []),
    }
    body = json.dumps(result).encode()
    url = f"{BASE_URL}/tasks/{task_id}/result"
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        resp.read()


task = poll_task()
if task is None:
    print(f"stub {ROLE}: no task found within {TIMEOUT}s", file=sys.stderr)
    sys.exit(1)

submit_result(task["task_id"])
print(f"stub {ROLE}: submitted result for {task['task_id']}")
