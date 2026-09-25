import socket
import pytest

from agent_crew.setup import find_free_port, list_duplicate_port_claims, write_port_file


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


@pytest.mark.parametrize("recorded", ["0", "80", "70000"])
def test_invalid_recorded_own_port_is_not_returned(tmp_path, recorded):
    (tmp_path / "mine").mkdir()
    (tmp_path / "mine" / "port").write_text(recorded)
    assert find_free_port(9160, base=str(tmp_path), project="mine", limit=9162) == 9160


def test_in_use_recorded_own_port_is_not_returned(tmp_path):
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        busy = held.getsockname()[1]
        (tmp_path / "mine").mkdir()
        (tmp_path / "mine" / "port").write_text(str(busy))
        assert find_free_port(busy + 1, base=str(tmp_path), project="mine", limit=busy + 2) == busy + 1


def test_prewrite_allocations_claim_distinct_ports(tmp_path):
    first = find_free_port(9170, base=str(tmp_path), project="a", limit=9172)
    second = find_free_port(9170, base=str(tmp_path), project="b", limit=9172)
    assert (first, second) == (9170, 9171)


def test_dead_claim_is_reclaimed(tmp_path):
    claims = tmp_path / ".ports"
    claims.mkdir()
    (claims / "9180").write_text("99999999")
    assert find_free_port(9180, base=str(tmp_path), project="recovered", limit=9181) == 9180


def test_exhaustion_is_explicit(tmp_path):
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "port").write_text("9150")
    with pytest.raises(RuntimeError, match="no free ports"):
        find_free_port(9150, base=str(tmp_path), limit=9150)


def test_explicit_write_refuses_another_project_claim(tmp_path):
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "port").write_text("9200")
    (tmp_path / "mine").mkdir()
    with pytest.raises(ValueError, match="9200.*other"):
        write_port_file(str(tmp_path / "mine" / "port"), 9200, project="mine")
    assert not (tmp_path / "mine" / "port").exists()


def test_own_port_with_duplicate_claim_is_refused(tmp_path):
    for project in ("mine", "other"):
        (tmp_path / project).mkdir()
        (tmp_path / project / "port").write_text("9210")
    with pytest.raises(ValueError, match="9210.*other"):
        find_free_port(9220, base=str(tmp_path), project="mine")


def test_duplicate_listing_is_read_only(tmp_path):
    for project, port in (("zeta", 9230), ("alpha", 9230), ("solo", 9231)):
        (tmp_path / project).mkdir()
        (tmp_path / project / "port").write_text(str(port))
    assert list_duplicate_port_claims(str(tmp_path)) == {9230: ["alpha", "zeta"]}
    assert (tmp_path / "alpha" / "port").read_text() == "9230"


def test_existing_single_owner_file_stays_unchanged(tmp_path):
    (tmp_path / "mine").mkdir()
    port_file = tmp_path / "mine" / "port"
    port_file.write_text("9240\n")
    assert find_free_port(9250, base=str(tmp_path), project="mine") == 9240
    write_port_file(str(port_file), 9240, project="mine")
    assert port_file.read_text() == "9240\n"
