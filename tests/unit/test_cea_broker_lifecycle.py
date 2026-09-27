"""Lifecycle tests use disposable state and never address the installed broker."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading

import pytest

from agent_crew.cea import broker_lifecycle as life
from agent_crew.cea.broker import Broker


def test_health_op_requires_caller_token(tmp_path):
    class Authenticator:
        def authenticate(self, token):
            return object() if token == "secret" else None

    broker = Broker(str(tmp_path), degraded=True,
                    authenticator=Authenticator())
    # The no-op does not need a decision engine or mutate broker registrations.
    assert broker.handle(os.getpid(), os.geteuid(), {"op": "health", "credential": "wrong"})["ok"] is False
    assert broker.handle(os.getpid(), os.geteuid(), {"op": "health", "credential": "secret"}) == {"ok": True}
    assert broker.handle(os.getpid(), os.geteuid(), {"op": "status"})["registrations"] == 0


def test_health_connects_and_authenticates_in_fake_socket_dir(tmp_path):
    token = tmp_path / "tokens.json"
    token.write_text(json.dumps({"adapters": {"secret": {"principal": "x", "provenance": "http"}}}))
    sock = tmp_path / "broker.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(sock))
    server.listen(1)
    seen = []

    def serve():
        conn, _ = server.accept()
        with conn:
            seen.append(json.loads(conn.recv(4096)))
            conn.sendall(b'{"ok":true}\n')
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    life.health(tmp_path, token, os.geteuid())
    thread.join(timeout=2)
    assert seen == [{"op": "health", "credential": "secret"}]


def test_stale_pidfile_is_removed(tmp_path):
    pidfile = tmp_path / "broker.pid"
    pidfile.write_text(json.dumps({"pid": 99999999, "start_time": "1"}))
    assert life._broker_pid(pidfile, tmp_path, os.geteuid()) is None
    assert not pidfile.exists()
    life.stop(pidfile, tmp_path, os.geteuid())  # idempotent


def test_foreign_pid_is_never_signalled(tmp_path, monkeypatch):
    pidfile = tmp_path / "broker.pid"
    pidfile.write_text(json.dumps({"pid": os.getpid(), "start_time": life._start_time(os.getpid())}))
    signals = []
    monkeypatch.setattr(life.os, "kill", lambda *args: signals.append(args))
    with pytest.raises(life.LifecycleError, match="not this broker"):
        life.stop(pidfile, tmp_path, os.geteuid())
    assert signals == []


def test_double_start_only_checks_health(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(life, "_broker_pid", lambda *args: 123)
    checked = []
    monkeypatch.setattr(life, "health", lambda *args: checked.append(args))
    monkeypatch.setattr(life.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("spawned twice"))
    life.start(tmp_path / "broker.pid", tmp_path, os.geteuid(), tmp_path / "tokens", "python", "1000")
    assert len(checked) == 1
    assert "already running" in capsys.readouterr().out


def test_restart_failure_is_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(life, "stop", lambda *args: None)
    monkeypatch.setattr(life, "start", lambda *args: (_ for _ in ()).throw(life.LifecycleError("start failed")))
    assert life.main(["restart", str(tmp_path), str(os.geteuid()), str(tmp_path / "tokens"), "python", "1000"]) == 1
    assert "restart failed after stop: start failed" in capsys.readouterr().err


def test_restart_runs_stop_start_and_health(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(life, "stop", lambda *args: calls.append("stop"))
    monkeypatch.setattr(life, "start", lambda *args: calls.append("start"))
    assert life.main(["restart", str(tmp_path), str(os.geteuid()), str(tmp_path / "tokens"), "python", "1000"]) == 0
    assert calls == ["stop", "start"]
    assert "restart complete" in capsys.readouterr().out


def test_health_mode_fails_without_verified_pid(tmp_path, capsys):
    assert life.main(["health", str(tmp_path), str(os.geteuid()), str(tmp_path / "tokens"), "python", "1000"]) == 1
    assert "not running" in capsys.readouterr().err


def test_stop_sends_only_sigterm_to_verified_pid(tmp_path, monkeypatch):
    pidfile = tmp_path / "broker.pid"
    pidfile.write_text("x")
    monkeypatch.setattr(life, "_broker_pid", lambda *args: 123)
    calls = []
    monkeypatch.setattr(life.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(life, "_cmdline", lambda pid: None)
    life.stop(pidfile, tmp_path, os.geteuid())
    assert calls == [(123, signal.SIGTERM)]
    assert not pidfile.exists()


@pytest.mark.parametrize("mode", ["start", "stop", "restart", "health"])
def test_launcher_routes_modes_from_fake_root(tmp_path, mode):
    """Exercise shell mode selection without contacting the installed socket."""
    import grp
    import pwd
    import shutil

    launcher = tmp_path / "root" / "broker-launch.sh"
    launcher.parent.mkdir()
    shutil.copyfile(Path(__file__).parents[2] / "scripts/cea/broker-launch.sh", launcher)
    source = tmp_path / "root" / "src" / "agent_crew" / "cea"
    source.mkdir(parents=True)
    (source / "broker.py").write_text("# fake broker\n")
    (source / "broker_lifecycle.py").write_text("import sys\nprint('mode=' + sys.argv[1])\n")
    for directory in (source.parent, source):
        (directory / "__init__.py").touch()
    scope = tmp_path / "root" / "ptrace_scope"
    scope.write_text("1\n")
    env = dict(os.environ, AGENT_CREW_AUTHZ_CONFIG=str(tmp_path / "root" / "missing.env"),
               AGENT_CREW_AUTHZ_FAKE_EUID=str(pwd.getpwnam("crew-authz").pw_uid),
               AGENT_CREW_AUTHZ_PYTHON=sys.executable,
               AGENT_CREW_AUTHZ_PYTHONPATH=str(tmp_path / "root" / "src"),
               AGENT_CREW_AUTHZ_SOCK_DIR=str(tmp_path / "root" / "sock"),
               AGENT_CREW_AUTHZ_CLIENT_GROUP=grp.getgrgid(os.getgid()).gr_name,
               AGENT_CREW_AUTHZ_PTRACE_SCOPE_FILE=str(scope))
    result = subprocess.run(["bash", str(launcher), f"--{mode}"], env=env,
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert f"mode={mode}" in result.stdout
