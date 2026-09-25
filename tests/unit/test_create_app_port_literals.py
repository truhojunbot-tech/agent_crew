"""Keep test servers on valid crew ports when dispatch guards tighten."""

import ast
from pathlib import Path


def test_create_app_has_no_invalid_literal_crew_ports():
    tests_dir = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted(tests_dir.rglob("test*.py")):
        if path.name == "test_issue_362_never_write_port_zero.py":
            continue
        tree = ast.parse(path.read_text())
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id != "create_app":
                continue
            for keyword in call.keywords:
                value = keyword.value
                if (keyword.arg == "port" and isinstance(value, ast.Constant)
                        and type(value.value) is int
                        and not 1024 <= value.value <= 65535):
                    offenders.append(f"{path.relative_to(tests_dir)}:{value.lineno}: {value.value}")
    assert not offenders, "invalid create_app crew port literals:\n" + "\n".join(offenders)
