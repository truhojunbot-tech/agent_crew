"""#930 slice B: the gemini/agy alias and the pane-provider set live in role_mapping."""
import ast
import pathlib

import pytest

from agent_crew.role_mapping import PANE_PROVIDERS, is_gemini_family

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "agent_crew"
FORBIDDEN = {("gemini", "agy"), ("claude", "codex", "gemini")}


@pytest.mark.parametrize("agent", ["gemini", "agy", "agy-2"])
def test_gemini_family_members(agent):
    assert is_gemini_family(agent) is True


@pytest.mark.parametrize("agent", ["claude", "codex", "claude_cloud"])
def test_non_gemini_agents(agent):
    assert is_gemini_family(agent) is False


def test_pane_providers():
    assert PANE_PROVIDERS == ("claude", "codex", "gemini")


def _string_tuples(tree):
    for node in ast.walk(tree):
        # Tuples only: failure_kind.py's ``{"gemini", "agy"}`` sets are outside
        # slice B's site list and stay for a later slice.
        if isinstance(node, ast.Tuple) and node.elts and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
            yield node.lineno, tuple(e.value for e in node.elts)


def test_no_provider_tuple_literals_outside_role_mapping():
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == "role_mapping.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        offenders += [f"{path.name}:{line} {values}"
                      for line, values in _string_tuples(tree) if values in FORBIDDEN]
    assert offenders == [], "resolve providers via role_mapping (#930): " + ", ".join(offenders)
