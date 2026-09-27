"""Lifecycle for the sudoers-pinned broker launcher.

Only a PID whose /proc command line names this broker and this socket directory
can be signalled. A stale PID is removed; an unrelated live PID is refused.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time


class LifecycleError(RuntimeError):
    pass


def _cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0")
        return raw.decode().split("\0") if raw else None
    except (OSError, UnicodeError):
        return None


def _start_time(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _process_uid(pid: int) -> int | None:
    try:
        return Path(f"/proc/{pid}").stat().st_uid
    except OSError:
        return None


def _exact_broker_cmd(cmd: list[str] | None, sock_dir: Path, *, allow_degraded: bool = False) -> bool:
    if allow_degraded and cmd and cmd[-1] == "--degraded":
        cmd = cmd[:-1]
    if not cmd or len(cmd) not in (5, 7):
        return False
    if cmd[1:5] != ["-m", "agent_crew.cea.broker", "--sock-dir", str(sock_dir)]:
        return False
    return len(cmd) == 5 or (cmd[5] == "--client-uid" and cmd[6].isdigit())


def _socket_peer(sock_path: Path) -> tuple[int, int] | None:
    if not sock_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1)
            connection.connect(str(sock_path))
            pid, uid, _ = struct.unpack("3i", connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            return pid, uid
    except OSError as exc:
        raise LifecycleError(f"cannot verify socket peer at {sock_path}: {exc}") from exc


def verified_orphan_pid(sock_dir: Path, service_uid: int, *, allow_degraded: bool = False) -> int | None:
    """Identify a no-pidfile broker by the connected socket peer, not PID scans."""
    peer = _socket_peer(sock_dir / "broker.sock")
    if peer is None:
        return None
    pid, peer_uid = peer
    if pid <= 1 or peer_uid != service_uid or _process_uid(pid) != service_uid:
        raise LifecycleError(f"socket peer PID {pid} is not broker uid {service_uid}")
    if not _exact_broker_cmd(_cmdline(pid), sock_dir, allow_degraded=allow_degraded):
        raise LifecycleError(f"socket peer PID {pid} is not this broker; refusing to signal")
    if _start_time(pid) is None:
        raise LifecycleError(f"cannot verify socket peer PID {pid} start time")
    return pid


def _broker_pid(pidfile: Path, sock_dir: Path, service_uid: int) -> int | None:
    if not pidfile.exists():
        return None
    try:
        record = json.loads(pidfile.read_text())
        pid = int(record["pid"])
        born = str(record["start_time"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LifecycleError(f"invalid pidfile {pidfile}: {exc}") from exc
    if pid <= 1:
        raise LifecycleError(f"invalid broker PID {pid}")
    cmd = _cmdline(pid)
    if cmd is None:
        # /proc absence is a stale process, never a signal target.
        pidfile.unlink(missing_ok=True)
        return None
    expected = ["-m", "agent_crew.cea.broker", "--sock-dir", str(sock_dir)]
    if not any(cmd[i:i + len(expected)] == expected for i in range(len(cmd))):
        raise LifecycleError(f"PID {pid} is not this broker; refusing to signal")
    if _start_time(pid) != born:
        raise LifecycleError(f"PID {pid} start time changed; refusing to signal")
    try:
        actual_uid = Path(f"/proc/{pid}").stat().st_uid
    except OSError as exc:
        raise LifecycleError(f"cannot verify PID {pid} uid: {exc}") from exc
    if actual_uid != service_uid:
        raise LifecycleError(f"PID {pid} uid {actual_uid} is not broker uid {service_uid}")
    return pid


def health(sock_dir: Path, tokens_path: Path, service_uid: int) -> None:
    try:
        tokens = json.loads(tokens_path.read_text())["adapters"]
        credential = next(iter(tokens))
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(2)
            connection.connect(str(sock_dir / "broker.sock"))
            if hasattr(socket, "SO_PEERCRED"):
                import struct
                peer_uid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
                if peer_uid != service_uid:
                    raise LifecycleError(f"socket peer uid {peer_uid} is not broker uid {service_uid}")
            connection.sendall((json.dumps({"op": "health", "credential": credential}) + "\n").encode())
            response = b""
            while not response.endswith(b"\n") and len(response) < 65536:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response += chunk
        if json.loads(response).get("ok") is not True:
            raise LifecycleError("authenticated health response refused")
    except (OSError, ValueError, KeyError, StopIteration, TimeoutError) as exc:
        raise LifecycleError(f"authenticated health failed: {exc}") from exc


def stop(pidfile: Path, sock_dir: Path, service_uid: int, *, timeout: float = 10,
         allow_degraded: bool = False) -> None:
    pid = _broker_pid(pidfile, sock_dir, service_uid)
    orphan = pid is None
    if orphan:
        pid = verified_orphan_pid(sock_dir, service_uid, allow_degraded=allow_degraded)
    if pid is None:
        print("broker already stopped", flush=True)
        return
    born = _start_time(pid)
    if born is None or _process_uid(pid) != service_uid or not _exact_broker_cmd(
            _cmdline(pid), sock_dir, allow_degraded=allow_degraded):
        raise LifecycleError(f"broker PID {pid} changed before signal; refusing")
    if orphan and _socket_peer(sock_dir / "broker.sock") != (pid, service_uid):
        raise LifecycleError(f"broker socket peer changed before signal; refusing PID {pid}")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _cmdline(pid) is None:
            pidfile.unlink(missing_ok=True)
            print(f"broker stopped pid={pid}", flush=True)
            return
        if _start_time(pid) != born:
            raise LifecycleError(f"broker PID {pid} changed after SIGTERM")
        time.sleep(0.1)
    raise LifecycleError(f"broker PID {pid} did not stop after SIGTERM within {timeout:g}s; no SIGKILL sent")


def start(pidfile: Path, sock_dir: Path, service_uid: int, tokens_path: Path,
          python: str, client_uid: str, *, timeout: float = 10,
          env: dict[str, str] | None = None, degraded: bool = False) -> None:
    old = _broker_pid(pidfile, sock_dir, service_uid)
    if old is not None:
        health(sock_dir, tokens_path, service_uid)
        print(f"broker already running pid={old}", flush=True)
        return
    if (sock_dir / "broker.sock").exists():
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(0.5)
            try:
                probe.connect(str(sock_dir / "broker.sock"))
            except OSError:
                pass  # An orphaned socket may be replaced by Broker.bind().
            else:
                raise LifecycleError("broker socket answers without a verified pidfile; refusing double start")
    log = sock_dir / "broker.log"
    with log.open("ab") as output:
        command = [python, "-m", "agent_crew.cea.broker", "--sock-dir", str(sock_dir),
                   "--client-uid", client_uid]
        if degraded:
            command.append("--degraded")
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                env=env)
    born = _start_time(proc.pid)
    if born is None:
        raise LifecycleError("broker exited before its PID could be recorded")
    tmp = pidfile.with_suffix(".new")
    tmp.write_text(json.dumps({"pid": proc.pid, "start_time": born}) + "\n")
    tmp.replace(pidfile)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pidfile.unlink(missing_ok=True)
            raise LifecycleError(f"broker exited during start (code {proc.returncode}); see {log}")
        try:
            health(sock_dir, tokens_path, service_uid)
            print(f"broker started pid={proc.pid}; authenticated health ok", flush=True)
            return
        except LifecycleError:
            time.sleep(0.1)
    try:
        stop(pidfile, sock_dir, service_uid)
    except LifecycleError as exc:
        raise LifecycleError(f"broker failed authenticated health; cleanup failed: {exc}; see {log}") from exc
    raise LifecycleError(f"broker failed authenticated health within {timeout:g}s; stopped; see {log}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("start", "stop", "restart", "health"))
    parser.add_argument("sock_dir", type=Path)
    parser.add_argument("service_uid", type=int)
    parser.add_argument("tokens_path", type=Path)
    parser.add_argument("python")
    parser.add_argument("client_uid")
    args = parser.parse_args(argv)
    pidfile = args.sock_dir / "broker.pid"
    try:
        if args.mode == "stop":
            stop(pidfile, args.sock_dir, args.service_uid)
        elif args.mode == "health":
            if _broker_pid(pidfile, args.sock_dir, args.service_uid) is None:
                raise LifecycleError("broker is not running")
            health(args.sock_dir, args.tokens_path, args.service_uid)
            print("broker authenticated health ok", flush=True)
        elif args.mode == "start":
            start(pidfile, args.sock_dir, args.service_uid, args.tokens_path, args.python, args.client_uid)
        else:
            stop(pidfile, args.sock_dir, args.service_uid)
            try:
                start(pidfile, args.sock_dir, args.service_uid, args.tokens_path, args.python, args.client_uid)
            except LifecycleError as exc:
                raise LifecycleError(f"restart failed after stop: {exc}") from exc
            print("broker restart complete", flush=True)
    except LifecycleError as exc:
        print(f"broker-launch: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
