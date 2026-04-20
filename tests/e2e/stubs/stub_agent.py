#!/usr/bin/env python3
"""
Stub agent for e2e tests.

Environment variables:
  STUB_PORT      TCP port of the task server (required)
  STUB_ROLE      coder | reviewer | tester  (required)
  STUB_VERDICT   approve | request_changes  (optional, only used by reviewer)
  STUB_STATUS    completed | failed         (default: completed)
  STUB_TIMEOUT   poll timeout in seconds    (default: 30)
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
BASE_URL = f"http://127.0.0.1:{PORT}"


def poll_task():
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        url = f"{BASE_URL}/tasks/next?role={ROLE}"
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                data = json.loads(resp.read())
                if data is not None:
                    return data
        except Exception:
            pass
        time.sleep(0.1)
    return None


def submit_result(task_id):
    result = {
        "task_id": task_id,
        "status": STATUS,
        "summary": f"stub {ROLE} done",
        "verdict": VERDICT,
        "findings": [],
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
