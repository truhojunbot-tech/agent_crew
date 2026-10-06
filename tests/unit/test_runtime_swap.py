"""Runtime swap gates run against disposable files, never live listeners."""
import json
import os
import socket
import subprocess
import threading
import urllib.error
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from agent_crew.server import create_app
from scripts.runtime_swap import (api, parse_args, parse_env_file, check_checkout,
                                  complete_environ, require_no_running_work)


def test_argument_parsing_rejects_short_sha_and_unknown_step():
    sha = 'a' * 40
    assert parse_args(['alpha_engine', sha, 'preflight']).sha == sha
    with pytest.raises(SystemExit):
        parse_args(['alpha_engine', 'abc', 'preflight'])
    with pytest.raises(SystemExit):
        parse_args(['alpha_engine', sha, 'deploy'])
    with pytest.raises(SystemExit):
        parse_args(['../other', sha, 'go'])


def test_env_file_preserves_spaces_and_rejects_bad_keys(tmp_path):
    path = tmp_path / 'cea.env'
    path.write_text('AGENT_CREW_CEA_MODE=shadow\nAGENT_CREW_CEA_MEMORY_CMD=python3 /opt/alfred tools/admission_inputs.py\n')
    env = parse_env_file(path)
    assert env['AGENT_CREW_CEA_MEMORY_CMD'] == 'python3 /opt/alfred tools/admission_inputs.py'
    path.write_text('BAD KEY=value\n')
    with pytest.raises(ValueError, match='invalid env'):
        parse_env_file(path)


def test_checkout_refuses_dirty_and_sha_mismatch(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.com'], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Test'], check=True)
    (repo / 'src' / 'agent_crew').mkdir(parents=True)
    (repo / 'a').write_text('a')
    (repo / 'src' / 'agent_crew' / '__init__.py').write_text('')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'base'], check=True)
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    check_checkout(repo, sha)
    with pytest.raises(RuntimeError, match='HEAD'):
        check_checkout(repo, 'b' * 40)
    (repo / 'a').write_text('changed')
    with pytest.raises(RuntimeError, match='dirty'):
        check_checkout(repo, sha)


def test_queue_check_allows_pending_but_refuses_running_and_malformed():
    require_no_running_work([{'status': 'pending'}])
    require_no_running_work([{'status': 'completed'}])
    with pytest.raises(RuntimeError, match='queue not idle: 1 in-progress task'):
        require_no_running_work({'tasks': [{'status': 'pending'}, {'status': 'in_progress'}]})
    for payload in ({'tasks': 'invalid'}, 'invalid', [{'status': 'pending'}, None]):
        with pytest.raises(RuntimeError, match='task response malformed'):
            require_no_running_work(payload)


def test_api_reads_tasks_from_identity_required_server(tmp_path, monkeypatch):
    import scripts.runtime_swap as swap
    with TestClient(create_app(str(tmp_path / 'tasks.db'), project='demo',
                               identity_required=True, watchdog_disabled=True,
                               anomaly_disabled=True)) as client:
        assert client.get('/tasks').status_code == 428
        created = client.post('/tasks', headers={'X-Agent-Crew-Project': 'demo'},
                              json={'task_id': 'swap-task', 'task_type': 'implement',
                                    'description': 'work', 'project': 'demo'})
        assert created.status_code == 201

        def urlopen(request, timeout):
            response = client.get(request.full_url, headers=dict(request.header_items()))
            if response.status_code >= 400:
                raise urllib.error.HTTPError(request.full_url, response.status_code,
                                             response.text, response.headers, None)
            return BytesIO(response.content)

        monkeypatch.setattr(swap.urllib.request, 'urlopen', urlopen)
        tasks = api(8765, '/tasks', 'demo')
        assert any(task['task_id'] == 'swap-task' for task in tasks)


def test_stop_interlock_requires_health_and_database(tmp_path):
    import sqlite3
    from scripts.runtime_swap import paused
    db = tmp_path / 'tasks.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE runtime_stop (id INTEGER PRIMARY KEY, paused INTEGER)')
        conn.execute('INSERT INTO runtime_stop VALUES (1, 1)')
    paused(db, {'stop': {'paused': True}})
    with pytest.raises(RuntimeError, match='STOP'):
        paused(db, {'stop': {'paused': False}})
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE runtime_stop SET paused=0')
    with pytest.raises(RuntimeError, match='STOP'):
        paused(db, {'stop': {'paused': True}})


def test_listener_pid_parses_ss_and_refuses_missing(monkeypatch):
    import scripts.runtime_swap as swap
    monkeypatch.setattr(swap, 'run', lambda *args: 'LISTEN 0 128 127.0.0.1:8105 0.0.0.0:* users:(("python3",pid=1234,fd=7))')
    assert swap.listener_pid(8105) == 1234
    with pytest.raises(RuntimeError, match='no listener'):
        swap.listener_pid(8101)


