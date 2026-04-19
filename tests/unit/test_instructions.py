import pytest

from agent_crew.instructions import ROLE_FILES, generate, write


# U-I01: Generate for implementer — contains implementer role description and HTTP endpoint
def test_u_i01_generate_implementer():
    content = generate("implementer", project="myproject", port=8080)
    assert "implementer" in content.lower()
    assert "## Role: implementer" in content
    assert "src/" in content


# U-I02: Generate for reviewer — contains reviewer role description and HTTP endpoint
def test_u_i02_generate_reviewer():
    content = generate("reviewer", project="myproject", port=8080)
    assert "reviewer" in content.lower()
    assert "## Role: reviewer" in content
    assert "GitHub PR" in content or "PR" in content


# U-I03: Generate for tester — contains tester role description and HTTP endpoint
def test_u_i03_generate_tester():
    content = generate("tester", project="myproject", port=8080)
    assert "tester" in content.lower()
    assert "## Role: tester" in content
    assert "tests/" in content


# U-I04: Generate with custom role — returns string with role name, no ValueError
def test_u_i04_generate_custom_role():
    content = generate("custom_agent", project="myproject", port=8080)
    assert isinstance(content, str)
    assert "custom_agent" in content


# U-I05: Port placeholder replaced — <port> → actual port number in HTTP URLs
def test_u_i05_port_placeholder_replaced():
    content = generate("implementer", project="myproject", port=9000)
    assert "localhost:9000" in content
    assert "<port>" not in content


# U-I06: Project name injected — <project> → project name
def test_u_i06_project_name_injected():
    content = generate("reviewer", project="agent_crew", port=8080)
    assert "agent_crew" in content
    assert "<project>" not in content


# write(): correct filename saved, returned path matches
def test_u_i07_write_creates_file(tmp_path):
    port_file = tmp_path / "port"
    port_file.write_text("8080")
    path = write("implementer", str(tmp_path), project="p", port_file=str(port_file))
    assert path.endswith(ROLE_FILES["implementer"])
    assert (tmp_path / ROLE_FILES["implementer"]).read_text() == generate(
        "implementer", project="p", port=8080
    )


# write(): unknown role raises ValueError
def test_u_i08_write_unknown_role_raises(tmp_path):
    port_file = tmp_path / "port"
    port_file.write_text("8080")
    with pytest.raises(ValueError):
        write("unknown_role", str(tmp_path), project="p", port_file=str(port_file))
