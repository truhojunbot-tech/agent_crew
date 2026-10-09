"""#661: fleet failure patterns reach the live, bounded Context Pack."""

from agent_crew import context_pack as pack
from agent_crew.memory_runtime import MemoryRecord, MemoryScope, SQLiteMemoryStorage


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
