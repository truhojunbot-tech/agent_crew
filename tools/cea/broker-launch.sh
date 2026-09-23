#!/usr/bin/env bash
# SEV-0 CEA P2a Option B — launcher for the executor-binding authz broker.
#
# INSTALL (alfred side): copy this file BYTE-IDENTICAL to
#   /home/truhojun/alfred/tools/cea/broker-launch.sh   (mode 0755, owner truhojun)
# That path is fixed by the LIVE sudoers rule (/etc/sudoers.d/crew-authz-broker):
#   truhojun ALL=(crew-authz) NOPASSWD: /home/truhojun/alfred/tools/cea/broker-launch.sh
# Do not edit sudoers. Run:  sudo -n -u crew-authz /home/truhojun/alfred/tools/cea/broker-launch.sh
#
# crew-authz (uid 998) is nologin with no home: nothing here reads $HOME.
# sudo resets the environment (env_reset), so every setting has a fixed default;
# the AGENT_CREW_AUTHZ_* overrides only apply when run directly (e.g. --degraded).
#
# Modes:
#   (default)   refuse unless euid == crew-authz; exec the broker.
#   --check     print uid, ptrace_scope, socket-dir perms, python import; exit 0/1.
#   --degraded  run in the caller's uid; the broker NEVER issues VERIFIED.
#
# Tests inject a fake euid with AGENT_CREW_AUTHZ_FAKE_EUID. It only moves this
# script's gate: the Python broker re-checks the real euid and refuses, so the
# variable cannot turn a uid-1000 process into a VERIFIED-issuing broker.
set -euo pipefail

SERVICE_USER="crew-authz"
MODE="run"
for arg in "$@"; do
  case "$arg" in
    --check) MODE="check" ;;
    --degraded) MODE="degraded" ;;
    *) echo "broker-launch: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

SERVICE_UID="$(id -u "$SERVICE_USER" 2>/dev/null || echo "")"
EUID_NOW="${AGENT_CREW_AUTHZ_FAKE_EUID:-$(id -u)}"
PYTHON="${AGENT_CREW_AUTHZ_PYTHON:-/usr/bin/python3}"
# The agent_crew source must be readable by crew-authz. /home/truhojun is 0750,
# so the default is a world-readable install location, not the dev checkout.
PYPATH="${AGENT_CREW_AUTHZ_PYTHONPATH:-/opt/agent_crew-authz/src}"
SOCK_DIR="${AGENT_CREW_AUTHZ_SOCK_DIR:-/tmp/crew-authz-${SERVICE_UID:-none}}"
CLIENT_UID="${AGENT_CREW_AUTHZ_CLIENT_UID:-1000}"
CLIENT_GROUP="${AGENT_CREW_AUTHZ_CLIENT_GROUP:-truhojun}"
PTRACE_SCOPE_FILE="${AGENT_CREW_AUTHZ_PTRACE_SCOPE_FILE:-/proc/sys/kernel/yama/ptrace_scope}"

ptrace_scope() { cat "$PTRACE_SCOPE_FILE" 2>/dev/null || echo "unknown"; }

dir_report() {
  if [[ -d "$SOCK_DIR" ]]; then stat -c '%a %U:%G' "$SOCK_DIR"; else echo "absent"; fi
}

# 0710 + client group when crew-authz is in that group; else 0711 and the broker
# enforces the client uid with SO_PEERCRED. A pre-existing dir must be ours and
# not a symlink: /tmp is shared and a squatted dir would be someone else's socket.
prepare_dir() {
  if [[ -L "$SOCK_DIR" ]]; then echo "broker-launch: $SOCK_DIR is a symlink; refusing" >&2; exit 4; fi
  if [[ -e "$SOCK_DIR" ]]; then
    local owner; owner="$(stat -c '%u' "$SOCK_DIR")"
    if [[ "$owner" != "$(id -u)" ]]; then
      echo "broker-launch: $SOCK_DIR is owned by uid $owner, not $(id -u); refusing" >&2; exit 4
    fi
  else
    (umask 077; mkdir -p "$SOCK_DIR")
  fi
  if id -nG | tr ' ' '\n' | grep -qx "$CLIENT_GROUP"; then
    chgrp "$CLIENT_GROUP" "$SOCK_DIR"; chmod 0710 "$SOCK_DIR"
  else
    chmod 0711 "$SOCK_DIR"
  fi
}

if [[ "$MODE" == "check" ]]; then
  ok=0
  echo "euid=$EUID_NOW service_user=$SERVICE_USER service_uid=${SERVICE_UID:-missing}"
  echo "ptrace_scope=$(ptrace_scope)"
  echo "sock_dir=$SOCK_DIR perms=$(dir_report)"
  echo "pythonpath=$PYPATH readable=$([[ -r "$PYPATH/agent_crew/cea/broker.py" ]] && echo yes || echo no)"
  [[ -n "$SERVICE_UID" ]] || { echo "FAIL: user $SERVICE_USER missing"; ok=1; }
  s="$(ptrace_scope)"; [[ "$s" =~ ^[0-9]+$ && "$s" -ge 1 ]] || { echo "FAIL: ptrace_scope must be >=1"; ok=1; }
  [[ "$EUID_NOW" == "$SERVICE_UID" ]] || echo "NOTE: not running as $SERVICE_USER (only --degraded would start)"
  exit "$ok"
fi

s="$(ptrace_scope)"
if ! [[ "$s" =~ ^[0-9]+$ && "$s" -ge 1 ]]; then
  echo "broker-launch: kernel.yama.ptrace_scope=$s; >=1 required; refusing" >&2; exit 3
fi

if [[ "$MODE" == "run" && ( -z "$SERVICE_UID" || "$EUID_NOW" != "$SERVICE_UID" ) ]]; then
  echo "broker-launch: euid $EUID_NOW is not $SERVICE_USER (${SERVICE_UID:-missing}); refusing (use --degraded for an in-uid broker that never issues VERIFIED)" >&2
  exit 3
fi

prepare_dir
export PYTHONPATH="$PYPATH"
ARGS=(--sock-dir "$SOCK_DIR" --client-uid "$CLIENT_UID")
[[ "$MODE" == "degraded" ]] && ARGS+=(--degraded)
if [[ -n "${AGENT_CREW_AUTHZ_DRY_RUN:-}" ]]; then
  echo "exec $PYTHON -m agent_crew.cea.broker ${ARGS[*]}"; exit 0
fi
exec "$PYTHON" -m agent_crew.cea.broker "${ARGS[@]}"
