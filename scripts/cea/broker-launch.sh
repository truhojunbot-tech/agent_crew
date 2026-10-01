#!/usr/bin/env bash
# SEV-0 CEA P2a Option B — launcher for the executor-binding authz broker.
#
# Phase 1 installs byte-identically at the existing sudoers-pinned path under
# /home/truhojun/alfred. Phase 2 uses /usr/local/libexec/crew-authz; see the
# deploy plan. The old path is always a downgraded integrity-by-convention gate.
#
# crew-authz (uid 998) is nologin with no home: nothing here reads the home directory.
# sudo resets the environment (env_reset), so every setting has a fixed default;
# the AGENT_CREW_AUTHZ_* overrides only apply when run directly (e.g. --degraded).
#
# Modes:
#   (default)   refuse unless euid == crew-authz; exec the broker.
#   --check     print uid, ptrace_scope, socket-dir perms, python import; exit 0/1.
#   --degraded  run in the caller's uid; the broker NEVER issues VERIFIED.
#   --start|--stop|--restart|--health  managed lifecycle as crew-authz.
# Live broker lifecycle is managed separately from the owner update script.
#
# Tests inject a fake euid with AGENT_CREW_AUTHZ_FAKE_EUID. It only moves this
# script's gate: the Python broker re-checks the real euid and refuses, so the
# variable cannot turn a uid-1000 process into a VERIFIED-issuing broker.
set -euo pipefail

LAUNCHER_PATH="$(readlink -f "$0")"
export AGENT_CREW_AUTHZ_LAUNCHER_PATH="$LAUNCHER_PATH"
CONFIG="${AGENT_CREW_AUTHZ_CONFIG:-/opt/agent_crew-authz/broker.env}"
export AGENT_CREW_AUTHZ_CONFIG_PATH="$CONFIG"
export AGENT_CREW_AUTHZ_SRC_COMMIT_PATH="${AGENT_CREW_AUTHZ_SRC_COMMIT_PATH:-/opt/agent_crew-authz/SRC_COMMIT}"
if [[ -f "$CONFIG" ]]; then
  # The config and all its ancestors are included in the integrity check below.
  # Phase 2 installs this file under the root-owned broker tree.
  set -a
  source "$CONFIG"
  set +a
fi

SERVICE_USER="crew-authz"
MODE="run"
if [[ $# -gt 1 ]]; then echo "broker-launch: exactly one mode is allowed" >&2; exit 2; fi
for arg in "$@"; do
  case "$arg" in
    --check) MODE="check" ;;
    --degraded) MODE="degraded" ;;
    --start) MODE="start" ;;
    --stop) MODE="stop" ;;
    --restart) MODE="restart" ;;
    --health) MODE="health" ;;
    *) echo "broker-launch: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

SERVICE_UID="$(id -u "$SERVICE_USER" 2>/dev/null || echo "")"
EUID_NOW="${AGENT_CREW_AUTHZ_FAKE_EUID:-$(id -u)}"
VENV="${AGENT_CREW_AUTHZ_VENV:-/opt/agent_crew-authz/venv}"
PYTHON="${AGENT_CREW_AUTHZ_PYTHON:-$VENV/bin/python}"
# The agent_crew source must be readable by crew-authz. /home/truhojun is 0750,
# so the default is a world-readable install location, not the dev checkout.
PYPATH="${AGENT_CREW_AUTHZ_PYTHONPATH:-/opt/agent_crew-authz/src}"
SOCK_DIR="${AGENT_CREW_AUTHZ_SOCK_DIR:-/tmp/crew-authz-${SERVICE_UID:-none}}"
CLIENT_UID="${AGENT_CREW_AUTHZ_CLIENT_UID:-1000}"
CLIENT_GROUP="${AGENT_CREW_AUTHZ_CLIENT_GROUP:-truhojun}"
PTRACE_SCOPE_FILE="${AGENT_CREW_AUTHZ_PTRACE_SCOPE_FILE:-/proc/sys/kernel/yama/ptrace_scope}"
export PYTHONNOUSERSITE=1

ptrace_scope() { cat "$PTRACE_SCOPE_FILE" 2>/dev/null || echo "unknown"; }

