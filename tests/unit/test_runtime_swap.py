"""Runtime swap gates run against disposable files, never live listeners."""
import json
import subprocess

import pytest

from scripts.runtime_swap import parse_args, parse_env_file, check_checkout, require_empty_queue


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


def test_nonempty_queue_refused():
    with pytest.raises(RuntimeError, match='queue not empty'):
        require_empty_queue([{'status': 'pending'}])
    with pytest.raises(RuntimeError, match='queue not empty'):
        require_empty_queue({'tasks': [{'status': 'in_progress'}]})
    require_empty_queue([{'status': 'completed'}])


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
        conn.execute('CREATE TABLE tasks (status TEXT)')
        conn.execute("INSERT INTO tasks VALUES ('completed')")
    (directory / 'state.json').write_text(json.dumps({'port': 8765, 'db': str(db)}))
    (directory / 'pause.json').write_text('{"paused": true}')
    (directory / 'cea.env').write_text('AGENT_CREW_CEA_MODE=shadow\n')
    monkeypatch.setattr(swap, 'prepare_checkout', lambda *args: None)
    monkeypatch.setattr(swap, 'api', lambda port, path: {'stop': {'paused': True}, 'build': {'commit': 'a' * 40}} if path == '/health' else [])
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
    assert swap.main(['demo', 'a' * 40, 'post']) == 0
    assert not (evidence / 'env.pre.nul').exists()
    assert (evidence / 'env.pre.sha256').exists()
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO tasks VALUES ('failed')")
    with pytest.raises(RuntimeError, match='counts changed'):
        swap.main(['demo', 'a' * 40, 'post'])


def test_main_refuses_mismatched_preflight(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    evidence.mkdir(parents=True)
    (evidence / 'preflight.json').write_text(json.dumps({'sha': 'b' * 40, 'project': 'demo', 'port': 8765}))
    with pytest.raises(RuntimeError, match='does not match'):
        swap.main(['demo', 'a' * 40, 'go'])


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
    monkeypatch.setattr(swap, 'api', lambda port, path: {'stop': {'paused': True}, 'build': {'commit': 'b' * 40}} if path == '/health' else [])
    with pytest.raises(RuntimeError, match='build SHA differs'):
        swap.main(['demo', 'a' * 40, 'go'])
    assert len(killed) == 1
    assert (evidence / 'tasks.db.pre').exists()
