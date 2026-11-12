"""#1015: implementers must open the PR themselves and always report pr_number.

Step 3 used to say "open a PR if the task requests one", while the checklist
required ``pr_number``. Codex implementers followed step 3 literally, pushed a
branch with no PR, and a retry round was needed just to open it.
"""
import pytest

from agent_crew import instructions


@pytest.mark.parametrize("delivery", ["dispatcher", "mcp", "push"])
def test_implementer_section_requires_opening_a_pr(delivery):
    content = instructions.generate(
        "implementer", "proj", 8100, delivery=delivery)
    assert "gh pr create" in content
    assert "if the task requests one" not in content
    assert "`pr_number` set if you opened a PR; otherwise `null`" not in content
    assert "required for every completed implement result" in content


def test_review_and_test_sections_unchanged_by_1015():
    for role in ("reviewer", "tester"):
        section = instructions._ROLE_SECTIONS[role]
        assert "required for every completed implement result" not in section
