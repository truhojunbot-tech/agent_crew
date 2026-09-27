#!/usr/bin/env bash
# Phase 1 staging only. Never changes sudoers or starts the live broker.
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)/broker-launch.sh"
dest="${AGENT_CREW_CEA_PHASE1_LAUNCHER:-/home/truhojun/alfred/tools/cea/broker-launch.sh}"
install -m 0755 "$src" "$dest"
want="$(sha256sum "$src" | cut -d' ' -f1)"
got="$(sha256sum "$dest" | cut -d' ' -f1)"
[[ "$want" == "$got" && "$(stat -c '%a' "$dest")" == 755 ]] || {
  echo 'launcher install verification failed' >&2; exit 1;
}
echo "installed=$dest sha256=$got mode=0755"
