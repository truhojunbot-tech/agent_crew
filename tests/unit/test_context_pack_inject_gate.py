"""Golden scenarios from the Context Forge contract and token-cap injection."""

import asyncio
import json
import re
from pathlib import Path

import pytest

from agent_crew import context_pack as cpack


GOLDEN = (Path(__file__).parents[1] / "fixtures" / "context_pack" /
          "context-pack-golden-set-v1.md")


def _cases():
    # The source calls expected_pack_items the artifacts a failed retrieval
    # SHOULD have returned. Its negative examples therefore omit those items.
    text = GOLDEN.read_text()
    cases = []
    for block in re.findall(r"```yaml\n(id: gc-\d+\n.*?)```", text, re.S):
        case_id = re.search(r"^id: (\S+)", block, re.M).group(1)
        category = re.search(r"^type: (\S+)", block, re.M).group(1)
        item_types = re.findall(r"^  - artifact_type: (\S+)", block, re.M)
        section = block.split("expected_missing_signals:", 1)[1].split("actual_outcome:", 1)[0]
        expected = re.findall(r'^  - "([^"]+)"', section, re.M)
        cases.append((case_id, category, item_types, expected))
    assert len(cases) == 20
    return cases


@pytest.mark.parametrize("case_id,category,item_types,expected", _cases(), ids=lambda value: value if isinstance(value, str) and value.startswith("gc-") else None)
def test_golden_missing_context(case_id, category, item_types, expected):
    # Each case tests the retrieval gap described by expected_missing_signals.
    # The published v1 file abbreviates some signals, so expand to the exact
    # strings mandated by the accompanying is-sufficient spec.
    issue = cpack.Artifact("issue", "issue://1", cpack.TYPE_ISSUE)
    ac = cpack.Artifact("ac", "issue://1#ac", cpack.TYPE_AC)
    items = [issue, ac]
    if category in {"ac", "owner_decision"} and expected:
        items.remove(ac)
    if category in {"prior_finding", "linked_review"} and expected:
        assert cpack.TYPE_REVIEW in item_types
        task_id = f"fix-{case_id}"
    else:
        task_id = case_id
        if not expected:
            items.extend(cpack.Artifact(f"{kind}-{case_id}", f"fixture://{kind}", kind)
                         for kind in item_types if kind not in {cpack.TYPE_ISSUE, cpack.TYPE_AC})
    pack = cpack.ContextPack(task_id, "implementer", cpack.MODE_LEXICAL, items=items)
    actual = cpack.is_sufficient(pack).missing_signals
    canonical = []
    for signal in expected:
        if signal.startswith("AC artifact missing"):
            canonical.append("AC artifact missing — acceptance bar unknown; annotate no_ac=true in IssueProvider if issue has none")
        elif signal.startswith("fix round requires linked_review"):
            canonical.append("fix round requires linked_review — prior findings unknown; reviewer may re-raise same issues")
        else:
            canonical.append(signal)
    assert actual == canonical


def test_all_rules_priority_and_no_ac():
    pack = cpack.ContextPack("fix-r", "implementer", cpack.MODE_LEXICAL)
    signals = cpack.is_sufficient(pack, retry_of="old").missing_signals
    assert [s.split(" ", 1)[0] for s in signals] == ["issue", "AC", "fix", "retry"]
    issue = cpack.Artifact("issue", "issue://1", cpack.TYPE_ISSUE,
                           provenance="the claimed issue; no_ac=true")
    review = cpack.Artifact("review", "review://1", cpack.TYPE_REVIEW)
    episode = cpack.Artifact("episode:old", "episode://old", cpack.TYPE_EPISODE)
    pack.items = [issue, review, episode]
    assert cpack.is_sufficient(pack, retry_of="old").ok
    block = pack.to_prompt_block(cpack.InjectGate(False, ["missing test signal"]))
    assert block.index("INJECT WARNING") < block.index("--- [issue]")


@pytest.mark.parametrize(
    "task_id,task_type,requires_review",
    [
        ("impl-tok31-fixround-x", "implement", False),
        ("fix-foo", "implement", True),
        ("impl-foo", "fix", True),
    ],
)
def test_fix_round_identification_uses_prefix_or_task_type(task_id, task_type, requires_review):
    items = [
        cpack.Artifact("issue", "issue://1", cpack.TYPE_ISSUE),
        cpack.Artifact("ac", "issue://1#ac", cpack.TYPE_AC),
    ]
    pack = cpack.ContextPack(task_id, "implementer", cpack.MODE_LEXICAL, items=items)
    signals = cpack.is_sufficient(pack, task_type=task_type).missing_signals
    assert bool(signals) is requires_review
    if requires_review:
        assert signals[0].startswith("fix round requires linked_review")


def test_builder_marks_no_ac_only_for_complete_issue(monkeypatch):
    monkeypatch.setattr(cpack.LexicalRepoProvider, "retrieve", lambda self, query: [])
    ctx = {"issue": 1, "issue_title": "A", "issue_body": "Body with no criteria"}
    pack = cpack.build_pack_for_task(ctx, task_id="t", task_type="implement", role="implementer")
    assert cpack.is_sufficient(pack).ok
    assert "no_ac=true" in next(a.provenance for a in pack.items if a.artifact_type == cpack.TYPE_ISSUE)
    missing = cpack.build_pack_for_task({"issue": 1}, task_id="t", task_type="implement",
                                         role="implementer", issue_body_fn=lambda *_: "")
    assert [s.split(" ", 1)[0] for s in cpack.is_sufficient(missing).missing_signals] == ["issue", "AC"]


