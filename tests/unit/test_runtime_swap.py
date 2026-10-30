"""Runtime swap gates run against disposable files, never live listeners."""
import json
import hashlib
import os
import socket
import subprocess
import sys
import threading
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_crew.server import create_app
from scripts.runtime_swap import (api, parse_args, parse_env_file, check_checkout,
                                  complete_environ, require_no_running_work)


@pytest.mark.parametrize(('project', 'hold_message'), [
    ('digital-seller', '13024'),
    ('smart-money-alpha', '13024'),
    ('ht-8004', '13020'),
    ('agent_crew', None),
])
def test_shell_precheck_runs_before_swap(tmp_path, project, hold_message):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    precheck = bin_dir / 'owner-state'
    precheck.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$2" > "$SWAP_PRECHECK_MARKER"
case "$2" in
  digital-seller|smart-money-alpha) echo 'owner HOLD 13024'; exit 9 ;;
  ht-8004) echo 'owner HOLD 13020/13619'; exit 9 ;;
  *) echo 'owner FREE'; exit 0 ;;
esac
''')
    precheck.chmod(0o755)
    fake_python = bin_dir / 'python3'
    fake_python.write_text('''#!/usr/bin/env bash
if [[ $1 == *swap_precheck_receipt.py ]]; then exec "$SWAP_REAL_PYTHON" "$@"; fi
printf "called\\n" > "$SWAP_RUNTIME_MARKER"
''')
    fake_python.chmod(0o755)
    precheck_marker = tmp_path / 'precheck.called'
    runtime_marker = tmp_path / 'runtime.called'
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
           'AGENT_CREW_SWAP_PRECHECK': f'{precheck} --project',
           'AGENT_CREW_SWAP_RECEIPT_DIR': str(tmp_path / 'receipts'),
           'AGENT_CREW_SWAP_ACTOR': 'test-owner', 'SWAP_REAL_PYTHON': sys.executable,
           'SWAP_PRECHECK_MARKER': str(precheck_marker),
           'SWAP_RUNTIME_MARKER': str(runtime_marker)}
    script = Path(__file__).resolve().parents[2] / 'scripts/runtime_swap.sh'
    result = subprocess.run(['bash', str(script), project, 'a' * 40, 'go'],
                            env=env, text=True, capture_output=True)
    assert precheck_marker.read_text().strip() == project
    receipts = list((tmp_path / 'receipts').glob('*.json'))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt['project'] == project
    assert receipt['target_sha'] == 'a' * 40
    assert receipt['step'] == 'go'
    assert receipt['actor'] == 'test-owner'
    assert receipt['precheck_command'] == f'{precheck} --project {project}'
    assert receipt['rc'] == (9 if hold_message else 0)
    output = f'owner HOLD {hold_message}' if hold_message else 'owner FREE'
    if project == 'ht-8004':
        output = 'owner HOLD 13020/13619'
    assert receipt['output_sha256'] == hashlib.sha256((output + '\n').encode()).hexdigest()
    assert receipt['timestamp_utc'].endswith('Z')
    if hold_message:
        assert result.returncode != 0
        assert hold_message in result.stdout + result.stderr
        assert not runtime_marker.exists()
    else:
        assert result.returncode == 0
        assert runtime_marker.exists()


@pytest.mark.parametrize(('precheck', 'allowed'), [(None, False), ('none', True)])
def test_shell_precheck_requires_explicit_configuration(tmp_path, precheck, allowed):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake_python = bin_dir / 'python3'
    fake_python.write_text('''#!/usr/bin/env bash
