"""A live memory flag must reach a dispatch path (#661)."""

import ast
import re
from datetime import date
from pathlib import Path

import pytest


SRC = Path(__file__).parents[2] / "src" / "agent_crew"
FLAG = re.compile(r"AGENT_CREW_[A-Z0-9_]+_ENABLED$")
ALLOWLIST = {
    # Result capture runs after dispatch, not in the dispatch call graph.
    "AGENT_CREW_SHADOW_MEMORY_CAPTURE_ENABLED": {
        "owner": "agent_crew", "issue": 430, "expires": "2027-10-09"},
}


def _valid_allowlist(allowlist, today):
    for flag, entry in allowlist.items():
        assert FLAG.fullmatch(flag)
        assert entry["owner"] and entry["issue"]
        assert date.fromisoformat(entry["expires"]) >= today, flag


def test_expired_enabled_flag_exception_fails():
    with pytest.raises(AssertionError, match="AGENT_CREW_FAKE_ENABLED"):
        _valid_allowlist({"AGENT_CREW_FAKE_ENABLED": {
            "owner": "agent_crew", "issue": 661, "expires": "2000-01-01"}}, date.today())


def test_enabled_gates_have_dispatch_call_sites():
    _valid_allowlist(ALLOWLIST, date.today())
    calls = {}
    flags = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        constants = {
            target.id: node.value.value
            for node in tree.body if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            for target in node.targets if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = set()
            gated = set()
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                name = child.func.id if isinstance(child.func, ast.Name) else (
                    child.func.attr if isinstance(child.func, ast.Attribute) else "")
                if name:
                    called.add(name)
                if name == "getenv" and child.args:
                    arg = child.args[0]
                    value = arg.value if isinstance(arg, ast.Constant) else (
                        constants.get(arg.id, "") if isinstance(arg, ast.Name) else "")
                    if isinstance(value, str) and FLAG.fullmatch(value):
                        gated.add(value)
            calls.setdefault(node.name, set()).update(called)
            if gated:
                flags.setdefault(node.name, set()).update(gated)
    reachable = set()
    pending = ["create_app", "_dispatch_task", "_try_push_next"]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(calls.get(name, set()) - reachable)
    missing = {flag for name, values in flags.items() if name not in reachable
               for flag in values if flag not in ALLOWLIST}
    assert not missing, f"enabled flags have no dispatch call site: {sorted(missing)}"
