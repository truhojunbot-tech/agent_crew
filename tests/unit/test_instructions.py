from agent_crew.instructions import ROLE_FILES, generate


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
    assert ROLE_FILES["implementer"] == ".claude/CLAUDE.md"
    assert ROLE_FILES["reviewer"] == "AGENTS.md"
    assert ROLE_FILES["tester"] == "GEMINI.md"


def test_u_i07_common_instructs_ignore_alfred_global():
    """Agents must be told to ignore the global Alfred ~/.claude/CLAUDE.md
    so they don't invoke skills, use Telegram MCP, or create tmux panes."""
    content = generate("implementer", "myproject", 8123)
    lower = content.lower()
    assert "alfred" in lower
    assert "telegram" in lower  # explicitly prohibited
    assert "skill" in lower     # skill invocation prohibited
