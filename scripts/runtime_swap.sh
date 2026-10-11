#!/usr/bin/env bash
# Usage: runtime_swap.sh <project> <full-sha> preflight|go|post
set -euo pipefail

project=${1:?Usage: runtime_swap.sh <project> <full-sha> preflight|go|post}
precheck=${AGENT_CREW_SWAP_PRECHECK:-}
if [[ -z ${precheck//[[:space:]]/} ]]; then
    echo 'FAIL: AGENT_CREW_SWAP_PRECHECK is unset; set a precheck command or explicitly set none' >&2
    exit 2
fi
if [[ $precheck != none ]]; then
    read -r -a precheck_command <<< "$precheck"
    if precheck_output=$("${precheck_command[@]}" "$project" 2>&1); then
        [[ -z $precheck_output ]] || printf '%s\n' "$precheck_output"
    else
        rc=$?
        [[ -z $precheck_output ]] || printf '%s\n' "$precheck_output" >&2
        printf 'FAIL: swap precheck refused %s (rc=%d)\n' "$project" "$rc" >&2
        exit "$rc"
    fi
fi
exec python3 "$(dirname "$0")/runtime_swap.py" "$@"
