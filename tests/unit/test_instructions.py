import json
import re

import pytest

from agent_crew.instructions import ROLE_FILES, generate
from agent_crew.queue import validate_http_root_risk


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_delegation_template_declares_valid_http_root_risk(role):
    content = generate(role, "myproject", 8123, delivery="dispatcher")
    contexts = re.findall(r'"context": (\{[^\n]*\})', content)
    assert contexts
    context = json.loads(contexts[-1].replace("<n>", "684"))
    validate_http_root_risk(context)
    assert "X-Agent-Crew-Project: myproject" in content


@pytest.mark.parametrize("delivery", ["dispatcher", "mcp", "push", "both"])
def test_reviewer_approve_requires_empty_findings_in_every_delivery(delivery):
    content = generate("reviewer", "myproject", 8123, delivery=delivery)
    assert "approve` requires `findings: []`" in content
    assert "Non-blocking notes, nits, and coverage gaps" in content
    assert "summary or the GitHub PR comment" in content
    assert "request_changes` findings list only" in content
    assert "changes required to approve." in content
    assert "may be empty on `approve`" not in content
    assert content.index("approve` requires `findings: []`") < content.index("### Bounded pytest runs")


@pytest.mark.parametrize("role", ["reviewer", "tester"])
@pytest.mark.parametrize("delivery", ["dispatcher", "mcp", "both"])
def test_review_and_test_pytest_runs_are_bounded(role, delivery):
    content = generate(role, "myproject", 8123, delivery=delivery)
    assert 'timeout -k 10 "$N" python -m pytest' in content
    assert "remaining dispatch budget" in content
    assert "600" in content
    assert "900-second" in content
    assert "1200-second" not in content
    assert "124" in content and "137" in content
    assert "files changed by the PR" in content
    assert "full suite was not completed" in content
    assert "never report" in content.lower()


@pytest.mark.parametrize("role", ["reviewer", "tester"])
@pytest.mark.parametrize("delivery", ["dispatcher", "mcp", "push", "both"])
def test_review_and_test_claude_p_runs_commands_in_foreground(role, delivery):
    content = generate(role, "myproject", 8123, delivery=delivery)
    assert "run every command in the foreground" in content
    assert "run_in_background" in content
    assert "Monitor" in content
    assert "POST the result before ending the turn" in content
    assert "the session exits then" in content


@pytest.mark.parametrize("role", ["reviewer", "tester"])
@pytest.mark.parametrize("delivery", ["dispatcher", "mcp", "push", "both"])
def test_review_and_test_bash_timeout_covers_pytest_bound(role, delivery):
    content = generate(role, "myproject", 8123, delivery=delivery)
    assert "585 seconds" in content
    assert "Bash tool's `timeout` parameter" in content
    assert "(N+15)*1000" in content
    assert "600000" in content
    assert "await it in the same turn" in content
    assert "`status: failed`" in content


def test_implementer_protocol_has_no_pytest_rule():
    content = generate("implementer", "myproject", 8123, delivery="dispatcher")
    assert "Bounded pytest runs" not in content


def test_u_i01_generate_describes_push_model_and_result_submission():
    content = generate("implementer", "myproject", 8123)

    # Push model — no polling mention
    assert "push" in content.lower()
    assert "=== AGENT_CREW TASK ===" in content
    # Result submission endpoint
    assert "/tasks/<task_id>/result" in content
    # Port substitution
    assert "8123" in content
    # API-aligned status values
    assert "completed" in content
    assert "failed" in content
    assert "needs_human" in content
    # Role-specific section present
    assert "implementer" in content.lower()


def test_u_i02_generate_reviewer_includes_verdict():
    content = generate("reviewer", "myproject", 8123)
    assert "verdict" in content.lower()
    assert "approve" in content.lower()
    assert "request_changes" in content


def test_u_i03_implementer_result_submission_strengthened():
    """Result submission should be explicitly mandatory with a concrete template
    covering status/branch/commit/notes fields."""
    content = generate("implementer", "myproject", 8123)

    # Mandatory language
    lowered = content.lower()
    assert "mandatory" in lowered
    assert "never skip" in lowered
    # Concrete field guidance in summary
    assert "branch:" in content
    assert "commit:" in content
    # Worked example for implementer
    assert "t-042" in content
    assert "agent/fix-login-timeout" in content
    # Checklist for self-verification
    assert "[ ]" in content
    # Canonical curl template (includes content-type)
    assert "Content-Type: application/json" in content


