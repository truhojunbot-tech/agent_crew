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


def test_u_i06_role_files_use_dotclaude_subdir():
    """Instructions must be written to .claude/ so they don't conflict with the
    project's root CLAUDE.md (which is tracked in git and reverts on checkout)."""
    for role, path in ROLE_FILES.items():
        assert path.startswith(".claude/"), (
            f"Role {role!r} uses {path!r} — must use .claude/ prefix to avoid git conflicts"
        )


def test_u_i07_common_instructs_ignore_alfred_global():
    """Agents must be told to ignore the global Alfred ~/.claude/CLAUDE.md
    so they don't invoke skills, use Telegram MCP, or create tmux panes."""
    content = generate("implementer", "myproject", 8123)
    lower = content.lower()
    assert "alfred" in lower
    assert "telegram" in lower  # explicitly prohibited
    assert "skill" in lower     # skill invocation prohibited