def _dispatch(tmp_path, monkeypatch, port, *, gate=False, cap=False, broken=False,
              live=False, task_project=None):
    from fastapi.testclient import TestClient
    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    tmp_path.mkdir()
    wt = tmp_path / "claude"
    wt.mkdir()
    (wt / ".git").mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"worktrees": {"claude": str(wt)},
                                 "role_agents": {r: "claude" for r in ("implementer", "reviewer", "tester")}}))
    commands = []
    patches = []
    original_patch = TaskQueue.patch_context

    def spy_patch(self, task_id, patch):
        patches.append(patch)
        return original_patch(self, task_id, patch)

    async def fake_exec(*cmd, **kwargs):
        commands.append(cmd)

        class Process:
            returncode = 0
            pid = 1

            async def communicate(self):
                return b"", b""

            async def wait(self):
                return 0

        return Process()

    monkeypatch.setattr(TaskQueue, "patch_context", spy_patch)
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("agent_crew.server._format_task_message", lambda *a, **k: "baseline message")
    monkeypatch.setattr("agent_crew.server.claude_context_exceeds_cap",
                        lambda *a, **k: (cap, {"tripped_by": "tokens" if cap else "",
                                              "context_tokens": 321}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CONTEXT_PACK", "1" if live else "0")
    monkeypatch.setenv("AGENT_CREW_CONTEXT_PACK_SHADOW", "0")
    monkeypatch.setenv("AGENT_CREW_CONTEXT_PACK_INJECT_GATE", "1" if gate else "0")
    if broken:
        monkeypatch.setattr(cpack, "build_pack_for_task", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("builder broke")))

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=port, state_path=str(state),
                     project="agent_crew", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="gate-t", task_type="implement", description="do it",
                              branch="main", project=task_project,
                              context={"issue": 1, "issue_title": "A",
                                                       "issue_body": "Body without AC"}))
        task = q.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))
        row_context = q.get_task_context("gate-t")
    all_events = [json.loads(line) for line in (tmp_path / "context_events.jsonl").read_text().splitlines()]
    result = {"message": commands[0][commands[0].index("-p") + 1],
              "events": [e for e in all_events if e["event_type"] == "inject_gate"],
              "built": [e for e in all_events if e["event_type"] == "context_pack_built"],
              "memory": [e for e in all_events if e["event_type"] == "memory_served_live"],
              "resets": [e for e in all_events if e["event_type"] == "provider_context_capped"],
              "patches": patches, "row_context": row_context}
    monkeypatch.undo()
    return result


def test_dispatch_gate_only_on_token_cap(tmp_path, monkeypatch, unused_tcp_port):
    off = _dispatch(tmp_path / "off", monkeypatch, unused_tcp_port, cap=True)
    no_trip = _dispatch(tmp_path / "no-trip", monkeypatch, unused_tcp_port + 1, gate=True)
    gated = _dispatch(tmp_path / "gated", monkeypatch, unused_tcp_port + 2, gate=True, cap=True)
    assert off["message"] == no_trip["message"] == "baseline message"
    assert off["events"] == no_trip["events"] == []
    assert gated["message"].startswith("=== CONTEXT PACK")
    assert "INJECT WARNING" not in gated["message"]  # Complete issue with no AC is marked.
    assert gated["message"].endswith("baseline message")
    assert len(gated["events"]) == 1
    assert gated["events"][0]["ok"] is True
    assert gated["events"][0]["pack_tokens"] > 0
    assert gated["events"][0]["fresh_reason"] == "token_cap"
    assert len(gated["resets"]) == 1
    assert gated["patches"] == off["patches"]
    assert not any(key.startswith("context_pack_") for key in gated["row_context"])


def test_dispatch_warning_and_fail_soft(tmp_path, monkeypatch, unused_tcp_port):
    # A fix round lacks a linked review even though its issue is complete.
    original = cpack.is_sufficient
    def insufficient(pack, **kwargs):
        return cpack.InjectGate(False, ["missing linked review"])
    monkeypatch.setattr(cpack, "is_sufficient", insufficient)
    warning = _dispatch(tmp_path / "warning", monkeypatch, unused_tcp_port, gate=True, cap=True)
    assert "INJECT WARNING (ok=False):\n  - missing linked review" in warning["message"]
    assert warning["events"][0]["missing_signals"] == ["missing linked review"]
    monkeypatch.setattr(cpack, "is_sufficient", original)
    broken = _dispatch(tmp_path / "broken", monkeypatch, unused_tcp_port + 1,
                       gate=True, cap=True, broken=True)
    assert broken["message"] == "baseline message"
    assert broken["events"] == []
    assert len(broken["resets"]) == 1


def test_live_flag_wins_over_gate(tmp_path, monkeypatch, unused_tcp_port):
    result = _dispatch(tmp_path / "live", monkeypatch, unused_tcp_port,
                       gate=True, cap=True, live=True)
    assert result["events"] == []
    assert len(result["built"]) == 1
    assert result["message"].count("=== CONTEXT PACK cp") == 1
