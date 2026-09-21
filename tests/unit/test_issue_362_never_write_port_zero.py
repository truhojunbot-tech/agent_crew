"""#362: port 0 is never a crew endpoint or protocol input."""

import json

import pytest

from agent_crew import instructions
from agent_crew.cli import _read_state, _write_state
from agent_crew.setup import find_free_port, write_port_file


def test_port_file_write_rejects_zero_without_persisting(tmp_path):
    project = "zero-port-project"
    port_file = tmp_path / project / "port"
    port_file.parent.mkdir()
    with pytest.raises(ValueError, match=project):
        write_port_file(str(port_file), 0)
    assert not port_file.exists()


def test_allocator_rejects_zero_start_instead_of_resolving_zero():
    with pytest.raises(ValueError, match="allocator"):
        find_free_port(start=0)


def test_state_write_and_existing_zero_state_are_rejected(tmp_path):
    project = "stale-zero-project"
    with pytest.raises(ValueError, match=project):
        _write_state(str(tmp_path), project, {"project": project, "port": 0})
    assert not (tmp_path / project / "state.json").exists()
    project_dir = tmp_path / project
    project_dir.mkdir()
    (project_dir / "state.json").write_text(json.dumps({"project": project, "port": 0}))
    assert _read_state(str(tmp_path), project) is None


def test_zero_port_file_cannot_generate_protocol_directly_or_via_server(tmp_path):
    from agent_crew.server import _ensure_role_protocol

    project = "stale-zero-protocol"
    worktree = tmp_path / project / "worker"
    worktree.mkdir(parents=True)
    port_file = tmp_path / project / "port"
    port_file.write_text("0\n")
    with pytest.raises(ValueError, match=project):
        instructions.write("reviewer", str(worktree), project, str(port_file), agent="codex")
    assert not (worktree / "AGENTS.md").exists()

    port_file.unlink()
    assert not _ensure_role_protocol("reviewer", str(worktree), project, str(port_file),
                                     agent="codex", port=0)
    assert not port_file.exists()


@pytest.mark.parametrize("bad_port", [None, False, 1, 1023, 65536, "8105"])
def test_port_file_write_rejects_non_allocator_ports(tmp_path, bad_port):
    project = "invalid-port-project"
    port_file = tmp_path / project / "port"
    port_file.parent.mkdir()
    with pytest.raises(ValueError, match=project):
        write_port_file(str(port_file), bad_port)
    assert not port_file.exists()
