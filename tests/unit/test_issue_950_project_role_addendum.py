"""#950: repo-tracked role rules survive protocol regeneration."""

from types import SimpleNamespace
import subprocess
import inspect

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


def _git(worktree, *args):
    return subprocess.run(["git", "-C", str(worktree), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _base_and_head(tmp_path, role="reviewer", base_text="BASE RULE\n",
                   head_text=None):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    worktree = tmp_path / "wt"
    subprocess.run(["git", "init", "-b", "main", str(worktree)], check=True,
                   capture_output=True)
    _git(worktree, "config", "user.name", "Test")
    _git(worktree, "config", "user.email", "test@example.invalid")
    _git(worktree, "remote", "add", "origin", str(remote))
    path = worktree / ".agent_crew" / f"{role}.md"
    path.parent.mkdir()
    if base_text is None:
        (worktree / "README.md").write_text("base\n")
    else:
        path.write_text(base_text)
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "base rules")
    _git(worktree, "push", "-u", "origin", "main")
    _git(worktree, "checkout", "-b", "pr-head")
    if head_text is None and base_text is not None:
        _git(worktree, "rm", str(path.relative_to(worktree)))
    elif head_text is not None:
        path.write_text(head_text)
        _git(worktree, "add", ".")
    if head_text is not None or base_text is not None:
        _git(worktree, "commit", "-m", "PR head")
    return worktree


@pytest.mark.parametrize("head_text", [None, "PR HEAD RULE\n"])
@pytest.mark.parametrize("agent,filename", [
    ("codex", "AGENTS.md"),
    ("claude", ".claude/CLAUDE.md"),
    ("gemini", "GEMINI.md"),
])
def test_reviewer_addendum_reaches_every_provider_and_survives_rewrite(
        tmp_path, agent, filename, head_text):
    addendum = "**Verdict:** approve\n변경 파일 확인: src/a.py, src/b.py\n"
    worktree = _base_and_head(tmp_path, base_text=addendum, head_text=head_text)
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
        assert "PR HEAD RULE" not in body
        assert body.count("## Project role addendum") == 1
        if expected is not None:
            assert body == expected
        expected = body


@pytest.mark.parametrize("role", ["implementer", "tester"])
def test_other_role_addenda_are_loaded_from_their_own_file(tmp_path, role):
    worktree = _base_and_head(tmp_path, role=role, base_text=f"ONLY {role}\n")
    body = instructions.generate(role, "proj", 9123, agent="codex",
                                 delivery="dispatcher", worktree_path=str(worktree))
    assert f"ONLY {role}\n" in body


def test_explicit_dev_base_is_used_instead_of_main(tmp_path):
    worktree = _base_and_head(tmp_path, base_text="MAIN RULE\n")
    _git(worktree, "checkout", "main")
    _git(worktree, "checkout", "-b", "dev")
    (worktree / ".agent_crew" / "reviewer.md").write_text("DEV RULE\n")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "dev rules")
    _git(worktree, "push", "origin", "dev")
    _git(worktree, "checkout", "pr-head")
    body = instructions.generate("reviewer", "proj", 9123, agent="codex",
                                 delivery="dispatcher", worktree_path=str(worktree),
                                 base_branch="dev")
    assert "DEV RULE" in body
    assert "MAIN RULE" not in body


def test_fetch_failure_uses_last_fetched_base_ref(tmp_path, monkeypatch):
    worktree = _base_and_head(tmp_path, base_text="FETCHED RULE\n")
    original = instructions.subprocess.run
    def fail_fetch(argv, **kwargs):
        if argv[0:4] == ["git", "-C", str(worktree), "fetch"]:
            return SimpleNamespace(returncode=1, stderr="offline")
        return original(argv, **kwargs)
    monkeypatch.setattr(instructions.subprocess, "run", fail_fetch)
    body = instructions.generate("reviewer", "proj", 9123, agent="codex",
                                 delivery="dispatcher", worktree_path=str(worktree))
    assert "FETCHED RULE" in body


def test_head_only_addendum_is_ignored(tmp_path):
    worktree = _base_and_head(tmp_path, base_text=None,
                              head_text="UNTRUSTED HEAD RULE\n")
    baseline = instructions.generate("reviewer", "proj", 9123,
                                     agent="codex", delivery="dispatcher")
    body = instructions.generate("reviewer", "proj", 9123, agent="codex",
                                 delivery="dispatcher", worktree_path=str(worktree))
    assert body == baseline


def test_dispatch_regenerates_existing_protocol_for_task_base(tmp_path):
    from agent_crew import server
    worktree = _base_and_head(tmp_path, base_text="MAIN RULE\n")
    _git(worktree, "checkout", "main")
    _git(worktree, "checkout", "-b", "dev")
    (worktree / ".agent_crew" / "reviewer.md").write_text("DEV RULE\n")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "dev rules")
    _git(worktree, "push", "origin", "dev")
    _git(worktree, "checkout", "pr-head")
    port_file = _port_file(tmp_path)
    instructions.write("reviewer", str(worktree), "proj", port_file,
                       agent="codex", delivery="dispatcher")
    assert "MAIN RULE" in (worktree / "AGENTS.md").read_text()
    assert server._ensure_role_protocol(
        "reviewer", str(worktree), "proj", port_file, agent="codex",
        base_branch="dev")
    body = (worktree / "AGENTS.md").read_text()
    assert "DEV RULE" in body
    assert "MAIN RULE" not in body


def test_dispatch_protocol_io_is_off_event_loop():
    import agent_crew.server as server
    source = inspect.getsource(server.create_app)
    assert "await asyncio.to_thread(\n            _ensure_role_protocol" in source
    assert "await asyncio.to_thread(\n                            instructions.write" in source


def test_existing_protocol_survives_regeneration_error(tmp_path, monkeypatch):
    from agent_crew import server
    worktree = tmp_path / "wt"
    worktree.mkdir()
    protocol = worktree / "AGENTS.md"
    protocol.write_text("existing protocol\n")
    monkeypatch.setattr(instructions, "write", lambda *a, **k: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("git fetch", 30)))
    assert server._ensure_role_protocol("reviewer", str(worktree), "proj",
                                        _port_file(tmp_path), agent="codex")
    assert protocol.read_text() == "existing protocol\n"


def test_default_branch_probe_timeout_does_not_abort_generation(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(instructions, "remote_default_branch",
                        lambda *_a: (_ for _ in ()).throw(
                            subprocess.TimeoutExpired("git default branch", 30)))
    baseline = instructions.generate("reviewer", "proj", 9123,
                                     agent="codex", delivery="dispatcher")
    assert instructions.generate("reviewer", "proj", 9123, agent="codex",
                                 delivery="dispatcher", worktree_path=str(worktree)) == baseline


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
