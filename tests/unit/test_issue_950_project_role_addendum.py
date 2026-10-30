"""#950: repo-tracked role rules survive protocol regeneration."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_crew import github, instructions, setup as crew_setup
from agent_crew.github import post_review_comment as format_review_comment
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _port_file(tmp_path):
    port_file = tmp_path / "port"
    port_file.write_text("9123")
    return str(port_file)


@pytest.mark.parametrize("agent,filename", [
    ("codex", "AGENTS.md"),
    ("claude", ".claude/CLAUDE.md"),
    ("gemini", "GEMINI.md"),
])
def test_reviewer_addendum_reaches_every_provider_and_survives_rewrite(
        tmp_path, agent, filename):
    worktree = tmp_path / agent
    (worktree / ".agent_crew").mkdir(parents=True)
    addendum = "**Verdict:** approve\n변경 파일 확인: src/a.py, src/b.py\n"
    (worktree / ".agent_crew" / "reviewer.md").write_text(addendum)
    port_file = _port_file(tmp_path)
    expected = None
    for _ in range(2):
        crew_setup.write_instruction_files(
            {agent: str(worktree)}, "proj", port_file,
            roles=[{"role": "reviewer", "agent": agent,
                    "worktree": str(worktree)}])
        assert (worktree / filename).exists()
        body = (worktree / filename).read_text()
        assert "## Project role addendum" in body
        assert addendum in body
        assert body.count("## Project role addendum") == 1
        if expected is not None:
            assert body == expected
        expected = body


@pytest.mark.parametrize("role", ["implementer", "tester"])
def test_other_role_addenda_are_loaded_from_their_own_file(tmp_path, role):
    worktree = tmp_path / role
    (worktree / ".agent_crew").mkdir(parents=True)
    (worktree / ".agent_crew" / f"{role}.md").write_text(f"ONLY {role}\n")
    body = instructions.generate(role, "proj", 9123, agent="codex",
                                 delivery="dispatcher", worktree_path=str(worktree))
    assert f"ONLY {role}\n" in body


@pytest.mark.parametrize("agent,filename", [
    ("codex", "AGENTS.md"),
    ("claude", ".claude/CLAUDE.md"),
    ("gemini", "GEMINI.md"),
])
def test_missing_addendum_leaves_generated_protocol_identical(tmp_path, agent, filename):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    baseline = instructions.generate("reviewer", "proj", 9123, agent=agent,
                                     delivery="dispatcher")
    assert instructions.generate("reviewer", "proj", 9123, agent=agent,
                                 delivery="dispatcher", worktree_path=str(worktree)) == baseline
    instructions.write("reviewer", str(worktree), "proj", _port_file(tmp_path),
                       agent=agent, delivery="dispatcher")
    expected = (baseline if agent == "claude" else
                instructions._merge_agent_crew_block("", baseline))
    assert (worktree / filename).read_text() == expected


@pytest.mark.parametrize("filename", ["AGENTS.md", "GEMINI.md"])
def test_regeneration_preserves_bytes_outside_crew_block(tmp_path, filename):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    before = b"Project preface\r\n\xc3\xa9\r\n"
    after = b"\r\nProject suffix\r\n  spaces stay  \r\n"
    file_path = worktree / filename
    file_path.write_bytes(
        before + b"<!-- agent_crew:begin -->\nold\n<!-- agent_crew:end -->" + after)
    agent = "codex" if filename == "AGENTS.md" else "gemini"
    for _ in range(2):
        instructions.write("reviewer", str(worktree), "proj", _port_file(tmp_path),
                           agent=agent, delivery="dispatcher")
        current = file_path.read_bytes()
        assert current.startswith(before)
        assert current.endswith(after)


def test_files_reviewed_result_field_and_comment_rendering(monkeypatch):
    result = TaskResult(task_id="review-950", status="completed",
                        summary="Reviewed the changed files and approved the patch.",
                        verdict="approve", files_reviewed=["src/a.py", "src/b.py"])
    bodies = []
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)
    monkeypatch.setattr(github.subprocess, "run", lambda argv, **_kwargs:
                        bodies.append(argv[argv.index("--body") + 1]) or
                        SimpleNamespace(returncode=0))
    assert format_review_comment(950, result.verdict, result.summary,
                                 result.findings, result.task_id,
                                 repo="owner/repo",
                                 files_reviewed=result.files_reviewed)
    assert bodies[0].count("변경 파일 확인: src/a.py, src/b.py") == 1
    assert bodies[0].index("**Summary:**") < bodies[0].index("변경 파일 확인:")

    assert format_review_comment(950, result.verdict, result.summary,
                                 result.findings, result.task_id,
                                 repo="owner/repo")
    assert bodies[1] == (
        "[agent_crew review] verdict: ✅ approve\n\n"
        "**Summary:** Reviewed the changed files and approved the patch.\n\n"
        "> reviewer: agent | task: review-950")

    assert format_review_comment(950, result.verdict, result.summary,
                                 result.findings, result.task_id,
                                 repo="owner/repo",
                                 files_reviewed=["src/a.py\nsrc/b.py"])
    assert "변경 파일 확인: src/a.py src/b.py\n\n" in bodies[2]


def test_server_forwards_files_reviewed_to_comment_writer(tmp_db, monkeypatch):
    import agent_crew.server as server

    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(
        task_id="review-950", task_type="review", description="Review",
        branch="fix/950", project="demo", pr_number=950,
        context={"pr_number": 950, "reviewed_sha": "a" * 40,
                 "repo": "owner/repo", "risk_tier": 1}))
    monkeypatch.setattr(server, "review_publication_decision",
                        lambda *_a, **_kw: SimpleNamespace(publish=True))
    monkeypatch.setattr(server, "_pipeline_auto_enqueue_test",
                        lambda *_a, **_kw: None)
    posted = []
    monkeypatch.setattr(github, "post_review_comment",
                        lambda **kwargs: posted.append(kwargs) or True)
    app = create_app(db_path=tmp_db, project="demo", watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/review-950/result", json={
            "task_id": "review-950", "status": "completed",
            "summary": "Reviewed the changed files and approved the patch.",
            "verdict": "approve", "findings": [],
            "files_reviewed": ["src/a.py", "src/b.py"],
            "pr_number": 950,
        }, headers={"X-Agent-Crew-Project": "demo"})
    assert response.status_code == 200, response.text
    assert [call["files_reviewed"] for call in posted] == [["src/a.py", "src/b.py"]]
