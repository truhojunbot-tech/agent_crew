#!/usr/bin/env bash
# Launcher path is configurable for phase 1; phase 2 root path is the default.
set -euo pipefail
launcher="${AGENT_CREW_CEA_BROKER_LAUNCHER:-/usr/local/libexec/crew-authz/broker-launch.sh}"
exec sudo -n -u crew-authz "$launcher" "$@"
