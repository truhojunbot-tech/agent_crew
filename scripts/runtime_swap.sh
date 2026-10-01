#!/usr/bin/env bash
# Usage: runtime_swap.sh <project> <full-sha> preflight|go|post
set -euo pipefail
exec python3 "$(dirname "$0")/runtime_swap.py" "$@"
