from agent_crew.instructions import generate


def test_u_i01_generate_includes_polling_and_result_submission():
    content = generate("implementer", "myproject", 8123)

    assert "Start this background loop immediately when the session starts" in content
    assert "GET http://localhost:8123/tasks/next?role=<role>" in content
    assert "POST http://localhost:8123/tasks/{id}/result" in content
    assert "Result Note Template" in content
    assert "branch: <branch-name>" in content
    assert "commit: <commit-hash>" in content
    assert "notes: <context or follow-up details>" in content
    # API-aligned status values must be present
    assert "completed" in content
    assert "failed" in content
    assert "needs_human" in content
