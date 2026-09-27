#!/usr/bin/env bash
# Print the phase-2 owner commands. This script never runs them.
set -euo pipefail
build_sha="${1:-$(git rev-parse HEAD)}"
[[ "$build_sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'expected a full build SHA' >&2; exit 2; }
cat <<COMMANDS
# Phase 2 for build $build_sha — owner must review and run manually.
sudo install -d -o root -g root -m 0755 /usr/local/libexec/crew-authz
sudo install -o root -g root -m 0755 /home/truhojun/alfred/tools/cea/broker-launch.sh /usr/local/libexec/crew-authz/broker-launch.sh
sudo chown -R root:root /opt/agent_crew-authz
sudo chmod -R u=rwX,go=rX /opt/agent_crew-authz
echo 'truhojun ALL=(crew-authz) NOPASSWD: /usr/local/libexec/crew-authz/broker-launch.sh' | sudo tee /etc/sudoers.d/crew-authz-broker >/dev/null
sudo chmod 0440 /etc/sudoers.d/crew-authz-broker && sudo visudo -cf /etc/sudoers.d/crew-authz-broker
# Verify /opt/agent_crew-authz/SRC_COMMIT == $build_sha before restarting.
COMMANDS
