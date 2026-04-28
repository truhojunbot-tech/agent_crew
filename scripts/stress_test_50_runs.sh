#!/usr/bin/env bash
# Stress test: run `crew run` N times against a single project, then check
# the server log for watchdog timeouts. Acceptance criterion for #121
# (parent #106): N consecutive runs must produce zero `WATCHDOG TIMEOUT`
# events. Today's tmux-push baseline produces 1-2 per ~10 runs (#102,
# #105 sample); after the MCP-pull cutover (#119) this number must hit 0.
#
# Usage:
#   scripts/stress_test_50_runs.sh [-n N] [-p PROJECT] [-t "TASK"]
#       [--base ~/.agent_crew]
#
# Flags:
#   -n N        number of runs (default: 50)
#   -p PROJECT  project name (default: agent_crew)
#   -t TASK     task body sent to `crew run` (default: trivial echo task)
#   --base DIR  state base directory (default: ~/.agent_crew)
#   -h          help
#
# After the run, the script invokes the analyzer:
#
#   python -m agent_crew._stress_log_analyzer <state>/<project>/server.log
#
# and exits 0 only when the analyzer reports PASS (zero timeouts).

set -euo pipefail

N=50
PROJECT="agent_crew"
TASK="No-op stress probe — return immediately with status=completed"
BASE="${HOME}/.agent_crew"

usage() {
  sed -n '2,28p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) N="$2"; shift 2 ;;
    -p) PROJECT="$2"; shift 2 ;;
    -t) TASK="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 64 ;;
  esac
done

STATE_DIR="${BASE}/${PROJECT}"
LOG_PATH="${STATE_DIR}/server.log"
RUN_LOG="$(mktemp -t crew-stress.XXXXXX.log)"

if [[ ! -d "$STATE_DIR" ]]; then
  echo "no project state at ${STATE_DIR} — run 'crew setup ${PROJECT}' first" >&2
  exit 2
fi

echo "stress: ${N} runs of project=${PROJECT}"
echo "stress: server log: ${LOG_PATH}"
echo "stress: per-run output: ${RUN_LOG}"

# Mark the log start so the analyzer can scope to this stress session
# instead of the project's full history. We append a synthetic marker
# the analyzer ignores.
START_MARKER="STRESS-START-$(date -Iseconds)-$$"
echo "[stress] ${START_MARKER}" >>"$LOG_PATH" || true

failed_runs=0
for i in $(seq 1 "$N"); do
  echo "── run ${i}/${N} ──" | tee -a "$RUN_LOG"
  if ! crew run "${TASK} (#${i})" --project "$PROJECT" --base "$BASE" >>"$RUN_LOG" 2>&1; then
    failed_runs=$((failed_runs + 1))
    echo "stress: run ${i} failed (rc != 0)" | tee -a "$RUN_LOG"
  fi
done

echo
echo "stress: ${N} runs complete. ${failed_runs} non-zero exit(s) on the wrapper."
echo

# Slice the log to the stress window via the start marker, then analyze.
SLICED="$(mktemp -t crew-stress-slice.XXXXXX.log)"
awk -v m="${START_MARKER}" 'found {print} index($0, m){found=1}' "$LOG_PATH" >"$SLICED"

echo "stress: analyzer report ─────────────────────────────────"
python3 -m agent_crew._stress_log_analyzer "$SLICED" || RC=$?
RC="${RC:-0}"
echo "─────────────────────────────────────────────────────────"
echo "wrapper failed_runs: ${failed_runs}"
echo "analyzer rc: ${RC}"

# We exit non-zero if EITHER any wrapper exited non-zero OR the analyzer
# found a watchdog timeout in the log slice.
if [[ "$failed_runs" -ne 0 || "$RC" -ne 0 ]]; then
  exit 1
fi
exit 0