integrity_report() {
  "$PYTHON" - "$LAUNCHER_PATH" "$PYPATH/agent_crew/cea/broker.py" "$CONFIG" "$PYPATH" "$AGENT_CREW_AUTHZ_SRC_COMMIT_PATH" <<'PY'
import os, stat, sys
for path in (sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[5]):
    if path == sys.argv[3] and not os.path.exists(path):
        continue
    current = os.path.abspath(path)
    while True:
        try:
            st = os.lstat(current)
        except OSError:
            print(f"downgrade_reason=BROKER_TREE_USER_WRITABLE path={current} missing")
            sys.exit(0)
        if st.st_uid != 0 or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
            print(f"downgrade_reason=BROKER_TREE_USER_WRITABLE path={current}")
            sys.exit(0)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
for root, dirs, files in os.walk(os.path.abspath(sys.argv[4]), followlinks=False):
    for name in dirs + files:
        path = os.path.join(root, name)
        st = os.lstat(path)
        if st.st_uid != 0 or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
            print(f"downgrade_reason=BROKER_TREE_USER_WRITABLE path={path}")
            sys.exit(0)
print("downgrade_reason=none")
PY
}

dir_report() {
  if [[ -d "$SOCK_DIR" ]]; then stat -c '%a %U:%G' "$SOCK_DIR"; else echo "absent"; fi
}

# 0710 + the client group, and nothing else. The old fallback (0711 when
# crew-authz is not in the client group, plus a 0666 socket) let every uid on the
# host traverse to the socket inode; codex review-sev0-cea-lineage-s3-x named it,
# and `Broker.bind()` now refuses any mode but 0710. If crew-authz is not in
# CLIENT_GROUP the fix is `usermod -aG`, not a wider mode. A pre-existing dir must
# be ours and not a symlink: /tmp is shared and a squatted dir would be someone
# else's socket.
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
  if ! id -nG | tr ' ' '\n' | grep -qx "$CLIENT_GROUP"; then
    echo "broker-launch: $(id -un) is not in $CLIENT_GROUP, so $SOCK_DIR cannot be 0710 to the" >&2
    echo "  clients. Refusing: the old 0711 + 0666-socket fallback admitted every uid on the host." >&2
    echo "  Fix: usermod -aG $CLIENT_GROUP $SERVICE_USER" >&2
    exit 4
  fi
  chgrp "$CLIENT_GROUP" "$SOCK_DIR"; chmod 0710 "$SOCK_DIR"
}

if [[ "$MODE" == "check" ]]; then
  ok=0
  echo "euid=$EUID_NOW service_user=$SERVICE_USER service_uid=${SERVICE_UID:-missing}"
  echo "ptrace_scope=$(ptrace_scope)"
  echo "sock_dir=$SOCK_DIR perms=$(dir_report)"
  echo "pythonpath=$PYPATH readable=$([[ -r "$PYPATH/agent_crew/cea/broker.py" ]] && echo yes || echo no)"
  integrity="$(integrity_report)"
  echo "$integrity"
  [[ "$integrity" == "downgrade_reason=none" ]] || ok=1
  if [[ ! -x "$PYTHON" ]]; then echo "FAIL: pinned venv python missing: $PYTHON"; ok=1
  else
    if ! PYTHONPATH="$PYPATH" "$PYTHON" - <<'PY'
import importlib.metadata as m
from agent_crew.cea.schema import load_schema
from agent_crew.cea import broker, auth, engine, wiring, service
for name in ("jsonschema", "cryptography", "httpx"):
    print(f"dependency {name}={m.version(name)}")
import jsonschema
assert hasattr(jsonschema, "Draft202012Validator"), "jsonschema >=4.18 required"
load_schema()
print("receipt_schema=ok")
PY
    then ok=1; fi
  fi
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
if [[ "$MODE" == "start" || "$MODE" == "stop" || "$MODE" == "restart" || "$MODE" == "health" ]]; then
  if [[ -z "$SERVICE_UID" || "$EUID_NOW" != "$SERVICE_UID" ]]; then
    echo "broker-launch: lifecycle requires $SERVICE_USER via the sudoers-pinned launcher" >&2; exit 3
  fi
fi

prepare_dir
integrity_report >&2
export PYTHONPATH="$PYPATH"
if [[ "$MODE" == "start" || "$MODE" == "stop" || "$MODE" == "restart" || "$MODE" == "health" ]]; then
  "$PYTHON" -m agent_crew.cea.broker_lifecycle "$MODE" "$SOCK_DIR" "$SERVICE_UID" \
    "${AGENT_CREW_CEA_CALLER_TOKENS:-/opt/agent_crew-authz/caller-tokens.json}" "$PYTHON" "$CLIENT_UID"
  exit $?
fi
ARGS=(--sock-dir "$SOCK_DIR" --client-uid "$CLIENT_UID")
[[ "$MODE" == "degraded" ]] && ARGS+=(--degraded)
if [[ -n "${AGENT_CREW_AUTHZ_DRY_RUN:-}" ]]; then
  echo "exec $PYTHON -m agent_crew.cea.broker ${ARGS[*]}"; exit 0
fi
exec "$PYTHON" -m agent_crew.cea.broker "${ARGS[@]}"
