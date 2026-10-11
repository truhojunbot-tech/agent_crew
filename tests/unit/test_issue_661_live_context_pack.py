"""#661: fleet failure patterns reach the live, bounded Context Pack."""

from agent_crew import context_pack as pack
from agent_crew.memory_runtime import (MemoryRecord, MemoryScope, SQLiteMemoryStorage,
                                       dispatch_memory_block)
import hashlib
import json
import sqlite3
import time

import pytest


def test_strict_retrieve_treats_fleet_only_records_as_project_ancestors(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    storage.put(MemoryRecord("failure_pattern", "wc1:fleet", {"text": "fleet warning"},
                             MemoryScope(fleet="fleet")))
    storage.put(MemoryRecord("failure_pattern", "local", {"text": "local warning"},
                             MemoryScope(project="agent_crew")))
    storage.put(MemoryRecord("failure_pattern", "local-2", {"text": "another local warning"},
                             MemoryScope(project="agent_crew")))
    storage.put(MemoryRecord("failure_pattern", "sibling", {"text": "other warning"},
                             MemoryScope(fleet="fleet", project="other")))

    assert {r.key for r in storage.retrieve(MemoryScope(project="agent_crew"))} == {
        "wc1:fleet", "local", "local-2"}
    assert {r.key for r in storage.retrieve(MemoryScope(fleet="named", project="agent_crew"))} == {
        "wc1:fleet"}


def test_live_pack_reserves_failure_pattern_slots_and_reports_served_keys(tmp_path, monkeypatch):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    storage.put(MemoryRecord("failure_pattern", "wc1:fleet", {"text": "fleet warning"},
                             MemoryScope(fleet="fleet")))
    storage.put(MemoryRecord("failure_pattern", "local", {"text": "local warning"},
                             MemoryScope(project="agent_crew")))
    storage.put(MemoryRecord("failure_pattern", "local-2", {"text": "another local warning"},
                             MemoryScope(project="agent_crew")))
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", storage.path)

    class CodeProvider(pack.RetrievalProvider):
        def retrieve(self, query):
            return [pack.Artifact(f"code:{n}", f"src/{n}.py", pack.TYPE_CODE,
                                  excerpt="code") for n in range(10)]

    built = pack.build_pack_for_task(
        {"issue": 661, "repo": "o/agent_crew", "issue_title": "Fleet memory",
         "issue_body": "## Acceptance Criteria\n- Include fleet memory"},
        task_id="impl-661", task_type="implement", role="implementer",
        project="agent_crew", extra_providers=[CodeProvider()],
        budget={"max_items": 4, "max_tokens": 1000, "type_caps": {}},
    )
    assert {item.artifact_id for item in built.items if item.artifact_type == "failure_pattern"} == {
        "memory:local", "memory:wc1:fleet"}
    assert {"local", "wc1:fleet"} <= set(built.telemetry()["result_ids"])
    assert pack.is_sufficient(built).ok


def test_assembly_inherits_issue_and_repo_and_parses_done_heading():
    context = pack.assemble_task_context(
        {"prev_task_id": "impl-1"}, project="agent_crew",
        repo="truhojunbot-tech/agent_crew",
        lineage_contexts=[{"issue": 661}],
    )
    assert context["issue"] == 661
    assert context["repo"] == "truhojunbot-tech/agent_crew"
    assert pack.assemble_task_context(
        {"issue": 661, "repo": "wrong/project"},
        repo="truhojunbot-tech/agent_crew")["repo"] == "truhojunbot-tech/agent_crew"
    assert pack.IssueProvider.extract_ac("## Done (numeric)\n- Live rows served\n") == "- Live rows served"
    assert pack.assemble_task_context({"issue": "661"})["issue"] == 661


def test_complete_issue_without_ac_is_marked_and_sufficient(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_SHADOW_MEMORY_DB", raising=False)
    built = pack.build_pack_for_task(
        {"issue": "661"}, task_id="impl-661", task_type="implement", role="implementer",
        repo="truhojunbot-tech/agent_crew", project="agent_crew", repo_path=str(tmp_path),
        issue_body_fn=lambda repo, issue: "## Background\nNo formal acceptance section.",
    )
    assert pack.is_sufficient(built).ok
    assert "no_ac=true" in next(a.provenance for a in built.items
                                 if a.artifact_type == pack.TYPE_ISSUE)


def test_dispatch_event_names_the_served_failure_pattern(tmp_path, monkeypatch, unused_tcp_port):
    from tests.unit.test_context_pack_inject_gate import _dispatch

    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    storage.put(MemoryRecord("failure_pattern", "wc1:dispatch", {"text": "fleet warning"},
                             MemoryScope(fleet="fleet")))
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", storage.path)
    result = _dispatch(tmp_path / "dispatch", monkeypatch, unused_tcp_port, live=True)
    assert "wc1:dispatch" in result["built"][0]["result_ids"]
    assert "fleet warning" in result["message"]


def test_adr001_live_dispatch_serves_owner_and_fleet_with_context_pack_off(
        tmp_path, monkeypatch, unused_tcp_port):
    from tests.unit.test_context_pack_inject_gate import _dispatch

    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    owner_text = "Owner decision for this project"
    owner_key = "owner:agent_crew:telegram:6419236710:2154"
    storage.put(MemoryRecord("authoritative", owner_key, {
        "kind": "owner_statement", "text": owner_text,
        "text_sha256": hashlib.sha256(owner_text.encode()).hexdigest(),
        "chat_id": "6419236710", "message_id": "2154",
        "verification_status": "VERIFIED", "supersedes": [],
    }, MemoryScope(project="agent_crew")))
    storage.put(MemoryRecord("failure_pattern", "wc1:live-replay", {
        "kind": "worst_case", "title": "Fleet failure", "pattern": "Avoid stale owner state",
    }, MemoryScope(fleet="fleet")))
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", storage.path)
    monkeypatch.setenv("AGENT_CREW_MEMORY_PROJECT_ALIASES", json.dumps({"agent_crew": "agent_crew"}))
    result = _dispatch(tmp_path / "dispatch", monkeypatch, unused_tcp_port,
                       task_project="agent_crew")
    assert owner_key in result["message"]
    assert "wc1:live-replay" in result["message"]
    assert result["row_context"]["memory_served"]["authoritative"] > 0
    assert result["row_context"]["memory_served"]["failure_pattern"] > 0
    assert result["memory"][0]["memory_served"] == result["row_context"]["memory_served"]


def test_adr001_project_aliases_are_operator_configured(monkeypatch):
    from agent_crew.server import _adr001_project

    monkeypatch.setenv("AGENT_CREW_MEMORY_PROJECT_ALIASES",
                       json.dumps({"quota-*": "Quota", "agent_crew": "Crew"}))
    assert _adr001_project("quota-ops") == "Quota"
    assert _adr001_project("agent_crew") == "Crew"
    assert _adr001_project("alpha_engine") == "alpha_engine"


def test_dispatch_memory_uses_only_effective_project_owner_statements(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))

    def owner(project, message, text, supersedes=()):
        key = f"owner:{project}:telegram:6419236710:{message}"
        storage.put(MemoryRecord("authoritative", key, {
            "kind": "owner_statement", "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "chat_id": "6419236710", "message_id": message,
            "verification_status": "VERIFIED", "supersedes": list(supersedes),
        }, MemoryScope(project=project)))
        return key

    old = owner("agent_crew", "1", "superseded decision")
    current = owner("agent_crew", "2", "effective decision", (old,))
    foreign = owner("other", "3", "other project decision")
    block, counts = dispatch_memory_block(
        storage, "implementer", "impl-661", MemoryScope(project="agent_crew"))
    assert current in block
    assert old not in block
    assert foreign not in block
    assert counts == {"authoritative": 1, "failure_pattern": 0}


def test_migrated_column_retirements_stay_out_of_live_readers(tmp_path, monkeypatch):
    from agent_crew.memory_hybrid import HybridMemoryStorage

    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="agent_crew")
    for layer, key in (
        ("failure_pattern", "retired-failure"),
        ("failure_pattern", "replaced-failure"),
        ("failure_pattern", "current-failure"),
        ("authoritative", "retired-truth"),
        ("authoritative", "current-truth"),
    ):
        storage.put(MemoryRecord(layer, key, {"text": key}, scope))
    HybridMemoryStorage(storage.path)
    with sqlite3.connect(storage.path) as db:
        db.execute("UPDATE adr001_memory SET invalidated_at=? WHERE key=?",
                   (time.time(), "retired-failure"))
        db.execute("UPDATE adr001_memory SET superseded_by=? WHERE key=?",
                   ("current-truth", "retired-truth"))
        db.execute("UPDATE adr001_memory SET superseded_by=? WHERE key=?",
                   ("current-failure", "replaced-failure"))

    block, counts = dispatch_memory_block(
        storage, "implementer", "impl-661", scope)
    artifacts = pack.FailurePatternProvider(storage.path).retrieve(
        pack.RetrievalQuery(project="agent_crew"))
    assert "retired-failure" not in block
    assert "replaced-failure" not in block
    assert "retired-truth" not in block
    assert "current-failure" in block
    assert "current-truth" in block
    assert counts == {"authoritative": 1, "failure_pattern": 1}
    assert {item.artifact_id for item in artifacts} == {"memory:current-failure"}