if [[ $1 == *swap_precheck_receipt.py ]]; then exec "$SWAP_REAL_PYTHON" "$@"; fi
printf "called\\n" > "$SWAP_RUNTIME_MARKER"
''')
    fake_python.chmod(0o755)
    runtime_marker = tmp_path / 'runtime.called'
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
           'AGENT_CREW_SWAP_RECEIPT_DIR': str(tmp_path / 'receipts'),
           'SWAP_REAL_PYTHON': sys.executable,
           'SWAP_RUNTIME_MARKER': str(runtime_marker)}
    env.pop('AGENT_CREW_SWAP_PRECHECK', None)
    if precheck is not None:
        env['AGENT_CREW_SWAP_PRECHECK'] = precheck
    script = Path(__file__).resolve().parents[2] / 'scripts/runtime_swap.sh'
    result = subprocess.run(['bash', str(script), 'agent_crew', 'a' * 40, 'go'],
                            env=env, text=True, capture_output=True)
    assert (result.returncode == 0) is allowed
    assert runtime_marker.exists() is allowed
    if not allowed:
        assert 'AGENT_CREW_SWAP_PRECHECK' in result.stdout + result.stderr
        assert not (tmp_path / 'receipts').exists()
    else:
        receipt = json.loads(next((tmp_path / 'receipts').glob('*.json')).read_text())
        assert receipt['precheck_command'] == 'none'
        assert receipt['rc'] == 0
        assert receipt['output_sha256'] == hashlib.sha256(b'').hexdigest()


def test_shell_precheck_rc3_receipted_and_unwritable_receipt_refuses(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts/runtime_swap.sh'
    precheck = tmp_path / 'precheck'
    precheck.write_text('#!/usr/bin/env bash\necho "owner HOLD"; exit 3\n')
    precheck.chmod(0o755)
    fake_python = tmp_path / 'python3'
    fake_python.write_text('''#!/usr/bin/env bash
if [[ $1 == *swap_precheck_receipt.py ]]; then exec "$SWAP_REAL_PYTHON" "$@"; fi
printf called > "$SWAP_RUNTIME_MARKER"
''')
    fake_python.chmod(0o755)
    marker = tmp_path / 'runtime.called'
    env = {**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}',
           'AGENT_CREW_SWAP_PRECHECK': str(precheck),
           'AGENT_CREW_SWAP_RECEIPT_DIR': str(tmp_path / 'receipts'),
           'SWAP_REAL_PYTHON': sys.executable, 'SWAP_RUNTIME_MARKER': str(marker)}
    args = ['bash', str(script), 'agent_crew', 'a' * 40, 'go']
    result = subprocess.run(args, env=env, text=True, capture_output=True)
    assert result.returncode == 3
    assert 'owner HOLD' in result.stderr
    receipt = json.loads(next((tmp_path / 'receipts').glob('*.json')).read_text())
    assert receipt['rc'] == 3
    assert receipt['output_sha256'] == hashlib.sha256(b'owner HOLD\n').hexdigest()
    assert not marker.exists()

    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('cannot create receipts here')
    env['AGENT_CREW_SWAP_RECEIPT_DIR'] = str(blocked)
    env['AGENT_CREW_SWAP_PRECHECK'] = 'none'
    result = subprocess.run(args, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert 'receipt' in result.stderr.lower()
    assert not marker.exists()


def test_shell_preserves_owner_hold_when_receipt_write_fails(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts/runtime_swap.sh'
    precheck = tmp_path / 'precheck'
    precheck.write_text('#!/usr/bin/env bash\necho "owner HOLD 13024"; exit 9\n')
    precheck.chmod(0o755)
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('cannot create receipts here')
    result = subprocess.run(
        ['bash', str(script), 'agent_crew', 'a' * 40, 'go'],
        env={**os.environ, 'AGENT_CREW_SWAP_PRECHECK': str(precheck),
             'AGENT_CREW_SWAP_RECEIPT_DIR': str(blocked)},
        text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert 'owner HOLD 13024' in result.stderr
    assert 'swap precheck refused agent_crew (rc=9)' in result.stderr
    assert 'receipt could not be written' in result.stderr


def test_shell_removes_precheck_output_before_successful_exec(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts/runtime_swap.sh'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake_python = bin_dir / 'python3'
    fake_python.write_text('''#!/usr/bin/env bash
if [[ $1 == *swap_precheck_receipt.py ]]; then exec "$SWAP_REAL_PYTHON" "$@"; fi
exit 0
''')
    fake_python.chmod(0o755)
    temp_dir = tmp_path / 'temporary'
    temp_dir.mkdir()
    result = subprocess.run(
        ['bash', str(script), 'agent_crew', 'a' * 40, 'preflight'],
        env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
             'TMPDIR': str(temp_dir), 'AGENT_CREW_SWAP_PRECHECK': 'none',
             'AGENT_CREW_SWAP_RECEIPT_DIR': str(tmp_path / 'receipts'),
             'SWAP_REAL_PYTHON': sys.executable},
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert list(temp_dir.iterdir()) == []


def test_default_actor_records_only_parent_executable(monkeypatch):
    import scripts.swap_precheck_receipt as receipt

    monkeypatch.delenv('AGENT_CREW_SWAP_ACTOR', raising=False)
    monkeypatch.setattr(receipt.getpass, 'getuser', lambda: 'worker')
    monkeypatch.setattr(receipt.socket, 'gethostname', lambda: 'host')
    monkeypatch.setattr(receipt.Path, 'read_bytes',
                        lambda path: b'/usr/bin/bash\0-c\0secret=do-not-store\0')
    assert receipt._actor(1234) == 'worker@host:1234 bash'


def test_shell_precheck_default_receipt_dir_and_actor(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts/runtime_swap.sh'
    fake_python = tmp_path / 'python3'
    fake_python.write_text('''#!/usr/bin/env bash
