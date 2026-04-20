from agent_crew.instructions import generate


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