def test_dispatch_memory_budget_counts_only_rendered_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    for number in range(30):
        storage.put(MemoryRecord("authoritative", f"fact:{number:02d}",
                                 {"text": "long excerpt " * 40},
                                 MemoryScope(project="agent_crew")))
    storage.put(MemoryRecord("failure_pattern", "wc1:budget", {"text": "warning"},
                             MemoryScope(fleet="fleet")))
    block, counts = dispatch_memory_block(
        storage, "implementer", "impl-661", MemoryScope(project="agent_crew"),
        max_chars=1600)
    assert len(block) <= 1600
    assert counts["authoritative"] == block.count("authoritative key=")
    assert counts["failure_pattern"] == block.count("failure_pattern key=")
    assert counts["authoritative"] == 30
    assert "long excerpt" not in block


def test_dispatch_memory_caps_utf8_bytes_after_all_authoritative_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    for number in range(300):
        storage.put(MemoryRecord("authoritative", f"fact:{number:03d}",
                                 {"text": "한글 " * 100},
                                 MemoryScope(project="agent_crew")))
    storage.put(MemoryRecord("failure_pattern", "wc1:budget",
                             {"text": "warning"}, MemoryScope(fleet="fleet")))
    block, counts = dispatch_memory_block(
        storage, "implementer", "impl-738", MemoryScope(project="agent_crew"))
    assert len(block.encode("utf-8")) <= 8900
    assert counts == {"authoritative": 300, "failure_pattern": 1}
    assert all(f"authoritative key=fact:{number:03d}" in block for number in range(300))
    assert "failure_pattern key=wc1:budget" in block
    if "authoritative excerpt=" in block:
        assert block.index("authoritative key=fact:299") < block.index("authoritative excerpt=")


