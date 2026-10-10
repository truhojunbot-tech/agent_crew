"""#703: committed decision documents become bounded ADR-001 pointers."""

import json
import sqlite3
import subprocess

from agent_crew.memory_capture import backfill_document_pointers, capture_document_pointer
from agent_crew.memory_runtime import SQLiteMemoryStorage
from scripts.backfill_task_outcomes import count_available_refs


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def test_pointer_key_and_body_cap(tmp_path):
    memory = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    record = capture_document_pointer(
        memory, repo="truhojun/alpha_engine", project="alpha_engine",
        path="docs/decisions/005-llm-tool-allowlist.md", commit="a" * 40,
        content="# Tool allowlist\n\n" + "한국어 decision sentence. " * 80,
    )
    assert record.key == "doc:truhojun/alpha_engine:docs/decisions/005-llm-tool-allowlist.md@" + "a" * 40
    assert record.value["kind"] == "doc_pointer"
    assert record.value["path"] in record.value["body"]
    assert len(record.value["body"].encode()) <= 400
    assert "한국어" in record.value["body"]
    assert len(memory.retrieve(record.scope, exact_key=record.key)) == 1


def test_backfill_is_idempotent_and_refreshes_on_new_commit(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    decision = repo / "docs" / "decisions" / "ruling.md"
    decision.parent.mkdir(parents=True)
    decision.write_text("# First ruling\n\nUse the first rule.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first")
    first_sha = _git(repo, "rev-parse", "HEAD")
    memory = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    sources = [("truhojunbot-tech/agent_crew", str(repo))]
    assert backfill_document_pointers(memory, sources) == 1
    with sqlite3.connect(memory.path) as db:
        first = db.execute("SELECT key,value,version FROM adr001_memory WHERE key GLOB 'doc:*'").fetchall()
    assert first[0][0].endswith("@" + first_sha)
    assert backfill_document_pointers(memory, sources) == 0
    with sqlite3.connect(memory.path) as db:
        assert db.execute("SELECT key,value,version FROM adr001_memory WHERE key GLOB 'doc:*'").fetchall() == first
    decision.write_text("# Revised ruling\n\nUse the revised rule.\n")
    _git(repo, "commit", "-qam", "revise")
    assert backfill_document_pointers(memory, sources) == 1
    with sqlite3.connect(memory.path) as db:
        rows = db.execute("SELECT key,value FROM adr001_memory WHERE key GLOB 'doc:*' ORDER BY key").fetchall()
    assert len(rows) == 2
    assert any(json.loads(value)["title"] == "Revised ruling" for _, value in rows)
    historic = {"truhojunbot-tech/agent_crew": [("docs/decisions/ruling.md", first_sha)]}
    assert backfill_document_pointers(memory, sources, historic_files=historic) == 0


def test_audit_counts_document_path_with_project_identity(tmp_path):
    memory = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    capture_document_pointer(memory, repo="truhojun/alpha_engine", project="alpha_engine",
                             path="docs/specs/04-l2.md", commit="b" * 40,
                             content="# OpportunitySignal\n\nAdvisory behavior.\n")
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(json.dumps({"task_cases": [
        {"case_id": "hit", "project": "alpha_engine", "expected": [
            {"source_kind": "adr_spec", "ref": "docs/specs/04-l2.md §2.1(a)"}]},
        {"case_id": "wrong-project", "project": "other", "expected": [
            {"source_kind": "adr_spec", "ref": "docs/specs/04-l2.md §2.1(a)"}]},
        {"case_id": "wrong-commit", "project": "alpha_engine", "expected": [
            {"source_kind": "adr_spec", "ref": "docs/specs/04-l2.md (commit aaaaaaa)"}]},
    ]}))
    hits, total, missing = count_available_refs(memory.path, str(eval_set))
    assert (hits, total) == (1, 3)
    assert missing[0][0] == "wrong-project"
    assert missing[1][0] == "wrong-commit"


def test_explicit_working_verdict_is_honestly_versioned_and_refreshes(tmp_path):
    repo = tmp_path / "halla"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("source")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "initial")
    verdict = repo / "research" / "verdicts" / "earnings.json"
    verdict.parent.mkdir(parents=True)
    verdict.write_text('{"pass_if":"profit improves"}')
    memory = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    sources = [("truhojunbot-tech/halla", str(repo))]
    explicit = {"truhojunbot-tech/halla": {"research/verdicts/earnings.json"}}
    assert backfill_document_pointers(memory, sources, explicit) == 1
    assert backfill_document_pointers(memory, sources, explicit) == 0
    verdict.write_text('{"pass_if":"drawdown improves"}')
    assert backfill_document_pointers(memory, sources, explicit) == 1
    with sqlite3.connect(memory.path) as db:
        rows = db.execute("SELECT key,value,version FROM adr001_memory WHERE key GLOB 'doc:*'").fetchall()
    assert len(rows) == 1
    assert rows[0][0].endswith("@" + _git(repo, "rev-parse", "HEAD"))
    assert json.loads(rows[0][1])["source_state"] == "working_file"
    assert rows[0][2] == 2
