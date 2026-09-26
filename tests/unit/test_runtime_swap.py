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
