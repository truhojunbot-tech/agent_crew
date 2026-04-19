import pytest

from agent_crew.instructions import ROLE_FILES, generate, write


# U-I01: Generate for implementer — common + implementer section present
def test_u_i01_generate_implementer():
    content = generate("implementer", project="myproject", port_file="/tmp/port")
    assert "implementer" in content.lower()
    assert len(content) > 0


# U-I02: Generate for reviewer — common + reviewer section present
def test_u_i02_generate_reviewer():
    content = generate("reviewer", project="myproject", port_file="/tmp/port")
    assert "reviewer" in content.lower()


# U-I03: Generate for tester — common + tester section present
def test_u_i03_generate_tester():
    content = generate("tester", project="myproject", port_file="/tmp/port")
    assert "tester" in content.lower()


# U-I04: Generate with custom role — returns string, no ValueError
def test_u_i04_generate_custom_role():
    content = generate("custom_agent", project="myproject", port_file="/tmp/port")
    assert isinstance(content, str)
    assert len(content) > 0


# U-I05: Port placeholder replaced — <port> → port_file path
def test_u_i05_port_placeholder_replaced():
    content = generate("implementer", project="myproject", port_file="/tmp/my.port")
    assert "/tmp/my.port" in content
    assert "<port>" not in content


# U-I06: Project name injected — <project> → project name
def test_u_i06_project_name_injected():
    content = generate("reviewer", project="agent_crew", port_file="/tmp/port")
    assert "agent_crew" in content
    assert "<project>" not in content


# write(): correct filename saved at returned path
def test_u_i07_write_creates_file(tmp_path):
    path = write("implementer", str(tmp_path), project="p", port_file="/tmp/port")
    assert path.endswith(ROLE_FILES["implementer"])
    assert (tmp_path / ROLE_FILES["implementer"]).read_text() == generate(
        "implementer", project="p", port_file="/tmp/port"
    )


# write(): unknown role raises ValueError
def test_u_i08_write_unknown_role_raises(tmp_path):
    with pytest.raises(ValueError):
        write("unknown_role", str(tmp_path), project="p", port_file="/tmp/port")