def _swap_fixture(tmp_path, monkeypatch, *, db_outside=False):
    import sqlite3
    import scripts.runtime_swap as swap
    monkeypatch.setenv('HOME', str(tmp_path))
    directory = tmp_path / '.agent_crew' / 'demo'
    directory.mkdir(parents=True)
    db = (tmp_path / 'elsewhere.db') if db_outside else (directory / 'tasks.db')
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE runtime_stop (id INTEGER PRIMARY KEY, paused INTEGER)')
        conn.execute('INSERT INTO runtime_stop VALUES (1, 1)')
        conn.execute('CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT)')
        conn.execute("INSERT INTO tasks VALUES ('done-1', 'completed')")
    (directory / 'state.json').write_text(json.dumps({'port': 8765, 'db': str(db)}))
    (directory / 'pause.json').write_text('{"paused": true}')
    (directory / 'cea.env').write_text('AGENT_CREW_CEA_MODE=shadow\n')
    monkeypatch.setattr(swap, 'prepare_checkout', lambda *args: None)
    monkeypatch.setattr(swap, 'api', lambda port, path, project: {'stop': {'paused': True}, 'build': {'commit': 'a' * 40}} if path == '/health' else [])
    monkeypatch.setattr(swap, 'listener_pid', lambda port: __import__('os').getpid())
    return swap, directory, db


def test_main_refuses_db_escape_before_any_runtime_call(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch, db_outside=True)
    with pytest.raises(RuntimeError, match='escapes'):
        swap.main(['demo', 'a' * 40, 'preflight'])


def test_main_preflight_and_post_count_mismatch(tmp_path, monkeypatch):
    import sqlite3
    swap, directory, db = _swap_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(swap.os, 'readlink', lambda path: str(tmp_path))
    assert swap.main(['demo', 'a' * 40, 'preflight']) == 0
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    assert (tmp_path / '.sev0-evidence').stat().st_mode & 0o777 == 0o700
    assert (evidence / 'env.pre.nul').exists()
    assert (evidence / 'preflight.json').exists()
    (evidence / 'counts.pre.json').write_text('[ ["completed", 2] ]')
    with pytest.raises(RuntimeError, match='counts changed'):
        swap.main(['demo', 'a' * 40, 'post'])
    (evidence / 'counts.pre.json').write_text('[ ["completed", 1] ]')
    (evidence / 'pending.pre.json').write_text('[]')
    assert swap.main(['demo', 'a' * 40, 'post']) == 0
    assert not (evidence / 'env.pre.nul').exists()
    assert (evidence / 'env.pre.sha256').exists()
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO tasks VALUES ('failed-1', 'failed')")
    with pytest.raises(RuntimeError, match='counts changed'):
        swap.main(['demo', 'a' * 40, 'post'])


