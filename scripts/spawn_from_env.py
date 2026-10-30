#!/usr/bin/env python3
"""Relaunch crew using project-declared fleet and CEA environment files."""
from __future__ import annotations

import re
import os
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from agent_crew.cea_launch import client_command, parse_env_file

KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def parse_env(raw: bytes) -> dict[str, str]:
    env = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        name, value = item.split(b"=", 1)
        key = name.decode("ascii")
        if KEY.fullmatch(key):
            env[key] = value.decode("utf-8", "surrogateescape")
    return env


def launch_env(env_file: Path, cea_file: Path, source: Path) -> dict[str, str]:
    base_keys = ("HOME", "USER", "PATH", "LANG")
    missing = [key for key in base_keys if key not in os.environ]
    if missing:
        raise RuntimeError(f"swap invoker missing base keys: {', '.join(missing)}")
    env = {key: os.environ[key] for key in base_keys}
    fleet = parse_env_file(env_file, prefix=None, require_nonempty=False)
    forbidden = set(fleet) & {*base_keys, "PYTHONPATH"}
    forbidden.update(key for key in fleet if key.startswith("AGENT_CREW_CEA_"))
    if forbidden:
        raise ValueError(f"fleet.env has reserved keys: {', '.join(sorted(forbidden))}")
    env.update(fleet)
    env.update(parse_env_file(cea_file))
    env["PYTHONPATH"] = str(source)
    return env


def probe_broker(env_file: Path, cea_file: Path, source: Path) -> None:
    env = launch_env(env_file, cea_file, source)
    sock = env.get("AGENT_CREW_CEA_BROKER_SOCKET")
    if not sock:
        return
    code = "import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(sys.argv[1]); s.close()"
    probe = subprocess.run(client_command(env, [sys.executable, "-c", code, sock]),
                           env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=10)
    if probe.returncode:
        raise RuntimeError("new server cannot connect to broker socket")


def spawn(env_file: Path, cea_file: Path, log: Path, port: int, source: Path) -> int:
    env = launch_env(env_file, cea_file, source)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        process = subprocess.Popen(
            client_command(env, [sys.executable, "-m", "uvicorn", "agent_crew.server:app",
                                 "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"]), env=env,
            stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
    return process.pid


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--probe-broker":
        probe_broker(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
    elif len(sys.argv) == 6:
        print(spawn(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]),
                    int(sys.argv[4]), Path(sys.argv[5])))
    else:
        raise SystemExit("usage: spawn_from_env.py [--probe-broker FLEET_ENV CEA_ENV NEW_SRC | FLEET_ENV CEA_ENV LOG PORT NEW_SRC]")
