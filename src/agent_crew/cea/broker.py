"""P2a Option B — the EXECUTOR-binding authz broker (owner 11390, ADR Π P2a, O21b).

A long-running process meant to run as the ``crew-authz`` uid (998, nologin, no
home) behind ``tools/cea/broker-launch.sh``. It is the **only** code path that
may write ``executor_binding_status = VERIFIED``, and — see O21c below — it does
not currently write it at all. It never writes ``caller_identity_status =
VERIFIED``: caller-role identity stays UNVERIFIED (ADR r3 — broker-spawned
adapters are TO BUILD / DEFER).

⛔**Registration is peer-asserted, and peer assertion cannot be authenticated
  under one uid.** codex (``review-sev0-cea-lineage-s3-x``, P1) reproduced it
  exactly: an arbitrary uid-1000 process forks a child, calls
  ``register(pid=<its own child>, start_time=…, receipt_id='victim-receipt',
  attempt=1)``, the child attests, and the answer was
  ``executor_binding_status=VERIFIED, reason=PEER_CRED_BOUND``. Every check
  below held while it did: ``SO_PEERCRED`` was genuine, the registered pid
  really was a direct child of the registrant, the start_time matched. They
  prove *the attacker spawned its own child* — not that the authorized
  dispatcher spawned the executor. Those are different claims and only the
  second one is worth anything, and ``SO_PEERCRED`` cannot tell them apart while
  dispatcher and attacker share uid 1000 (ADR P2a). The socket permissions do
  not repair it either: the whole point of the socket is that every uid-1000
  process may reach it.

So the binding tuple ``(pid, start_time, receipt_id, attempt, registered_by)``
is recorded and returned as **evidence only**, and ``attest`` answers
``UNVERIFIED`` with ``downgrade_reason=DISPATCHER_REGISTRATION_UNAUTHENTICATED``.

**O21c — DEFERRED.** ``VERIFIED`` is reserved for a spawn the broker itself
performed under a privilege boundary. :class:`BrokerSpawnRequest` and
:meth:`Broker.spawn` are the interface for it and return ``UNAVAILABLE`` today.
Landing it requires, exactly:

1. the broker runs as ``crew-authz`` (998) and the dispatcher as a *different*
   uid — the ``--degraded`` in-uid mode can never satisfy this;
2. the broker, not the dispatcher, calls ``fork``/``exec`` for the executor, so
   the ``(pid, start_time)`` it registers is one it observed itself and no peer
   asserted;
3. the dispatcher's request to spawn is authorized by something that is not
   "being uid 1000" — a credential the broker validates, of the shape
   :mod:`agent_crew.cea.auth` already checks with ``hmac.compare_digest``;
4. ``kernel.yama.ptrace_scope >= 1`` still holds, or the spawned executor can be
   ptraced after the fact and the binding is a label again.

Until all four hold, nothing in this module may return ``VERIFIED``; the static
test ``test_no_verified_promotion_path_outside_the_broker`` pins that to the one
deferred function.

Why a different uid is the whole point: under one uid, anything the executor
could present (a bearer token, an env value, a field it fills in, a file it can
read) is equally available to every other process of that uid. The broker
instead binds to what the *kernel* says about the peer:

1. ``SO_PEERCRED`` on the accepted connection → ``(pid, uid)`` the peer cannot choose;
2. ``/proc/<pid>/stat`` field 22 ``start_time`` → a pid that was recycled is a
   different process;
3. the ``(receipt_id, attempt)`` the dispatcher registered that exact
   ``(pid, start_time)`` for at spawn.

Protocol (one JSON object per line, one request per connection)::

    {"op": "register", "pid": P, "start_time": S, "receipt_id": R, "attempt": A}
        dispatcher → broker, right after spawn. The registered pid must be a
        direct child of the registering peer, must not descend from another
        registered executor (refuse pids inside another agent's process tree),
        and (R, A) is single-use: a second registration is a CONFLICT that
        poisons the pair. ⛔None of that authenticates *which* uid-1000 process
        registered; see above. The result is evidence, never a promotion.
    {"op": "attest", "receipt_id": R, "attempt": A}
        executor → broker. BLOCKED if the peer is not exactly the registered
        (pid, start_time), or is being ptraced, or the pair is poisoned — those
        are refusals and stay. Otherwise UNVERIFIED: either
        NOT_REGISTERED_AT_SPAWN, or the binding held but was peer-asserted
        (downgrade_reason=DISPATCHER_REGISTRATION_UNAUTHENTICATED, with the
        tuple returned under ``binding_evidence``).
    {"op": "spawn", ...}
        dispatcher → broker, O21c. Returns UNAVAILABLE (see above).
    {"op": "status"}

The per-registration nonce lives only in this process's memory and is handed to
the attested executor; a broker restart forgets every registration, so every
executor after a restart is UNVERIFIED until re-registered (fail closed).

Refuses to start (non-degraded) unless euid is the service uid and
``kernel.yama.ptrace_scope >= 1`` — at 0 any same-uid process can ptrace the
executor after attestation, which would make VERIFIED a label.
``--degraded`` runs in-uid for development and **never** issues VERIFIED.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import secrets
import socket
import stat
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

SERVICE_USER = "crew-authz"
SOCKET_NAME = "broker.sock"
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
BLOCKED = "BLOCKED"
_PEERCRED = struct.Struct("3i")


class BrokerRefused(Exception):
    """The broker will not start in this environment (fail closed)."""


# ───────────────────────── /proc — the kernel's word, not the peer's ──────────

class ProcReader:
    """``/proc`` access, injectable so tests can describe a process tree."""

    def __init__(self, root: str = "/proc"):
        self.root = root

    def stat(self, pid: int) -> Optional[tuple[int, int]]:
        """``(ppid, start_time)`` or ``None`` if the process is gone."""
        try:
            with open(f"{self.root}/{int(pid)}/stat", "r") as fh:
                raw = fh.read()
        except OSError:
            return None
        # comm may contain spaces/parens: split after the *last* ')'.
        rest = raw[raw.rindex(")") + 2:].split()
        return int(rest[1]), int(rest[19])      # fields 4 (ppid) and 22 (starttime)

    def tracer(self, pid: int) -> Optional[int]:
        try:
            with open(f"{self.root}/{int(pid)}/status", "r") as fh:
                for line in fh:
                    if line.startswith("TracerPid:"):
                        return int(line.split()[1])
        except (OSError, ValueError):
            return None
        return None


def ptrace_scope(path: str = "/proc/sys/kernel/yama/ptrace_scope") -> Optional[int]:
    try:
        with open(path, "r") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def service_uid(user: str = SERVICE_USER) -> Optional[int]:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def peer_cred(conn: socket.socket) -> tuple[int, int, int]:
    """``(pid, uid, gid)`` of the other end, from the kernel (``SO_PEERCRED``)."""
    return _PEERCRED.unpack(conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size))


# ───────────────────────── the broker ─────────────────────────────────────────

PEER_ASSERTED = "PEER_ASSERTED"
BROKER_SPAWNED = "BROKER_SPAWNED"
DISPATCHER_REGISTRATION_UNAUTHENTICATED = "DISPATCHER_REGISTRATION_UNAUTHENTICATED"


@dataclass(frozen=True)
class Registration:
    pid: int
    start_time: int
    receipt_id: str
    attempt: int
    registered_by: int
    nonce: str          # broker memory only; never persisted, never logged
    origin: str = PEER_ASSERTED
    # ⛔`PEER_ASSERTED` is every registration this module can currently create.
    #   `BROKER_SPAWNED` is O21c and is reachable only from `spawn()`, which
    #   returns UNAVAILABLE. The field exists so the distinction is in the data
    #   model rather than in a comment somebody has to remember.


@dataclass(frozen=True)
class BrokerSpawnRequest:
    """O21c — DEFERRED. The request that would let the broker spawn the executor
    itself, which is the only way ``(pid, start_time)`` stops being a peer's
    claim about its own child.

    ``credential`` is the dispatcher's, and would be checked the way
    :mod:`agent_crew.cea.auth` checks one: ``hmac.compare_digest`` against a
    table the broker reads as ``crew-authz`` and uid 1000 cannot. "Is uid 1000"
    is exactly the authorization that failed.
    """
    receipt_id: str
    attempt: int
    argv: tuple[str, ...]
    credential: Optional[str] = None
    cwd: Optional[str] = None
    env: Optional[dict] = None


class Broker:
    """Decision logic is :meth:`handle` (pure given a ``ProcReader``); :meth:`serve` is I/O."""

    def __init__(self, sock_dir: str, *, degraded: bool = False,
                 client_uids: tuple[int, ...] = (1000,), proc: Optional[ProcReader] = None,
                 service_uid_override: Optional[int] = None, ptrace_scope_path: Optional[str] = None):
        self.sock_dir = sock_dir
        self.degraded = degraded
        self.client_uids = tuple(client_uids)
        self.proc = proc or ProcReader()
        # Tests run the broker in-uid and say so here. There is no CLI flag and
        # no env var for this: the launcher cannot reach it.
        self._service_uid = service_uid_override
        self._ptrace_path = ptrace_scope_path
        self._regs: dict[tuple[str, int], Registration] = {}
        self._poisoned: set[tuple[str, int]] = set()
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None

    # -- start-up checks -------------------------------------------------

    def preflight(self) -> dict:
        euid = os.geteuid()
        want = self._service_uid if self._service_uid is not None else service_uid()
        scope = ptrace_scope(self._ptrace_path) if self._ptrace_path else ptrace_scope()
        report = {"euid": euid, "service_uid": want, "ptrace_scope": scope,
                  "degraded": self.degraded, "sock_dir": self.sock_dir}
        if scope is None or scope < 1:
            raise BrokerRefused(f"kernel.yama.ptrace_scope={scope}; >=1 required (same-uid ptrace "
                                f"would defeat any peer binding)")
        if not self.degraded and (want is None or euid != want):
            raise BrokerRefused(f"euid {euid} is not {SERVICE_USER} ({want}); run via "
                                f"broker-launch.sh or pass --degraded (never issues VERIFIED)")
        return report

    # -- protocol --------------------------------------------------------

    def handle(self, peer_pid: int, peer_uid: int, req: dict) -> dict:
        if peer_uid not in self.client_uids and peer_uid != os.geteuid():
            return {"ok": False, "error": "PEER_UID_NOT_ALLOWED"}
        op = req.get("op") if isinstance(req, dict) else None
        if op == "register":
            return self._register(peer_pid, req)
        if op == "attest":
            return self._attest(peer_pid, req)
        if op == "spawn":
            return self.spawn(req)
        if op == "status":
            return {"ok": True, "degraded": self.degraded, "registrations": len(self._regs)}
        return {"ok": False, "error": "UNKNOWN_OP"}

    def _register(self, peer_pid: int, req: dict) -> dict:
        try:
            pid, start = int(req["pid"]), int(req["start_time"])
            key = (str(req["receipt_id"]), int(req["attempt"]))
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "BAD_REQUEST"}
        st = self.proc.stat(pid)
        if st is None or st[1] != start:
            return {"ok": False, "error": "PID_START_TIME_MISMATCH"}
        if st[0] != peer_pid:
            return {"ok": False, "error": "NOT_A_CHILD_OF_REGISTRANT"}
        with self._lock:
            if self._descends_from_executor(pid):
                return {"ok": False, "error": "DESCENDANT_OF_ANOTHER_AGENT"}
            if key in self._poisoned:
                return {"ok": False, "error": "REGISTRATION_CONFLICT"}
            if key in self._regs:
                # Two claims on one (receipt, attempt): neither is trustworthy.
                self._poisoned.add(key)
                del self._regs[key]
                return {"ok": False, "error": "REGISTRATION_CONFLICT"}
            self._regs[key] = Registration(pid, start, key[0], key[1], peer_pid,
                                           secrets.token_hex(16), origin=PEER_ASSERTED)
        # ⛔`ok` means "recorded", not "authenticated". codex P1: an attacker
        #   forks its own child and registers it against a victim receipt_id, and
        #   every check above passes, because every check above is about the
        #   registrant's own process tree. What the caller gets for this is an
        #   UNVERIFIED attestation carrying the tuple as evidence.
        return {"ok": True, "origin": PEER_ASSERTED,
                "registration_authentication": DISPATCHER_REGISTRATION_UNAUTHENTICATED}

    def _descends_from_executor(self, pid: int) -> bool:
        executors = {r.pid for r in self._regs.values()}
        seen: set[int] = set()
        cur = pid
        while cur > 1 and cur not in seen:
            seen.add(cur)
            st = self.proc.stat(cur)
            if st is None:
                return False
            cur = st[0]
            if cur in executors:
                return True
        return False

    def _attest(self, peer_pid: int, req: dict) -> dict:
        try:
            key = (str(req["receipt_id"]), int(req["attempt"]))
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "BAD_REQUEST"}
        base = {"ok": True, "receipt_id": key[0], "attempt": key[1],
                "caller_identity_status": UNVERIFIED}
        with self._lock:
            if key in self._poisoned:
                return dict(base, executor_binding_status=BLOCKED, reason="REGISTRATION_CONFLICT")
            reg = self._regs.get(key)
        if reg is None:
            return dict(base, executor_binding_status=UNVERIFIED, reason="NOT_REGISTERED_AT_SPAWN")
        st = self.proc.stat(peer_pid)
        if peer_pid != reg.pid or st is None or st[1] != reg.start_time:
            return dict(base, executor_binding_status=BLOCKED, reason="PEER_CRED_MISMATCH")
        if self.proc.tracer(peer_pid) not in (0,):
            return dict(base, executor_binding_status=BLOCKED, reason="PEER_TRACED")
        if self.degraded:
            return dict(base, executor_binding_status=UNVERIFIED, reason="BROKER_DEGRADED_SAME_UID",
                        downgrade_reason="BROKER_DEGRADED_SAME_UID")
        if reg.origin == BROKER_SPAWNED:
            return self._attest_broker_spawned(reg, base)      # O21c, unreachable today
        # The binding held. It still says only that *some* uid-1000 process
        # registered a child of its own against this (receipt_id, attempt), and
        # codex's reproduction is exactly an attacker doing that with a victim's
        # receipt_id. Evidence, not a promotion.
        return dict(base, executor_binding_status=UNVERIFIED,
                    reason="PEER_CRED_BOUND_EVIDENCE_ONLY",
                    downgrade_reason=DISPATCHER_REGISTRATION_UNAUTHENTICATED,
                    binding_evidence={"pid": reg.pid, "start_time": reg.start_time,
                                      "registered_by": reg.registered_by,
                                      "origin": reg.origin})

    # ── O21c — DEFERRED, and the only function in this module that may say
    #    VERIFIED. Reachable only from a Registration with origin
    #    BROKER_SPAWNED, which only `spawn()` creates, and `spawn()` returns
    #    UNAVAILABLE. Pinned by
    #    tests/unit/test_sev0_cea_s3.py::test_no_verified_promotion_path_outside_the_broker.

    def _attest_broker_spawned(self, reg: "Registration", base: dict) -> dict:
        """The executor is the process *this* broker forked, under a uid the
        dispatcher does not have. Then ``(pid, start_time)`` is the broker's own
        observation and SO_PEERCRED closes the loop."""
        return dict(base, executor_binding_status=VERIFIED, reason="BROKER_SPAWNED_BINDING",
                    nonce=reg.nonce, pid=reg.pid, start_time=reg.start_time)

    def spawn(self, req) -> dict:
        """O21c — DEFERRED. See the module docstring for the four requirements.

        It returns UNAVAILABLE rather than raising, and rather than not existing:
        a caller that wants a real binding needs to be able to ask for one and be
        told, in the receipt, that it could not have it. A missing method would
        instead be an AttributeError somewhere, or a silent fallback to the
        peer-asserted path — which is the bug.
        """
        if not isinstance(req, BrokerSpawnRequest):
            try:
                req = BrokerSpawnRequest(receipt_id=str(req["receipt_id"]),
                                         attempt=int(req["attempt"]),
                                         argv=tuple(req.get("argv") or ()),
                                         credential=req.get("credential"))
            except (KeyError, TypeError, ValueError):
                return {"ok": False, "error": "BAD_REQUEST"}
        return {"ok": False, "error": "BROKER_SPAWN_UNAVAILABLE", "status": "UNAVAILABLE",
                "deferred": "O21c", "receipt_id": req.receipt_id, "attempt": req.attempt,
                "requires": ["broker_runs_as_crew_authz_uid",
                             "dispatcher_runs_as_a_different_uid",
                             "broker_performs_the_fork_exec",
                             "dispatcher_spawn_credential_checked_by_hmac",
                             "kernel.yama.ptrace_scope>=1"]}

    # -- I/O -------------------------------------------------------------

    @property
    def sock_path(self) -> str:
        return os.path.join(self.sock_dir, SOCKET_NAME)

    def _is_client_group(self, gid: int) -> bool:
        """Is ``gid`` a group one of the configured client uids is actually in?"""
        for uid in self.client_uids:
            try:
                ent = pwd.getpwuid(uid)
            except KeyError:
                continue
            if gid == ent.pw_gid:
                return True
            try:
                if gid in os.getgrouplist(ent.pw_name, ent.pw_gid):
                    return True
            except OSError:
                continue
        return False

    def bind(self) -> None:
        st = os.lstat(self.sock_dir)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise BrokerRefused(f"{self.sock_dir} is not a directory owned by euid {os.geteuid()}")
        mode = stat.S_IMODE(st.st_mode)
        # ⛔0710 with the client group, and nothing else. The 0711 variant let
        #   *every* uid on the host traverse to the socket inode, which is why
        #   the socket itself then had to be 0666 — and a 0666 socket is not a
        #   boundary, it is a formality. codex P1 named the fallback; it does not
        #   fix the peer-assertion hole either way, so it buys nothing to keep.
        if mode != 0o710:
            raise BrokerRefused(
                f"{self.sock_dir} is mode {mode:04o}; the only accepted mode is 0710 owned by "
                f"{SERVICE_USER} with the client uid's group. 0711 + a 0666 socket admitted every "
                f"uid on the host and was removed")
        if not self._is_client_group(st.st_gid):
            raise BrokerRefused(
                f"{self.sock_dir} has gid {st.st_gid}, which is not a group of any client uid "
                f"{self.client_uids}; 0710 to a group the clients are not in serves nobody")
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.sock_path)
        # Connect needs write on the socket inode. Group only: the directory is
        # 0710 to the client group, so group is exactly the set that can traverse.
        os.chmod(self.sock_path, 0o660)
        s.listen(64)
        self._sock = s

    def serve_forever(self) -> None:
        assert self._sock is not None, "bind() first"
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _one(self, conn: socket.socket) -> None:
        with conn:
            try:
                pid, uid, _gid = peer_cred(conn)
                conn.settimeout(5.0)
                buf = b""
                while b"\n" not in buf and len(buf) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.decode("utf-8") or "{}")
                resp = self.handle(pid, uid, req)
            except Exception as exc:              # noqa: BLE001 — a bad request is an answer, not a crash
                resp = {"ok": False, "error": f"BAD_REQUEST: {type(exc).__name__}"}
            try:
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            except OSError:
                pass


# ───────────────────────── client side ────────────────────────────────────────

class BrokerClient:
    """Dispatcher/executor side. Trusts a VERIFIED answer only from the service uid.

    The check is ``SO_PEERCRED`` on the *client's* socket: a same-uid process
    that managed to answer on the socket path would be uid 1000, not the broker,
    and its VERIFIED is downgraded to UNVERIFIED here.
    """

    def __init__(self, sock_path: Optional[str] = None, *, expected_uid: Optional[int] = None,
                 timeout: float = 5.0, env: Optional[dict] = None):
        e = os.environ if env is None else env
        d = (e.get("AGENT_CREW_AUTHZ_SOCK_DIR") or "").strip() or \
            f"/tmp/crew-authz-{service_uid() if service_uid() is not None else 'none'}"
        self.sock_path = sock_path or os.path.join(d, SOCKET_NAME)
        self.expected_uid = expected_uid if expected_uid is not None else service_uid()
        self.timeout = timeout

    def _call(self, req: dict) -> tuple[dict, Optional[int]]:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.sock_path)
            _pid, uid, _gid = peer_cred(s)
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8")), uid
        finally:
            s.close()

    def register(self, pid: int, start_time: int, receipt_id: str, attempt: int) -> dict:
        try:
            resp, _ = self._call({"op": "register", "pid": pid, "start_time": start_time,
                                  "receipt_id": receipt_id, "attempt": attempt})
            return resp
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"BROKER_UNREACHABLE: {exc}"}

    def attest(self, receipt_id: str, attempt: int) -> dict:
        """``{executor_binding_status, caller_identity_status, reason}``; never raises."""
        down = {"executor_binding_status": UNVERIFIED, "caller_identity_status": UNVERIFIED}
        try:
            resp, uid = self._call({"op": "attest", "receipt_id": receipt_id, "attempt": attempt})
        except (OSError, ValueError) as exc:
            return dict(down, reason=f"BROKER_UNREACHABLE: {type(exc).__name__}")
        status = resp.get("executor_binding_status")
        if status == VERIFIED and (self.expected_uid is None or uid != self.expected_uid):
            return dict(down, reason="BROKER_PEER_NOT_SERVICE_UID",
                        downgrade_reason="BROKER_PEER_NOT_SERVICE_UID")
        if status not in (VERIFIED, BLOCKED):
            status = UNVERIFIED
        # caller-role identity is never VERIFIED (ADR r3), whatever was answered.
        # `binding_evidence` is passed through so a receipt can record the tuple
        # the broker observed without anyone mistaking it for a promotion — the
        # status next to it is UNVERIFIED and says why.
        return {"executor_binding_status": status, "caller_identity_status": UNVERIFIED,
                "reason": resp.get("reason"),
                "downgrade_reason": resp.get("downgrade_reason"),
                "binding_evidence": resp.get("binding_evidence"),
                "nonce": resp.get("nonce") if status == VERIFIED else None}

    def spawn(self, req: "BrokerSpawnRequest") -> dict:
        """O21c — DEFERRED; the broker answers UNAVAILABLE. Present so callers
        can ask and be refused instead of silently using the peer-asserted path."""
        try:
            resp, _ = self._call({"op": "spawn", "receipt_id": req.receipt_id,
                                  "attempt": req.attempt, "argv": list(req.argv),
                                  "credential": req.credential})
            return resp
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"BROKER_UNREACHABLE: {exc}", "status": "UNAVAILABLE"}


def start_time_of(pid: int, proc: Optional[ProcReader] = None) -> Optional[int]:
    """What the dispatcher sends with ``register`` right after spawn."""
    st = (proc or ProcReader()).stat(pid)
    return None if st is None else st[1]


# ───────────────────────── entry point ────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent_crew.cea.broker")
    ap.add_argument("--sock-dir", required=True)
    ap.add_argument("--client-uid", type=int, action="append", default=None)
    ap.add_argument("--degraded", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    b = Broker(a.sock_dir, degraded=a.degraded, client_uids=tuple(a.client_uid or (1000,)))
    try:
        report = b.preflight()
    except BrokerRefused as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}), file=sys.stderr)
        return 3
    if a.check:
        print(json.dumps(dict(report, ok=True)))
        return 0
    b.bind()
    print(json.dumps(dict(report, ok=True, listening=b.sock_path, at=time.time())), flush=True)
    b.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