def test_dispatch_memory_uses_scope_only_rows_and_alias_preserves_local_patterns(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    for layer, key, scope in (
        ("authoritative", "owner:Quota:telegram:6419236710:2154", MemoryScope(project="Quota")),
        ("failure_pattern", "wc1:fleet", MemoryScope(fleet="fleet")),
        ("failure_pattern", "local:quota-ops", MemoryScope(project="quota-ops")),
        ("failure_pattern", "foreign:Quota", MemoryScope(project="Quota")),
        ("episodic", "episode:ranked", MemoryScope(project="Quota")),
        ("decision", "decision:ranked", MemoryScope(project="Quota")),
    ):
        value = {"text": key}
        if layer == "authoritative":
            value.update(kind="owner_statement", chat_id="6419236710", message_id="2154",
                         verification_status="VERIFIED", supersedes=[],
                         text_sha256=hashlib.sha256(key.encode()).hexdigest())
        storage.put(MemoryRecord(layer, key, value, scope))
    block, counts = dispatch_memory_block(
        storage, "implementer", "impl-738", MemoryScope(project="Quota"),
        local_project="quota-ops")
    assert "owner:Quota:telegram:6419236710:2154" in block
    assert "wc1:fleet" in block
    assert "local:quota-ops" in block
    assert "foreign:Quota" not in block
    assert "episode:ranked" not in block
    assert "decision:ranked" not in block
    assert counts == {"authoritative": 1, "failure_pattern": 2}


def test_dispatch_alias_keeps_the_crews_own_failure_pattern(
        tmp_path, monkeypatch, unused_tcp_port):
    from tests.unit.test_context_pack_inject_gate import _dispatch

    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    storage.put(MemoryRecord("authoritative", "quota:policy", {"text": "Quota owner rule"},
                             MemoryScope(project="Quota")))
    storage.put(MemoryRecord("failure_pattern", "local:quota-ops",
                             {"text": "Quota Ops local warning"},
                             MemoryScope(project="quota-ops")))
    storage.put(MemoryRecord("failure_pattern", "foreign:Quota",
                             {"text": "wrong local warning"},
                             MemoryScope(project="Quota")))
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", storage.path)
    monkeypatch.setenv("AGENT_CREW_MEMORY_PROJECT_ALIASES", json.dumps({"quota-ops": "Quota"}))
    result = _dispatch(tmp_path / "dispatch", monkeypatch, unused_tcp_port,
                       task_project="quota-ops")
    assert "quota:policy" in result["message"]
    assert "local:quota-ops" in result["message"]
    assert "foreign:Quota" not in result["message"]


@pytest.mark.parametrize("failure", ["missing", "exception", "timeout"])
def test_adr001_live_read_failure_keeps_message_and_zero_receipt(
        tmp_path, monkeypatch, unused_tcp_port, failure):
    from tests.unit.test_context_pack_inject_gate import _dispatch
    import agent_crew.server as server

    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", str(tmp_path / "missing.db"))
    if failure == "exception":
        def fail(*args, **kwargs):
            raise RuntimeError("read failed")
        monkeypatch.setattr(server.SQLiteMemoryStorage, "existing", fail)
    elif failure == "timeout":
        storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
        monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", storage.path)

        def slow(*args, **kwargs):
            time.sleep(2.1)
            return "late", {"authoritative": 1, "failure_pattern": 0}
        monkeypatch.setattr(server, "dispatch_memory_block", slow)
    result = _dispatch(tmp_path / "dispatch", monkeypatch, unused_tcp_port,
                       task_project="agent_crew")
    assert result["message"] == "baseline message"
    assert result["row_context"]["memory_served"] == {
        "authoritative": 0, "failure_pattern": 0}
    assert result["memory"][0]["memory_served"] == result["row_context"]["memory_served"]
