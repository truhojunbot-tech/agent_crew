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


def test_pr_creation_names_the_pushed_head_for_detached_dispatch():
    """Review of PR #1017: a detached HEAD pushed via ``git push origin
    HEAD:<branch>`` leaves gh no current branch to infer, so the generated
    command must pass ``--head`` with the branch that was pushed."""
    content = instructions.generate(
        "implementer", "proj", 8100, delivery="dispatcher")
    assert "git push origin HEAD:<pushed-branch>" in content
    assert ("gh pr create --head <pushed-branch> --base <base_branch>"
            in content)
    # Every gh pr create command line must name its head explicitly.
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("gh pr create"):
            assert "--head " in stripped, stripped
    # The detached-HEAD safety rule is still there; no force-move advice added.
    assert "Never \"fix\" a detached HEAD with `git branch -f`" in content
    assert "git branch -f <" not in content
    assert "checkout -B <" not in content