if [[ $1 == *swap_precheck_receipt.py ]]; then exec "$SWAP_REAL_PYTHON" "$@"; fi
printf called > "$SWAP_RUNTIME_MARKER"
''')
    fake_python.chmod(0o755)
    marker = tmp_path / 'runtime.called'
    env = {**os.environ, 'HOME': str(tmp_path),
           'PATH': f'{tmp_path}:{os.environ["PATH"]}',
           'AGENT_CREW_SWAP_PRECHECK': 'none',
           'SWAP_REAL_PYTHON': sys.executable, 'SWAP_RUNTIME_MARKER': str(marker)}
    env.pop('AGENT_CREW_SWAP_RECEIPT_DIR', None)
    env.pop('AGENT_CREW_SWAP_ACTOR', None)
    result = subprocess.run(['bash', str(script), 'agent_crew', 'a' * 40, 'preflight'],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert marker.exists()
    directory = tmp_path / '.agent_crew' / 'agent_crew' / 'swap-precheck-receipts'
    receipt = json.loads(next(directory.glob('*.json')).read_text())
    assert receipt['step'] == 'preflight'
    assert '@' in receipt['actor'] and ':' in receipt['actor']


def test_argument_parsing_rejects_short_sha_and_unknown_step():
    sha = 'a' * 40
    assert parse_args(['alpha_engine', sha, 'preflight']).sha == sha
    with pytest.raises(SystemExit):
        parse_args(['alpha_engine', 'abc', 'preflight'])
    with pytest.raises(SystemExit):
        parse_args(['alpha_engine', sha, 'deploy'])
    with pytest.raises(SystemExit):
        parse_args(['../other', sha, 'go', '--decision-id', 'T0-TEST'])


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
    monkeypatch.setenv('USER', 'test-user')
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.setenv('LANG', 'C.UTF-8')
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
    (directory / 'fleet.env').write_text('')
    old_source = tmp_path / 'old' / 'src'
    (old_source / 'agent_crew').mkdir(parents=True, exist_ok=True)
    live = {key: os.environ[key] for key in ('HOME', 'USER', 'PATH', 'LANG')}
    live.update({key: value for key, value in os.environ.items() if key == 'LOGNAME' or key.startswith('LC_')})
    live.update(AGENT_CREW_CEA_MODE='shadow', PYTHONPATH=str(old_source))
    monkeypatch.setattr(swap, 'read_live_environ', lambda _pid: b''.join(
        f'{key}={value}'.encode() + b'\0' for key, value in live.items()))
    monkeypatch.setattr(swap, 'prepare_checkout', lambda *args: None)
    monkeypatch.setattr(swap, 'api', lambda port, path, project: {'stop': {'paused': True}, 'build': {'commit': 'a' * 40}, 'project': 'demo'} if path == '/health' else [])
    monkeypatch.setattr(swap, 'listener_pid', lambda port: __import__('os').getpid())
    monkeypatch.setattr(swap, 'recheck_precheck', lambda _project: None)
    monkeypatch.setattr(swap, 'verify_swap_decision', lambda *_args: None)
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


def test_preflight_refuses_undeclared_live_key_with_names_only(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    original = swap.read_live_environ
    monkeypatch.setattr(swap, 'read_live_environ',
                        lambda pid: original(pid) + b'UNDECLARED_TOKEN=private-value\0')

    with pytest.raises(RuntimeError, match='UNDECLARED_TOKEN') as error:
        swap.main(['demo', 'a' * 40, 'preflight'])
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    diff = json.loads((evidence / 'env_diff.json').read_text())
    assert diff['live_but_undeclared'] == ['UNDECLARED_TOKEN']
    assert diff['declared_but_not_live'] == []
    assert 'private-value' not in str(error.value) + (evidence / 'env_diff.json').read_text()
    assert not (evidence / 'preflight.json').exists()

    assert swap.main(['demo', 'a' * 40, 'preflight',
                      '--accept-env-diff', 'UNDECLARED_TOKEN']) == 0
    signalled = []
    monkeypatch.setattr(swap.os, 'kill', lambda *args: signalled.append(args))
    with pytest.raises(RuntimeError, match='go requires --accept-env-diff'):
        swap.main(['demo', 'a' * 40, 'go', '--decision-id', 'T0-TEST'])
    assert signalled == []

    (tmp_path / '.agent_crew' / 'demo' / 'fleet.env').write_text('EXTRA=changed\n')
    with pytest.raises(RuntimeError, match='fleet.env changed'):
        swap.main(['demo', 'a' * 40, 'go', '--decision-id', 'T0-TEST', '--accept-env-diff', 'UNDECLARED_TOKEN'])
    assert signalled == []


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
    with pytest.raises(RuntimeError, match='post refused: health unavailable: health down'):
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
    monkeypatch.setattr(swap, 'read_live_environ', lambda _pid: (
        _ for _ in ()).throw(PermissionError('environ unreadable')))
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
        swap.main(['demo', 'a' * 40, 'go', '--decision-id', 'T0-TEST'])


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
        swap.main(['demo', 'a' * 40, 'go', '--decision-id', 'T0-TEST'])
    assert signalled == []
    assert not (evidence / 'tasks.db.pre').exists()


def test_complete_environ_preserves_valid_capture():
    assert complete_environ(b'X=Y\0') == b'X=Y\0'


def test_spawn_declared_env_preserves_spaces_and_uses_new_agent_source(tmp_path, monkeypatch):
    import scripts.spawn_from_env as spawn
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USER', 'test-user')
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.setenv('LANG', 'C.UTF-8')
    old = tmp_path / 'unusual' / 'worktree' / 'src'
    (old / 'agent_crew').mkdir(parents=True)
    new = tmp_path / 'new' / 'src'
    new.mkdir(parents=True)
    env_file = tmp_path / 'fleet.env'
    env_file.write_text('CUSTOM=has spaces\n')
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
    assert captured['env']['PYTHONPATH'] == str(new)
    assert str(old) not in captured['env']['PYTHONPATH']


def test_broker_probe_uses_client_group_and_connects(tmp_path, monkeypatch):
    import scripts.spawn_from_env as spawn
    import grp
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USER', 'test-user')
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.setenv('LANG', 'C.UTF-8')
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
    env_file = tmp_path / 'fleet.env'
    env_file.write_text('')
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
    env_file = tmp_path / 'fleet.env'
    env_file.write_text('')
    cea_file = tmp_path / 'cea.env'
    cea_file.write_text(f'AGENT_CREW_CEA_BROKER_SOCKET={tmp_path / "missing.sock"}\n')
    with pytest.raises((FileNotFoundError, RuntimeError, KeyError)):
        spawn.probe_broker(env_file, cea_file, tmp_path)


def test_go_mocked_relaunch_requires_new_build(tmp_path, monkeypatch, capsys):
    swap, directory, _ = _swap_fixture(tmp_path, monkeypatch)
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    evidence.mkdir(parents=True)
    (evidence / 'preflight.json').write_text(json.dumps({'sha': 'a' * 40, 'project': 'demo', 'port': 8765, 'pid': __import__('os').getpid()}))
    old_src = tmp_path / 'old' / 'src'
    (old_src / 'agent_crew').mkdir(parents=True, exist_ok=True)
    (evidence / 'env.pre.nul').write_bytes(f'PYTHONPATH={old_src}\0'.encode())
    (evidence / 'cwd.pre').write_text(str(tmp_path))
    swap.write_json(evidence / 'health.pre.json', {'build': {'commit': 'b' * 40}})
    killed = []
    monkeypatch.setattr(swap.os, 'kill', lambda pid, sig: killed.append((pid, sig)))
    calls = iter([__import__('os').getpid(), None])
    def listener(port):
        value = next(calls, None)
        if value is None:
            raise RuntimeError('no listener')
        return value
    monkeypatch.setattr(swap, 'listener_pid', listener)
    monkeypatch.setattr(swap.time, 'sleep', lambda _: None)
    spawns = []
    def run(command, **_kwargs):
        if '--probe-broker' in command:
            return None
        spawns.append(command)
        return type('Result', (), {'stdout': f'{43209 + len(spawns)}\n'})()
    monkeypatch.setattr(swap.subprocess, 'run', run)
    monkeypatch.setattr(swap, 'process_alive', lambda candidate: not any(
        candidate == signalled and sig == swap.signal.SIGTERM for signalled, sig in killed))
    monkeypatch.setattr(swap, 'api', lambda port, path, project: {'stop': {'paused': True}, 'build': {'commit': 'b' * 40}, 'project': 'demo'} if path == '/health' else [])
    with pytest.raises(RuntimeError, match='build SHA differs'):
        swap.main(['demo', 'a' * 40, 'go', '--decision-id', 'T0-TEST'])
    assert len(killed) >= 1
    assert len(spawns) == 2
    assert spawns[1][-1] == str(old_src)
    assert 'ROLLBACK_OK served_sha=' in capsys.readouterr().out
    assert (evidence / 'tasks.db.pre').exists()
    assert json.loads((evidence / 'pending.pre.json').read_text()) == []
    assert not json.loads((directory / 'state.json').read_text()).get('cea_env_expected')


def test_swap_refuses_foreign_cea_override_before_signalling(tmp_path, monkeypatch):
    swap, directory, _ = _swap_fixture(tmp_path, monkeypatch)
    configured = tmp_path / 'configured.env'
    configured.write_text('AGENT_CREW_CEA_MODE=enforce\n')
    (directory / 'cea.env').unlink()
    monkeypatch.setenv('AGENT_CREW_SWAP_CEA_ENV_FILE', str(configured))
    signalled = []
    monkeypatch.setattr(swap.os, 'kill', lambda *args: signalled.append(args))

    with pytest.raises(RuntimeError, match='project-local cea.env'):
        swap.main(['demo', 'a' * 40, 'go', '--decision-id', 'T0-TEST'])
    assert signalled == []



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
        swap.main(['demo', 'a' * 40, 'go', '--decision-id', 'T0-TEST'])
    assert killed == []


def _ready_go(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    evidence = tmp_path / '.sev0-evidence' / 'crew-swap-demo-aaaaaaa'
    evidence.mkdir(parents=True)
    old_src = tmp_path / 'old' / 'src'
    (old_src / 'agent_crew').mkdir(parents=True, exist_ok=True)
    (evidence / 'preflight.json').write_text(json.dumps({
        'sha': 'a' * 40, 'project': 'demo', 'port': 8765, 'pid': os.getpid()}))
    raw = f'PYTHONPATH={old_src}:/other\0X=Y\0'.encode()
    (evidence / 'env.pre.nul').write_bytes(raw)
    (evidence / 'cwd.pre').write_text(str(tmp_path))
    swap.write_json(evidence / 'health.pre.json',
                    {'stop': {'paused': True}, 'build': {'commit': 'b' * 40}, 'project': 'demo'})
    calls = iter([os.getpid(), None])
    def listener(_port):
        value = next(calls, None)
        if value is None:
            raise RuntimeError('no listener')
        return value
    monkeypatch.setattr(swap, 'listener_pid', listener)
    monkeypatch.setattr(swap.os, 'kill', lambda *_args: None)
    return swap, evidence, old_src, raw


def test_previous_source_works_with_script_run_import_path(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'runtime_swap.py'
    old_src = tmp_path / 'old' / 'src'
    (old_src / 'agent_crew').mkdir(parents=True)
    new_src = tmp_path / 'new' / 'src'
    env_dump = tmp_path / 'env.pre.nul'
    env_dump.write_bytes(f'PYTHONPATH={old_src}\0'.encode())
    code = (
        'import runpy, sys; from pathlib import Path; '
        'sys.path.insert(0, str(Path(sys.argv[1]).parent)); '
        'swap = runpy.run_path(sys.argv[1]); '
        'print(swap["previous_source"](Path(sys.argv[2]), Path(sys.argv[3])))'
    )
    result = subprocess.run(
        [sys.executable, '-I', '-c', code, str(script), str(env_dump), str(new_src)],
        cwd=script.parent.parent, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(old_src)


def test_go_waits_past_sixty_seconds_for_live_startup(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    swap, evidence, _, _ = _ready_go(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(swap.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(swap.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(swap, 'process_alive', lambda _pid: True, raising=False)
    monkeypatch.setattr(swap.subprocess, 'run', lambda *_args, **_kwargs: SimpleNamespace(stdout='43210\n'))
    def health(_port, path, _project):
        if path == '/tasks':
            return []
        if clock[0] > 60:
            return {'stop': {'paused': True}, 'build': {'commit': 'a' * 40}, 'project': 'demo'}
        if clock[0] == 0:
            return {'stop': {'paused': True}, 'build': {'commit': 'b' * 40}, 'project': 'demo'}
        raise OSError('starting')
    monkeypatch.setattr(swap, 'api', health)
    assert swap.main(['demo', 'a' * 40, 'go', '--decision-id', 'T0-TEST']) == 0
    assert clock[0] > 60
    assert 'STARTING' in capsys.readouterr().out
    assert not (evidence / 'env.pre.nul').exists()
    assert (evidence / 'env.pre.sha256').exists()


@pytest.mark.parametrize('failure', ['dead', 'budget'])
def test_go_failure_rolls_back_previous_source_and_scrubs_env(tmp_path, monkeypatch, failure, capsys):
    from types import SimpleNamespace
    import hashlib
    swap, evidence, old_src, raw = _ready_go(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(swap.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(swap.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setenv('AGENT_CREW_SWAP_STARTUP_BUDGET_SECONDS', '2')
    spawns = []
    def run(command, **kwargs):
        if '--probe-broker' in command:
            return SimpleNamespace(stdout='')
        spawns.append(command)
        return SimpleNamespace(stdout=f'{43210 + len(spawns)}\n')
    monkeypatch.setattr(swap.subprocess, 'run', run)
    stopped = []
    monkeypatch.setattr(swap.os, 'kill', lambda pid, sig: stopped.append((pid, sig)))
    monkeypatch.setattr(swap, 'process_alive',
                        lambda pid: pid != 43211 if failure == 'dead' else not any(
                            item == (pid, swap.signal.SIGTERM) for item in stopped), raising=False)
    def health(_port, path, _project):
        if path == '/tasks':
            return []
        if not spawns:
            return {'stop': {'paused': True}, 'build': {'commit': 'b' * 40}, 'project': 'demo'}
        if len(spawns) == 2:
            return {'stop': {'paused': True}, 'build': {'commit': 'b' * 40}, 'project': 'demo'}
        raise OSError('starting')
    monkeypatch.setattr(swap, 'api', health)
    with pytest.raises(RuntimeError, match='died|budget'):
        swap.main(['demo', 'a' * 40, 'go', '--decision-id', 'T0-TEST'])
    assert len(spawns) == 2
    assert spawns[1][-1] == str(old_src)
    assert 'ROLLBACK_OK' in capsys.readouterr().out
    assert not (evidence / 'env.pre.nul').exists()
    assert (evidence / 'env.pre.sha256').read_text().strip() == hashlib.sha256(raw).hexdigest()


def test_post_unhealthy_refuses_before_db_counts(tmp_path, monkeypatch):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(swap, 'api', lambda *_args: (_ for _ in ()).throw(OSError('unreachable')))
    monkeypatch.setattr(swap, 'counts', lambda *_args: pytest.fail('opened DB counts'))
    with pytest.raises(RuntimeError, match='post refused.*health unavailable'):
        swap.main(['demo', 'a' * 40, 'post'])


def test_failed_rollback_retains_captured_env_for_manual_recovery(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    swap, evidence, old_src, raw = _ready_go(tmp_path, monkeypatch)
    spawns = []
    def run(command, **_kwargs):
        if '--probe-broker' in command:
            return SimpleNamespace(stdout='')
        spawns.append(command)
        if len(spawns) == 2:
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout='43211\n')
    monkeypatch.setattr(swap.subprocess, 'run', run)
    monkeypatch.setattr(swap.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(swap, 'process_alive', lambda _pid: False)
    monkeypatch.setattr(swap, 'api', lambda _port, path, _project: (
        {'stop': {'paused': True}, 'build': {'commit': 'b' * 40}, 'project': 'demo'}
        if path == '/health' and not spawns else
        [] if path == '/tasks' else
        (_ for _ in ()).throw(OSError('unavailable'))))
    with pytest.raises(RuntimeError, match='died'):
        swap.main(['demo', 'a' * 40, 'go', '--decision-id', 'T0-TEST'])
    assert len(spawns) == 2 and spawns[1][-1] == str(old_src)
    assert 'ROLLBACK_FAILED' in capsys.readouterr().out
    assert (evidence / 'env.pre.nul').read_bytes() == raw
    assert not (evidence / 'env.pre.sha256').exists()