def test_post_accepts_hidden_pid_only_with_matching_health(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    assert swap.main(['demo', 'a' * 40, 'preflight']) == 0
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    (evidence / 'counts.pre.json').write_text('[ ["completed", 1] ]')
    (evidence / 'pending.pre.json').write_text('[]')
    monkeypatch.setattr(swap, 'listener_pid',
                        lambda port: (_ for _ in ()).throw(RuntimeError('no listener')))

    assert swap.main(['demo', 'a' * 40, 'post']) == 0


def test_post_hidden_pid_refuses_wrong_or_unreachable_health(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    assert swap.main(['demo', 'a' * 40, 'preflight']) == 0
    monkeypatch.setattr(swap, 'listener_pid',
                        lambda port: (_ for _ in ()).throw(RuntimeError('no listener')))
    monkeypatch.setattr(swap, 'api', lambda port, path, project: {
        'stop': {'paused': True}, 'build': {'commit': 'b' * 40}} if path == '/health' else [])
    with pytest.raises(RuntimeError, match='build SHA differs'):
        swap.main(['demo', 'a' * 40, 'post'])

    monkeypatch.setattr(swap, 'api',
                        lambda port, path, project: (_ for _ in ()).throw(OSError('health down')))
    with pytest.raises(OSError, match='health down'):
        swap.main(['demo', 'a' * 40, 'post'])


def test_preflight_hands_off_to_socket_group_before_proc_capture(tmp_path, monkeypatch):
    swap, directory, _ = _swap_fixture(tmp_path, monkeypatch)
    sock_dir = tmp_path / 'sock'
    sock_dir.mkdir()
    (directory / 'cea.env').write_text(
        f'AGENT_CREW_CEA_MODE=shadow\nAGENT_CREW_CEA_BROKER_SOCKET={sock_dir / "broker.sock"}\n')
    monkeypatch.setattr(swap.os, 'getegid', lambda: os.stat(sock_dir).st_gid + 1)
    commands = []

    def execvp(program, command):
        commands.append((program, command))
        raise RuntimeError('group handoff')

    monkeypatch.setattr(swap.os, 'execvp', execvp)
    with pytest.raises(RuntimeError, match='group handoff'):
        swap.main(['demo', 'a' * 40, 'preflight'])
    assert commands and commands[0][0] == 'sg'
    assert 'preflight' in commands[0][1][-1]
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    assert not (evidence / 'env.pre.nul').exists()


def test_preflight_unreadable_environ_never_records_partial_capture(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    original = type(tmp_path).read_bytes

    def read_bytes(path):
        if str(path).endswith('/environ'):
            raise PermissionError('environ unreadable')
        return original(path)

    monkeypatch.setattr(type(tmp_path), 'read_bytes', read_bytes)
    with pytest.raises(PermissionError, match='environ unreadable'):
        swap.main(['demo', 'a' * 40, 'preflight'])
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    assert not (evidence / 'preflight.json').exists()


def test_pending_only_preflight_and_post_requires_same_ids(tmp_path, monkeypatch):
    import sqlite3
    swap, _, db = _swap_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO tasks VALUES ('pending-1', 'pending')")
    monkeypatch.setattr(swap, 'api', lambda port, path, project: {'stop': {'paused': True}, 'build': {'commit': 'a' * 40}} if path == '/health' else [{'task_id': 'pending-1', 'status': 'pending'}])
    assert swap.main(['demo', 'a' * 40, 'preflight']) == 0
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    (evidence / 'counts.pre.json').write_text(json.dumps([['completed', 1], ['pending', 1]]))
    (evidence / 'pending.pre.json').write_text(json.dumps(['pending-1']))
    assert swap.main(['demo', 'a' * 40, 'post']) == 0
    assert json.loads((evidence / 'counts.post.json').read_text()) == [['completed', 1], ['pending', 1]]
    assert json.loads((evidence / 'pending.post.json').read_text()) == ['pending-1']
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM tasks WHERE task_id='pending-1'")
        conn.execute("INSERT INTO tasks VALUES ('pending-2', 'pending')")
    with pytest.raises(RuntimeError, match='pending task IDs changed'):
        swap.main(['demo', 'a' * 40, 'post'])


def test_main_refuses_mismatched_preflight(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    evidence.mkdir(parents=True)
    (evidence / 'preflight.json').write_text(json.dumps({'sha': 'b' * 40, 'project': 'demo', 'port': 8765}))
    with pytest.raises(RuntimeError, match='does not match'):
        swap.main(['demo', 'a' * 40, 'go'])


@pytest.mark.parametrize('captured', [b'', b'X=Y'])
def test_go_refuses_incomplete_captured_env_before_signalling(tmp_path, monkeypatch, captured):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    evidence.mkdir(parents=True)
    (evidence / 'preflight.json').write_text(json.dumps({
        'sha': 'a' * 40, 'project': 'demo', 'port': 8765, 'pid': os.getpid(),
    }))
    (evidence / 'env.pre.nul').write_bytes(captured)
    signalled = []
    monkeypatch.setattr(swap.os, 'kill', lambda *args: signalled.append(args))

    with pytest.raises(RuntimeError, match='captured environment missing or incomplete'):
        swap.main(['demo', 'a' * 40, 'go'])
    assert signalled == []
    assert not (evidence / 'tasks.db.pre').exists()


def test_complete_environ_preserves_valid_capture():
    assert complete_environ(b'X=Y\0') == b'X=Y\0'


def test_spawn_captured_env_preserves_spaces_and_drops_old_agent_source(tmp_path, monkeypatch):
    import scripts.spawn_from_env as spawn
    old = tmp_path / 'unusual' / 'worktree' / 'src'
    (old / 'agent_crew').mkdir(parents=True)
    new = tmp_path / 'new' / 'src'
    new.mkdir(parents=True)
    env_file = tmp_path / 'env.nul'
    env_file.write_bytes(f'PYTHONPATH={old}:/other/lib\0AGENT_CREW_CEA_MODE=old\0CUSTOM=has spaces\0'.encode())
    cea_file = tmp_path / 'cea.env'
    cea_file.write_text('AGENT_CREW_CEA_MODE=shadow\nAGENT_CREW_CEA_MEMORY_CMD=python3 /path with spaces/tool.py\n')
    captured = {}
    class Process:
        pid = 42
    def popen(argv, **kwargs):
        captured.update(kwargs)
        return Process()
    monkeypatch.setattr(spawn.subprocess, 'Popen', popen)
    assert spawn.spawn(env_file, cea_file, tmp_path / 'log', 8765, new) == 42
    assert captured['env']['CUSTOM'] == 'has spaces'
    assert captured['env']['AGENT_CREW_CEA_MEMORY_CMD'] == 'python3 /path with spaces/tool.py'
    assert captured['env']['AGENT_CREW_CEA_MODE'] == 'shadow'
    assert captured['env']['PYTHONPATH'] == f'{new}:/other/lib'


def test_broker_probe_uses_client_group_and_connects(tmp_path, monkeypatch):
    import scripts.spawn_from_env as spawn
    import grp
    directory = tmp_path / 'sock'
    directory.mkdir()
    path = directory / 'broker.sock'
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    alternate = next((gid for gid in os.getgroups() if gid != os.getegid()), None)
    if alternate is None:
        listener.close()
        pytest.skip('no distinct supplementary group')
    os.chown(directory, -1, alternate)
    os.chmod(directory, 0o710)
    os.chown(path, -1, alternate)
    os.chmod(path, 0o660)
    listener.listen(1)
    accepted = []
    thread = threading.Thread(target=lambda: accepted.append(listener.accept()[0]), daemon=True)
    thread.start()
    env_file = tmp_path / 'env.nul'
    env_file.write_bytes(b'HOME=/tmp\0')
    cea_file = tmp_path / 'cea.env'
    cea_file.write_text(f'AGENT_CREW_CEA_BROKER_SOCKET={path}\n')
    command = spawn.client_command(spawn.launch_env(env_file, cea_file, tmp_path), ['python3', '-c', 'pass'])
    assert command[:3] == ['sg', grp.getgrgid(os.stat(directory).st_gid).gr_name, '-c']
    try:
        spawn.probe_broker(env_file, cea_file, tmp_path)
        thread.join(timeout=5)
        assert accepted
    finally:
        for peer in accepted:
            peer.close()
        listener.close()


def test_broker_probe_refuses_missing_socket(tmp_path):
    import scripts.spawn_from_env as spawn
    env_file = tmp_path / 'env.nul'
    env_file.write_bytes(b'HOME=/tmp\0')
    cea_file = tmp_path / 'cea.env'
    cea_file.write_text(f'AGENT_CREW_CEA_BROKER_SOCKET={tmp_path / "missing.sock"}\n')
    with pytest.raises((FileNotFoundError, RuntimeError, KeyError)):
        spawn.probe_broker(env_file, cea_file, tmp_path)


def test_go_mocked_relaunch_requires_new_build(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    evidence.mkdir(parents=True)
    (evidence / 'preflight.json').write_text(json.dumps({'sha': 'a' * 40, 'project': 'demo', 'port': 8765, 'pid': __import__('os').getpid()}))
    (evidence / 'env.pre.nul').write_bytes(b'X=Y\0')
    (evidence / 'cwd.pre').write_text(str(tmp_path))
    killed = []
    monkeypatch.setattr(swap.os, 'kill', lambda pid, sig: killed.append((pid, sig)))
    calls = iter([__import__('os').getpid(), None])
    def listener(port):
        value = next(calls)
        if value is None:
            raise RuntimeError('no listener')
        return value
    monkeypatch.setattr(swap, 'listener_pid', listener)
    monkeypatch.setattr(swap.time, 'sleep', lambda _: None)
    monkeypatch.setattr(swap.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(swap, 'api', lambda port, path, project: {'stop': {'paused': True}, 'build': {'commit': 'b' * 40}} if path == '/health' else [])
    with pytest.raises(RuntimeError, match='build SHA differs'):
        swap.main(['demo', 'a' * 40, 'go'])
    assert len(killed) == 1
    assert (evidence / 'tasks.db.pre').exists()
    assert json.loads((evidence / 'pending.pre.json').read_text()) == []


def test_go_broker_probe_failure_keeps_old_server(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    evidence.mkdir(parents=True)
    (evidence / 'preflight.json').write_text(json.dumps({
        'sha': 'a' * 40, 'project': 'demo', 'port': 8765, 'pid': os.getpid()}))
    (evidence / 'env.pre.nul').write_bytes(b'X=Y\0')
    killed = []
    monkeypatch.setattr(swap.os, 'kill', lambda *args: killed.append(args))
    def fail_probe(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(swap.subprocess, 'run', fail_probe)
    with pytest.raises(subprocess.CalledProcessError):
        swap.main(['demo', 'a' * 40, 'go'])
    assert killed == []
