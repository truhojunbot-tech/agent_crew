#!/usr/bin/env bash
# Print the phase-2 owner entrypoint. This script never changes the host.
set -euo pipefail
build_sha="${1:-$(git rev-parse HEAD)}"
[[ "$build_sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'expected a full build SHA' >&2; exit 2; }
cat <<COMMANDS
# Phase 2 for build $build_sha — owner reviews dry-run and supplies token source.
scripts/cea/owner_root_enforce_setup.sh --dry-run
# sudo scripts/cea/owner_root_enforce_setup.sh --apply --caller-tokens /path/to/private/adapter-tokens.json
# sudo scripts/cea/owner_root_enforce_setup.sh --undo
# Verify /opt/agent_crew-authz/SRC_COMMIT and downgrade_reason before restarting.
COMMANDS
