import socket
import pytest

from agent_crew.setup import find_free_port


def test_durable_dead_project_port_is_not_reallocated(tmp_path):
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "port").write_text("9130")
    assert find_free_port(9130, base=str(tmp_path), limit=9131) == 9131


def test_external_listener_is_skipped(tmp_path):
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
        assert find_free_port(port, base=str(tmp_path), limit=port + 1) == port + 1


def test_project_reuses_its_owned_port(tmp_path):
    (tmp_path / "mine").mkdir()
    (tmp_path / "mine" / "port").write_text("9140")
    assert find_free_port(8100, base=str(tmp_path), project="mine") == 9140


def test_exhaustion_is_explicit(tmp_path):
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "port").write_text("9150")
    with pytest.raises(RuntimeError, match="no free ports"):
        find_free_port(9150, base=str(tmp_path), limit=9150)
