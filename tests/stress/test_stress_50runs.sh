#!/usr/bin/env bash
# stress/test_stress_50runs.sh — 50 task cycles, verify WATCHDOG TIMEOUT == 0
#
# Usage:
#   tests/stress/test_stress_50runs.sh [-n N] [-p PORT] [-h]
#
# Flags:
#   -n N     number of task cycles (default: 50)
#   -p PORT  server port (default: 18765)
#   -h       show help
#
# Exit codes:
#   0  PASS — zero WATCHDOG TIMEOUT in server log
#   1  FAIL — one or more WATCHDOG TIMEOUT found
#   2  ERROR — server failed to start
#
# No real agents, tmux panes, or LLM calls needed.
# The script starts a bare server and drives the full
# enqueue→dequeue→complete cycle via Python/curl.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
N=50
PORT=18765

usage() { sed -n '2,20p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) N="$2"; shift 2 ;;
    -p) PORT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 64 ;;
  esac
done

# --- temp workspace --------------------------------------------------------
WORK_DIR="$(mktemp -d -t crew-stress.XXXXXX)"
DB_FILE="${WORK_DIR}/stress.db"
LOG_FILE="${WORK_DIR}/server.log"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$WORK_DIR"' EXIT

echo "stress: N=${N}, port=${PORT}"
echo "stress: workdir=${WORK_DIR}"

# --- start server ----------------------------------------------------------
PYTHONPATH="${REPO_ROOT}/src" python3 \
  "${REPO_ROOT}/tests/stress/_server_launcher.py" \
  "$PORT" "$LOG_FILE" "$DB_FILE" &
SERVER_PID=$!

# wait for /health
READY=0
for _i in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.25
done

if [[ "$READY" -ne 1 ]]; then
  echo "ERROR: server did not become ready on port ${PORT}" >&2
  exit 2
fi
echo "stress: server ready"

# --- simulate N task cycles ------------------------------------------------
python3 - <<PYEOF
import sys, json, urllib.request, urllib.error, time

BASE = "http://127.0.0.1:${PORT}"
N = ${N}


def post(path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def get(path, params=""):
    url = BASE + path + (f"?{params}" if params else "")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


print(f"sim: enqueueing {N} tasks...")
for i in range(N):
    post("/tasks", {
        "task_id": f"stress-{i:03d}",
        "task_type": "implement",
        "description": f"Stress probe #{i} — complete immediately",
        "branch": "main",
        "priority": 3,
        "context": {"coordinator_managed": True},
    })

print(f"sim: draining {N} tasks...")
for cycle in range(N):
    task = get("/tasks/next", "role=coder")
    if task is None:
        print(f"sim: ERROR — GET /tasks/next returned null at cycle {cycle}", file=sys.stderr)
        sys.exit(1)
    task_id = task["task_id"]
    post(f"/tasks/{task_id}/result", {
        "task_id": task_id,
        "status": "completed",
        "summary": "stress probe done",
        "findings": [],
    })
    if (cycle + 1) % 10 == 0:
        print(f"sim: {cycle + 1}/{N} done")

print("sim: all cycles complete")
PYEOF

# Let watchdog do a couple of ticks before we stop the server,
# to catch any tasks that leaked into in_progress.
sleep 3

kill "${SERVER_PID}" 2>/dev/null || true
wait "${SERVER_PID}" 2>/dev/null || true
SERVER_PID=""

# --- analyze log -----------------------------------------------------------
TIMEOUTS=$(grep -c "WATCHDOG TIMEOUT" "$LOG_FILE" 2>/dev/null; true)

echo
echo "─────────────────────────────────────────────"
echo "stress: server log: ${LOG_FILE}"
echo "stress: WATCHDOG TIMEOUT count: ${TIMEOUTS}"
echo "─────────────────────────────────────────────"

if [[ "${TIMEOUTS}" -eq 0 ]]; then
  echo "PASS: 0 WATCHDOG TIMEOUT in ${N} cycles"
  exit 0
else
  echo "FAIL: ${TIMEOUTS} WATCHDOG TIMEOUT event(s) found"
  grep "WATCHDOG TIMEOUT" "$LOG_FILE" || true
  exit 1
fi
