#!/usr/bin/env bash
# Usage: runtime_swap.sh <project> <full-sha> preflight|go|post
# go additionally requires --decision-id T0-ID and --actor NAME (or AGENT_CREW_SWAP_ACTOR).
set -euo pipefail

project=${1:?Usage: runtime_swap.sh <project> <full-sha> preflight|go|post}
target_sha=${2:?Usage: runtime_swap.sh <project> <full-sha> preflight|go|post}
step=${3:?Usage: runtime_swap.sh <project> <full-sha> preflight|go|post}
# The go receipt and the T0 verifier must cite the same explicit actor.
# Extract only this option; runtime_swap.py remains the argument validator.
if [[ $step == go ]]; then
    swap_args=("${@:4}")
    for ((i=0; i<${#swap_args[@]}; i++)); do
        case ${swap_args[i]} in
            --actor)
                if (( i + 1 < ${#swap_args[@]} )); then
                    export AGENT_CREW_SWAP_ACTOR=${swap_args[i+1]}
                    i=$((i + 1))
                fi
                ;;
            --actor=*) export AGENT_CREW_SWAP_ACTOR=${swap_args[i]#--actor=} ;;
        esac
    done
fi
precheck=${AGENT_CREW_SWAP_PRECHECK:-}
if [[ -z ${precheck//[[:space:]]/} ]]; then
    echo 'FAIL: AGENT_CREW_SWAP_PRECHECK is unset; set a precheck command or explicitly set none' >&2
    exit 2
fi
precheck_output_file=$(mktemp)
trap 'rm -f "$precheck_output_file"' EXIT
rc=0
if [[ $precheck == none ]]; then
    precheck_command=(none)
else
    read -r -a precheck_command <<< "$precheck"
    if "${precheck_command[@]}" "$project" >"$precheck_output_file" 2>&1; then
        :
    else
        rc=$?
    fi
fi
if (( rc != 0 )); then
    cat "$precheck_output_file" >&2
    printf 'FAIL: swap precheck refused %s (rc=%d)\n' "$project" "$rc" >&2
fi
if [[ $precheck == none ]]; then
    python3 "$(dirname "$0")/swap_precheck_receipt.py" \
        "$project" "$target_sha" "$step" "$rc" "$precheck_output_file" "$PPID" none || exit 2
else
    python3 "$(dirname "$0")/swap_precheck_receipt.py" \
        "$project" "$target_sha" "$step" "$rc" "$precheck_output_file" "$PPID" \
        "${precheck_command[@]}" "$project" || exit 2
fi
if (( rc != 0 )); then
    exit "$rc"
fi
cat "$precheck_output_file"
rm -f "$precheck_output_file"
exec python3 "$(dirname "$0")/runtime_swap.py" "$@"