def test_u_i04_reviewer_checklist_requires_verdict():
    content = generate("reviewer", "myproject", 8123)
    assert "[ ]" in content
    # Reviewer must not leave verdict null
    assert "never `null`" in content or "not null" in content.lower()


def test_u_i05_failure_path_documented():
    """Agents must know how to report failure — silence is worse than failure."""
    content = generate("implementer", "myproject", 8123)
    assert "needs_human" in content
    assert "failed" in content
    # Failure/escalation worked example present
    assert "needs direction" in content.lower() or "needs_human" in content


def test_u_i06_role_files_match_each_agent_cli_lookup_path():
    """Each role's instruction file must live where that agent's CLI
    actually reads from (Issue #110):

    - implementer (claude) → `.claude/CLAUDE.md` (Claude Code merges
      with the project root CLAUDE.md without conflict)
    - reviewer (codex)     → `AGENTS.md` (Codex reads only the
      project-root copy)
    - tester (gemini)      → `GEMINI.md` (Gemini reads only the
      project-root copy)

    The previous all-`.claude/` layout meant codex/gemini never saw
    the agent_crew prompts and led to the tester force-pushing over
    the implementer's PR head.
    """
    assert ROLE_FILES["implementer"] == "AGENTS.md"
    assert ROLE_FILES["reviewer"] == ".claude/CLAUDE.md"
    assert ROLE_FILES["tester"] == "GEMINI.md"


def test_u_i07_common_instructs_ignore_alfred_global():
    """Agents must be told to ignore the global Alfred ~/.claude/CLAUDE.md
    so they don't invoke skills, use Telegram MCP, or create tmux panes."""
    content = generate("implementer", "myproject", 8123)
    lower = content.lower()
    assert "alfred" in lower
    assert "telegram" in lower  # explicitly prohibited
    assert "skill" in lower     # skill invocation prohibited


def test_u_i08_common_includes_polling_routine():
    """_COMMON (push/both mode) must include 30-second polling routine.

    Agents should poll GET /tasks/next (or MCP get_next_task) every 30
    seconds at session start and after each task completes. This makes
    push-mode agents resilient to missed pushes.
    """
    content = generate("implementer", "myproject", 8100)
    lower = content.lower()
    # Polling routine present
    assert "30 second" in lower or "30s" in lower
    # No longer says "do not poll" — polling is now allowed/encouraged
    assert "do not poll" not in lower


def test_u_i09_mcp_common_includes_result_format_guidance():
    """_MCP_COMMON must include summary-field format (branch/commit/notes)
    and a concrete curl-style example so agents know exactly what to include.
    """
    from agent_crew.instructions import _MCP_COMMON
    assert "branch=" in _MCP_COMMON
    assert "commit=" in _MCP_COMMON
    assert "notes:" in _MCP_COMMON


def test_u_i10_common_post_result_strengthened():
    """POST result instruction must be unmistakably mandatory in both modes."""
    from agent_crew.instructions import _COMMON, _MCP_COMMON
    # Both modes must carry "never skip" or equivalent mandatory language
    assert "never skip" in _COMMON.lower() or "mandatory" in _COMMON.lower()
    assert "never skip" in _MCP_COMMON.lower() or "mandatory" in _MCP_COMMON.lower()


def test_u_i11_tester_warns_against_shared_worktree_branch_ref_corruption():
    """#281: the tester's worktree shares refs/heads/* with the implementer's
    (same repo, git worktree add) — a plain `git checkout <pr-branch>` can
    fail because that exact branch may already be checked out elsewhere, and
    an agent "fixing" that failure with `git branch -f`/`update-ref` can
    corrupt the branch the implementer is actively working on. The tester
    role must instruct a detached checkout instead, which never touches
    refs/heads/* and is therefore immune regardless of sibling worktrees."""
    content = generate("tester", "myproject", 8123)
    lowered = content.lower()

    # Names the concrete hazard, not just a vague warning
    assert "shared" in lowered
    assert "refs/heads" in content
    assert "worktree" in lowered

    # Explicitly forbids the unsafe workaround
    assert "git branch -f" in content or "update-ref" in content

    # Prescribes the safe, concrete alternative
    assert "git fetch origin" in content
    assert "checkout --detach" in content
    assert "FETCH_HEAD" in content
